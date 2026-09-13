"""Git remote helpers and the branch-base invariants guarding PR creation.

The loop clones and pushes through the developer's OWN local git
authentication (an SSH key, or the HTTPS credential helper `gh auth setup-git`
installs); no token is ever embedded in a remote URL or a repository's git
config."""

import os
import re
import subprocess
from typing import Callable, Mapping, Optional


def nps_env(env: Optional[Mapping[str, str]] = None) -> dict:
    """Environment making ALL git network operations non-interactive:
    GIT_TERMINAL_PROMPT=0 kills HTTPS username/password prompts, BatchMode=yes
    kills SSH passphrase/host-key prompts — a credential problem fails fast
    instead of hanging the loop."""
    base = dict(os.environ if env is None else env)
    base["GIT_TERMINAL_PROMPT"] = "0"
    base["GIT_SSH_COMMAND"] = base.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes"
    return base


def git(ws: Optional[str], *args: str, nps: bool = False,
        env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if ws:
        cmd += ["-C", ws]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True,
                          env=nps_env(env) if nps else (dict(env) if env else None))


def _out(ws: Optional[str], *args: str) -> str:
    p = git(ws, *args)
    return p.stdout.strip() if p.returncode == 0 else ""


def remote_url(full: str, proto: Optional[str] = None,
               host: Optional[str] = None,
               env: Optional[Mapping[str, str]] = None) -> str:
    """Token-free git remote URL. Auth is supplied at clone/push time by the
    developer's local SSH key or credential helper — never by the URL."""
    env = os.environ if env is None else env
    proto = proto or env.get("RALPH_GIT_PROTOCOL", "https")
    host = host or env.get("RALPH_GIT_HOST", "github.com")
    if proto == "ssh":
        return f"git@{host}:{full}.git"
    return f"https://{host}/{full}.git"


def detect_protocol(full: str, env: Optional[Mapping[str, str]] = None,
                    probe: Optional[Callable[[str], bool]] = None
                    ) -> Optional[str]:
    """Pick the transport the local environment can actually use, with zero
    configuration. An explicit RALPH_GIT_PROTOCOL=ssh|https always wins;
    otherwise try https (what `gh auth setup-git` provides), then ssh, via a
    non-interactive `git ls-remote`. None when neither reaches the repo."""
    env = os.environ if env is None else env
    explicit = env.get("RALPH_GIT_PROTOCOL", "auto")
    if explicit in ("ssh", "https"):
        return explicit
    if probe is None:
        def probe(url: str) -> bool:
            return git(None, "ls-remote", url, nps=True).returncode == 0
    for p in ("https", "ssh"):
        if probe(remote_url(full, p, env=env)):
            return p
    return None


def apply_repo_git_config(ws: str, url: str) -> None:
    """Point origin at the token-free URL and strip any repo-local inline
    credential helper, so the workspace authenticates exactly like the
    developer's shell does. Only ever touches the repo's own config."""
    # --unset-all: MULTIPLE credential.helper entries are legal and --unset
    # exits non-zero on them, leaving an inline token-injecting helper behind.
    git(ws, "config", "--unset-all", "credential.helper")
    git(ws, "remote", "set-url", "origin", url)


# ---------------------------------------------------------------------------
# Branch-base invariants — what a PR needs before it may exist
# ---------------------------------------------------------------------------
def _ref_exists(ws: str, ref: str) -> bool:
    return git(ws, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def infer_trunk(ws: str, target: str, candidates: str) -> str:
    """The branch the work really belongs on when the ticket did not say.
    A repository's default branch can be a stale stub of the real trunk, so
    ask git: if a candidate trunk C fully CONTAINS origin/target and is ahead
    of it, target is a stub of C and C is where feature branches belong.
    Returns target unchanged when nothing qualifies (the common case)."""
    if not _ref_exists(ws, f"refs/remotes/origin/{target}"):
        return target
    for c in candidates.split():
        if c == target:
            continue
        if not _ref_exists(ws, f"refs/remotes/origin/{c}"):
            continue
        if git(ws, "merge-base", "--is-ancestor",
               f"refs/remotes/origin/{target}",
               f"refs/remotes/origin/{c}").returncode != 0:
            continue
        # Ahead as well as containing: an identical branch is not a better trunk.
        if not _out(ws, "rev-list", "-1",
                    f"refs/remotes/origin/{target}..refs/remotes/origin/{c}"):
            continue
        return c
    return target


def pr_foreign_count(ws: str, target: str, branch: str) -> int:
    """How many of the commits a PR branch -> target would display are NOT
    this run's work, i.e. already sit on another branch of the remote. Zero
    is the only healthy answer.

    Ancestry alone cannot answer this: when "main" is an ancestor of
    "development", a branch cut from development still passes every
    --is-ancestor test while the PR against main shows development's whole
    history. What distinguishes foreign work is that it is already published
    somewhere else. Commits on origin/target are not counted: those never
    appear in the PR anyway. Fails closed: an uncountable range reports -1 so
    the caller parks the ticket instead of opening a PR on a guess."""
    excl = []
    for ref in _out(ws, "for-each-ref", "--format=%(refname)",
                    "refs/remotes/origin").splitlines():
        ref = ref.strip()
        if not ref or ref in (f"refs/remotes/origin/{branch}",
                              f"refs/remotes/origin/{target}"):
            continue
        excl.append(ref)
    rng = f"refs/remotes/origin/{target}..{branch}"
    total = _out(ws, "rev-list", "--count", rng)
    mine = _out(ws, "rev-list", "--count", rng,
                *((["--not"] + excl) if excl else []))
    if not total or not mine:
        return -1
    return int(total) - int(mine)


def pr_base_ok(ws: str, branch: str, target: str, base: str) -> bool:
    """Every invariant a PR needs: the commit the workspace based the work on
    is still an ancestor of the feature branch (the worker did not re-cut it)
    and of origin/target (that really is the branch it came from), and the PR
    would show none of anybody else's commits."""
    if not base:
        return False
    if git(ws, "merge-base", "--is-ancestor", base, branch).returncode != 0:
        return False
    if git(ws, "merge-base", "--is-ancestor", base,
           f"refs/remotes/origin/{target}").returncode != 0:
        return False
    return pr_foreign_count(ws, target, branch) == 0


def pr_retarget_candidate(ws: str, branch: str, current: str, base: str
                          ) -> Optional[str]:
    """The branch the PR SHOULD target when `current` fails pr_base_ok. A
    worker handed a branch it could not build on may merge the real trunk
    into the feature branch; the PR against the old target then shows that
    trunk's entire history. Rather than park, look for the branch against
    which every pr_base_ok invariant already holds.

    Tightest fit wins — the branch whose PR would show the fewest commits —
    and only when it is STRICTLY tightest. Ties and no-candidate both return
    None and the caller parks: this only converts a park into a PR when the
    answer is unambiguous."""
    best, best_n, tie = None, None, False
    for ref in _out(ws, "for-each-ref", "--format=%(refname)",
                    "refs/remotes/origin").splitlines():
        b = ref.strip().replace("refs/remotes/origin/", "", 1)
        if not b or b == current or b == "HEAD":
            continue
        # Only long-lived branches: another ticket's topic branch is never a
        # legitimate PR destination even when the ancestry happens to fit.
        if re.match(r"^(feature|bugfix|hotfix|release|dependabot|renovate)/", b):
            continue
        if not pr_base_ok(ws, branch, b, base):
            continue
        n_str = _out(ws, "rev-list", "--count",
                     f"refs/remotes/origin/{b}..{branch}")
        if not n_str.isdigit() or int(n_str) == 0:
            continue
        n = int(n_str)
        if best_n is None or n < best_n:
            best, best_n, tie = b, n, False
        elif n == best_n:
            tie = True
    return best if best and not tie else None


def rebase_finished_clean(ws: str) -> bool:
    """True only when NO rebase is in progress and there are no unmerged
    paths, i.e. the rebase completed without leaving conflict markers."""
    if os.path.isdir(os.path.join(ws, ".git", "rebase-merge")):
        return False
    if os.path.isdir(os.path.join(ws, ".git", "rebase-apply")):
        return False
    return not _out(ws, "ls-files", "-u")
