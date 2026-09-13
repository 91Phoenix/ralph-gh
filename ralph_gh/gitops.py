"""Per-ticket git workspace lifecycle. Every git step is checked — silently
swallowing them is what puts a feature branch on top of one branch and its PR
against another, so that the PR carries dozens of unrelated commits."""

import os
import shutil
from dataclasses import dataclass
from typing import Optional

from . import gitrepo
from .config import Config
from .state import encode_key

EXCLUDED_ARTIFACTS = (".ralph-ticket.md", ".ralph-siblings.md",
                      ".ralph-pr-body.md", ".ralph-pr-comments.md",
                      ".ralph-collaudo/",
                      "BUILD_OK", "BUILD_FAIL", "COLLAUDO_OK", "COLLAUDO_FAIL")


@dataclass
class PreparedWorkspace:
    ws: str
    target: str
    base: str
    corrected_from: str = ""   # non-empty when a default target was corrected


class GitOps:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def detect_protocol(self, full: str) -> Optional[str]:
        """Transport the developer's local git auth can actually use for
        owner/repo `full` — "ssh"/"https", None when neither works."""
        return gitrepo.detect_protocol(full)

    def workspace(self, key: str) -> str:
        return os.path.join(self.cfg.workspace_base, encode_key(key))

    def workspace_exists(self, key: str) -> bool:
        return os.path.isdir(os.path.join(self.workspace(key), ".git"))

    # -- workspace lifecycle --------------------------------------------------
    def prepare_workspace(self, key: str, url: str, branch: str, target: str,
                          explicit: bool) -> Optional[PreparedWorkspace]:
        """Clone (or refresh) the per-ticket workspace on `branch`, cut from
        origin/<target> and verified to sit exactly there. `url` is
        token-free; auth is the developer's own."""
        ws = self.workspace(key)
        if not os.path.isdir(os.path.join(ws, ".git")):
            shutil.rmtree(ws, ignore_errors=True)
            if gitrepo.git(None, "clone", url, ws, nps=True).returncode != 0:
                return None
        gitrepo.apply_repo_git_config(ws, url)
        # Explicit refspec, not a bare `fetch origin`: a workspace that
        # already existed may be a --single-branch clone (or otherwise have a
        # narrowed remote.origin.fetch), in which case
        # refs/remotes/origin/<target> NEVER appears no matter how often we
        # fetch.
        if gitrepo.git(ws, "fetch", "--prune", "origin",
                       "+refs/heads/*:refs/remotes/origin/*",
                       nps=True).returncode != 0:
            return None
        # Correct a default target that is not really this repo's trunk.
        # Skipped when the ticket named the branch itself, and a no-op on the
        # retry path (the corrected target infers to itself).
        corrected_from = ""
        if not explicit:
            inferred = gitrepo.infer_trunk(ws, target, self.cfg.trunk_candidates)
            if inferred and inferred != target:
                corrected_from, target = target, inferred
        if gitrepo.git(ws, "rev-parse", "--verify", "--quiet",
                       f"refs/remotes/origin/{target}").returncode != 0:
            return None
        # One command instead of checkout-target + reset + checkout -B: -B
        # with an explicit start point cannot land anywhere else, and -f
        # discards the previous run's working-tree changes.
        if gitrepo.git(ws, "checkout", "-f", "-B", branch,
                       f"refs/remotes/origin/{target}").returncode != 0:
            return None
        # Belt and braces: HEAD must BE origin/<target> now. The base commit
        # lets the pre-PR guard prove the branch is still cut from <target>.
        base = gitrepo._out(ws, "rev-parse", "HEAD")
        if not base or base != gitrepo._out(
                ws, "rev-parse", f"refs/remotes/origin/{target}"):
            return None
        return PreparedWorkspace(ws=ws, target=target, base=base,
                                 corrected_from=corrected_from)

    def ensure_excludes(self, ws: str) -> None:
        """Loop artifacts, excluded locally so no worker can commit them."""
        path = os.path.join(ws, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path) as f:
                existing = {l.strip() for l in f}
        except OSError:
            existing = set()
        with open(path, "a") as f:
            for name in EXCLUDED_ARTIFACTS:
                if name not in existing:
                    f.write(name + "\n")

    # -- publishing ------------------------------------------------------------
    def push(self, key: str, branch: str) -> bool:
        return gitrepo.git(self.workspace(key), "push", "-u", "origin",
                           branch, "--force-with-lease",
                           nps=True).returncode == 0

    def force_push(self, ws: str, branch: str) -> bool:
        return gitrepo.git(ws, "push", "--force-with-lease", "origin",
                           branch, nps=True).returncode == 0

    def fetch(self, ws: str) -> bool:
        return gitrepo.git(ws, "fetch", "--prune", "origin",
                           "+refs/heads/*:refs/remotes/origin/*",
                           nps=True).returncode == 0

    def head_sha(self, ws: str, branch: str) -> str:
        return gitrepo._out(ws, "rev-parse", branch)

    # -- sibling-rebase helpers -------------------------------------------------
    def checkout(self, ws: str, branch: str) -> bool:
        return gitrepo.git(ws, "checkout", branch).returncode == 0

    def behind_target(self, ws: str, branch: str, target: str) -> bool:
        return bool(gitrepo._out(ws, "rev-list",
                                 f"{branch}..refs/remotes/origin/{target}"))

    def rebase_onto(self, ws: str, target: str) -> bool:
        return gitrepo.git(ws, "rebase", f"origin/{target}",
                           nps=True).returncode == 0

    def abort_rebase(self, ws: str) -> None:
        gitrepo.git(ws, "rebase", "--abort")

    def rebase_finished_clean(self, ws: str) -> bool:
        return gitrepo.rebase_finished_clean(ws)

    # -- invariants (thin wrappers so the orchestrator can be faked) ------------
    def pr_base_ok(self, ws: str, branch: str, target: str, base: str) -> bool:
        return gitrepo.pr_base_ok(ws, branch, target, base)

    def pr_retarget_candidate(self, ws: str, branch: str, current: str,
                              base: str) -> Optional[str]:
        return gitrepo.pr_retarget_candidate(ws, branch, current, base)

    def pr_foreign_count(self, ws: str, target: str, branch: str) -> int:
        return gitrepo.pr_foreign_count(ws, target, branch)

    def commits_shown(self, ws: str, branch: str, target: str) -> str:
        return gitrepo._out(ws, "rev-list", "--count",
                            f"refs/remotes/origin/{target}..{branch}")

    def has_new_commits(self, ws: str, branch: str, base: str) -> bool:
        """Did the workspace gain commits beyond the recorded base? Lets the
        park path tell a human that committed-but-unpushed work exists (a
        worker that forgot BUILD_OK otherwise looks like it did nothing)."""
        if not base:
            return False
        return bool(gitrepo._out(ws, "rev-list", "-1", f"{base}..{branch}"))

    def head_author_epoch(self, ws: str, branch: str) -> Optional[int]:
        """Author date, not committer date: sibling rebases rewrite the
        branch after every merge, which would reset a committer-date clock
        and hide exactly the ticket edits the resync check exists to catch."""
        out = gitrepo._out(ws, "log", "-1", "--format=%at", branch)
        return int(out) if out.isdigit() else None
