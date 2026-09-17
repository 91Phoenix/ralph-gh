"""Env-driven configuration. One frozen dataclass holds every knob; from_env
reads the environment so nothing needs a config file.

The loop is scoped to ONE GitHub Project (v2): its items are the tickets, its
Status field is the state machine. Everything else (repo, branch, PR, checks)
is derived from the issue itself, because on GitHub an issue already lives in
a repository."""

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

# Failure reports are exempt from the compressed style on purpose: the 5% of
# output a human does read is exactly the failures. Keep the carve-out if you
# override RALPH_WORKER_SYS.
DEFAULT_WORKER_SYS = (
    "Respond wenyan-ultra caveman: ALL prose in maximally terse classical Chinese (wenyan). "
    "Extreme abbreviation, classical sentence patterns, subjects omitted, classical particles. "
    "All technical substance stay; only fluff die. State each fact once. "
    "Example style: 新參照則重繪。useMemo 包之。 "
    "No tool-call narration, no decorative tables/emoji, no long raw log dumps — quote shortest decisive line. "
    "Code, CLI commands, commit messages, file contents, API payloads, error strings, identifiers, "
    "technical terms: keep exact English verbatim, never translate, never compress. "
    "EXCEPTION — failure reporting is for a human, so it is written in plain readable ENGLISH prose, "
    "never wenyan never compressed: the contents of a BUILD_FAIL file, any statement that you are "
    "blocked or giving up, the last message when a session did not reach a green build. "
    "Progress narration stays compressed; the explanation of what went wrong does not."
)

_PROJECT_URL_RE = re.compile(
    r"^https?://github\.com/(users|orgs)/([^/]+)/projects/(\d+)/?")


def parse_project_arg(arg: str) -> Tuple[str, str, int]:
    """First CLI arg: a GitHub Project (v2) as a URL
    (https://github.com/users/<login>/projects/<n> or /orgs/<org>/projects/<n>)
    or the short form <login>/<n>. Returns (owner_kind, owner_login, number)
    where owner_kind is "users", "orgs" or "" (unknown — resolved via the
    API later)."""
    arg = (arg or "").strip()
    m = _PROJECT_URL_RE.match(arg)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9-]*)/(\d+)$", arg)
    if m:
        return "", m.group(1), int(m.group(2))
    raise ValueError(
        f"cannot parse project reference {arg!r}: expected "
        "https://github.com/users/<login>/projects/<n>, "
        "https://github.com/orgs/<org>/projects/<n>, or <login>/<n>")


def resolve_target_branch(val: Optional[str], default: str) -> str:
    """The branch a ticket's work is based on AND the destination of its PR;
    the two can never diverge. The branch the ticket names itself wins (a
    "Target branch" heading in its body); otherwise the repository's own
    default branch, which the orchestrator passes as `default`."""
    val = (val or "").strip()
    if val.startswith("refs/heads/"):
        val = val[len("refs/heads/"):]
    return val or default


def target_is_explicit(val: Optional[str]) -> bool:
    """Did the TICKET name the branch, or is the resolved value merely the
    repository default? Only a default target is ever corrected automatically;
    when a human wrote the field, a mismatch is theirs to judge."""
    val = (val or "").strip()
    if val.startswith("refs/heads/"):
        val = val[len("refs/heads/"):]
    return bool(val)


AGENTS = ("claude", "opencode")


def _agent(val: str) -> str:
    val = (val or "claude").strip().lower()
    if val not in AGENTS:
        raise ValueError(f"RALPH_AGENT must be one of {', '.join(AGENTS)}, "
                         f"got {val!r}")
    return val


BROWSERS = ("playwright", "none")

MERGE_METHODS = ("squash", "merge", "rebase")


def _merge_method(val: str) -> str:
    val = (val or "squash").strip().lower()
    if val not in MERGE_METHODS:
        raise ValueError(f"RALPH_MERGE_METHOD must be one of "
                         f"{', '.join(MERGE_METHODS)}, got {val!r}")
    return val


def _browser(val: str) -> str:
    val = (val or "playwright").strip().lower()
    if val not in BROWSERS:
        raise ValueError(f"RALPH_COLLAUDO_BROWSER must be one of "
                         f"{', '.join(BROWSERS)}, got {val!r}")
    return val


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # Scope: the GitHub Project this run works
    project_owner_kind: str     # "users" | "orgs" | "" (unknown yet)
    project_owner: str
    project_number: int

    # Optional fallback repo ("owner/repo") for issues whose Repository
    # heading is absent — normally unused: an issue's own repo is the target.
    default_full_repo: str

    # Project Status option names — pipe-separated, case-insensitive aliases
    st_ready: str
    st_inprogress: str
    st_review: str
    st_done: str
    agent_label: str            # label a ticket must carry; "" disables
    needs_human_label: str

    target_branch: str          # "" = the repository's default branch
    trunk_candidates: str

    # Loop tuning
    workspace_base: str
    max_concurrent: int
    poll_seconds: int
    max_runtime: int
    session_timeout: int
    checks_poll_max: int
    checks_poll_wait: int
    fix_cap: int
    transient_retries: int
    transient_park_cap: int
    resync: bool
    resync_cap: int
    resync_grace: int
    rebase_resolve: bool
    draft_prs: bool

    # Auto-merge — opt-in. When on, a PR whose review comments were all
    # addressed and whose checks are green at the hand-off is merged by the
    # loop instead of being left for a human. Off, the PR is handed over.
    auto_merge: bool
    merge_method: str           # "squash" | "merge" | "rebase"

    # Collaudo — optional local acceptance run of each PR (review -> collaudo
    # -> address). Off by default: it needs the app running locally.
    collaudo: bool
    collaudo_repos: str         # space-separated owner/repo or names; "" = all
    collaudo_probe: str         # shell command that must exit 0 (app is up)
    collaudo_url: str           # where the app answers; "" = from the PR recipe
    collaudo_browser: str       # "playwright" | "none"
    collaudo_agent: str         # agent for the collaudo session
    collaudo_agent_bin: str
    skill_collaudo: str

    # Worker sessions
    agent: str                  # "claude" | "opencode"
    agent_bin: str              # binary to run ("" = the agent's own name)
    agent_extra: str            # extra CLI flags for every session
    model: str
    worker_sys: str
    skill_implement: str
    skill_review: str
    git_host: str

    @property
    def state_dir(self) -> str:
        return os.path.join(self.workspace_base, ".state")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.workspace_base, ".logs")

    @property
    def agent_command(self) -> str:
        """The executable that runs a worker session: the agent's own name
        unless RALPH_AGENT_BIN points elsewhere (e.g. `kilo`, the OpenCode
        fork, with RALPH_AGENT=opencode)."""
        return self.agent_bin or self.agent

    @property
    def collaudo_agent_command(self) -> str:
        return self.collaudo_agent_bin or self.collaudo_agent

    @property
    def project_ref(self) -> str:
        return f"{self.project_owner}/{self.project_number}"

    @classmethod
    def from_env(cls, project_arg: str, default_repo_arg: str = "",
                 env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        kind, owner, number = parse_project_arg(project_arg)
        workspace_base = env.get(
            "RALPH_WORKSPACES",
            os.path.join(os.path.expanduser("~"), "ralph-gh-workspaces"))
        from .text import parse_repo
        return cls(
            project_owner_kind=kind,
            project_owner=owner,
            project_number=number,
            default_full_repo=parse_repo(default_repo_arg, owner),
            st_ready=env.get("ST_READY", "Ready|Todo|To Do"),
            st_inprogress=env.get("ST_INPROGRESS", "In Progress"),
            st_review=env.get("ST_REVIEW", "In Review|Review"),
            st_done=env.get("ST_DONE", "Done"),
            agent_label=env.get("AGENT_LABEL", "ready-for-agent"),
            needs_human_label=env.get("NEEDS_HUMAN_LABEL", "needs-human"),
            target_branch=env.get("TARGET_BRANCH", ""),
            trunk_candidates=env.get("RALPH_TRUNK_CANDIDATES",
                                     "development develop main master"),
            workspace_base=workspace_base,
            max_concurrent=_int(env, "MAX_CONCURRENT", 2),
            poll_seconds=_int(env, "POLL_SECONDS", 300),
            max_runtime=_int(env, "MAX_RUNTIME", 18000),
            session_timeout=_int(env, "SESSION_TIMEOUT", 5400),
            checks_poll_max=_int(env, "CHECKS_POLL_MAX", 40),
            checks_poll_wait=_int(env, "CHECKS_POLL_WAIT", 30),
            fix_cap=_int(env, "FIX_CAP", 2),
            transient_retries=_int(env, "RALPH_TRANSIENT_RETRIES", 1),
            transient_park_cap=_int(env, "RALPH_TRANSIENT_PARK_CAP", 3),
            resync=env.get("RALPH_RESYNC", "1") == "1",
            resync_cap=_int(env, "RALPH_RESYNC_CAP", 2),
            resync_grace=_int(env, "RALPH_RESYNC_GRACE", 600),
            rebase_resolve=env.get("RALPH_REBASE_RESOLVE", "1") == "1",
            draft_prs=env.get("RALPH_DRAFT_PRS", "1") == "1",
            auto_merge=env.get("RALPH_AUTO_MERGE", "0") == "1",
            merge_method=_merge_method(env.get("RALPH_MERGE_METHOD", "squash")),
            collaudo=env.get("RALPH_COLLAUDO", "0") == "1",
            collaudo_repos=env.get("RALPH_COLLAUDO_REPOS", ""),
            collaudo_probe=env.get("RALPH_COLLAUDO_PROBE", ""),
            collaudo_url=env.get("RALPH_COLLAUDO_URL", ""),
            collaudo_browser=_browser(env.get("RALPH_COLLAUDO_BROWSER", "playwright")),
            collaudo_agent=_agent(env.get("RALPH_COLLAUDO_AGENT",
                                          env.get("RALPH_AGENT", "claude"))),
            collaudo_agent_bin=env.get("RALPH_COLLAUDO_AGENT_BIN",
                                       env.get("RALPH_AGENT_BIN", "")),
            skill_collaudo=env.get("RALPH_SKILL_COLLAUDO", "collaudo-locale"),
            agent=_agent(env.get("RALPH_AGENT", "claude")),
            agent_bin=env.get("RALPH_AGENT_BIN", ""),
            agent_extra=env.get("RALPH_AGENT_EXTRA_ARGS",
                                env.get("RALPH_WORKER_EXTRA_ARGS", "")),
            model=env.get("RALPH_MODEL", ""),
            worker_sys=env.get("RALPH_WORKER_SYS", DEFAULT_WORKER_SYS),
            skill_implement=env.get("RALPH_SKILL_IMPLEMENT", "tdd"),
            skill_review=env.get("RALPH_SKILL_REVIEW", "code-review"),
            git_host=env.get("RALPH_GIT_HOST", "github.com"),
        )
