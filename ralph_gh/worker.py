"""Claude worker sessions (headless, permissions bypassed, run inside the
ticket's workspace) and the BUILD_OK / BUILD_FAIL / .ralph-pr-body.md
protocol between them and the orchestrator.

Workers talk to GitHub with the `gh` CLI they inherit from the developer's
shell — the same auth the loop itself uses. No MCP server is configured."""

import os
import re
import signal
import subprocess
from typing import Callable, List, Optional, Tuple

from .config import Config

# The implement phase writes the pull request body here; the orchestrator
# reads it when it opens the PR. Git-excluded like the other loop artifacts
# (see gitops.EXCLUDED_ARTIFACTS) so no worker can commit it.
PR_BODY = ".ralph-pr-body.md"
PR_COMMENTS = ".ralph-pr-comments.md"
TICKET = ".ralph-ticket.md"
SIBLINGS = ".ralph-siblings.md"

_TRANSIENT_RE = re.compile(
    r"API Error|Response stalled|overloaded|rate.?limit|529|503"
    r"|Service Unavailable|stream (disconnected|interrupted|ended|error)"
    r"|ECONNRESET|network error|timed out|Prompt is too long"
    # A brief DNS/connectivity blip is infrastructure, not a build failure:
    # it must requeue the ticket, never park it needs-human.
    r"|Could not resolve host|getaddrinfo|ENOTFOUND|EAI_AGAIN|ETIMEDOUT"
    r"|Temporary failure in name resolution|Connection refused"
    r"|connection reset|fetch failed", re.I)

# How a session ended, as far as the log tail can tell.
CLEAN = "clean"          # nothing failure-shaped in the tail
TRANSIENT = "transient"  # worth another attempt
TERMINAL = "terminal"    # no attempt can get further; quote it and stop

_STATUS_RE = re.compile(r"API Error:?\s*(\d{3})\b", re.I)
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
_TERMINAL_RE = re.compile(
    r"authentication_error|permission_error|not_found_error|Invalid API key"
    r"|Credit balance", re.I)
# Known 400s that a retry does clear.
_RETRYABLE_4XX_RE = re.compile(r"Prompt is too long|not found in available tools", re.I)

_FAILURE_HINTS = (
    ("not found in available tools",
     "a tool interceptor configured on this machine referenced a tool the "
     "worker session does not load; check enabledPlugins and "
     "env.ANTHROPIC_BASE_URL in ~/.claude/settings.json"),
    ("Invalid API key", "the worker environment has no usable Anthropic credential"),
    ("Credit balance", "the Anthropic account is out of credit"),
    ("gh auth", "the gh CLI is not authenticated in the worker environment; "
                "run `gh auth login`"),
)


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
def build_ok(ws: str) -> bool:
    return os.path.isfile(os.path.join(ws, "BUILD_OK"))


def build_failed(ws: str) -> bool:
    return os.path.isfile(os.path.join(ws, "BUILD_FAIL"))


def clear_markers(ws: str) -> None:
    for name in ("BUILD_OK", "BUILD_FAIL"):
        try:
            os.unlink(os.path.join(ws, name))
        except OSError:
            pass


def clear_pr_body(ws: str) -> None:
    """Separate from clear_markers on purpose: pipeline-fix clears markers
    before the PR is opened and must not blank the description."""
    try:
        os.unlink(os.path.join(ws, PR_BODY))
    except OSError:
        pass


def _one_line(path: str, cap: int, note: str) -> str:
    try:
        with open(path, errors="replace") as f:
            txt = " ".join(f.read().split())
    except OSError:
        return ""
    if len(txt) > cap:
        return txt[:cap] + note
    return txt


def build_fail_reason(ws: str) -> str:
    return _one_line(os.path.join(ws, "BUILD_FAIL"), 600,
                     f"… (truncated, full text in {ws}/BUILD_FAIL)")


def pr_body(ws: str) -> str:
    """The worker's PR description, Markdown preserved."""
    try:
        with open(os.path.join(ws, PR_BODY), errors="replace") as f:
            txt = f.read().strip()
    except OSError:
        return ""
    cap = 30000
    if len(txt) > cap:
        return txt[:cap] + "\n\n_(description truncated by ralph-gh at 30000 characters.)_"
    return txt


def write_pr_comments(ws: str, md: str) -> None:
    path = os.path.join(ws, PR_COMMENTS)
    if md:
        with open(path, "w") as f:
            f.write(md)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Session outcome classification
# ---------------------------------------------------------------------------
def session_failure(logf: str) -> Tuple[str, str]:
    """(CLEAN|TRANSIENT|TERMINAL, decisive line) from the last lines of a
    session log. Only the tail counts: an error the session recovered from
    mid-way is not how it ended."""
    try:
        with open(logf, errors="replace") as f:
            tail = f.read().splitlines()[-6:]
    except OSError:
        return CLEAN, ""
    for raw in reversed(tail):
        line = " ".join(raw.split())
        if len(line) > 300:
            line = line[:300] + "…"
        if _RETRYABLE_4XX_RE.search(line):
            return TRANSIENT, line
        if _TERMINAL_RE.search(line):
            return TERMINAL, line
        m = _STATUS_RE.search(line)
        if m:
            code = int(m.group(1))
            return (TRANSIENT if code in _RETRYABLE_STATUS else TERMINAL), line
        if _TRANSIENT_RE.search(line):
            return TRANSIENT, line
    return CLEAN, ""


def failure_hint(line: str) -> str:
    low = (line or "").lower()
    for needle, hint in _FAILURE_HINTS:
        if needle.lower() in low:
            return hint
    return ""


def keep_failed_log(logf: str, attempt: int) -> str:
    """Move a dead attempt's log aside so the retry does not overwrite the
    evidence. Returns the new path, "" when there was nothing to keep."""
    dst = f"{logf}.attempt{attempt}"
    try:
        os.replace(logf, dst)
        return dst
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _exec(cmd: List[str], cwd: str, logf: str, timeout: int,
          env: Optional[dict] = None) -> int:
    with open(logf, "w") as log:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=log,
                                stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                proc.terminate()
            proc.wait()
            return 124


class WorkerRunner:
    def __init__(self, cfg: Config, exec_fn: Optional[Callable] = None):
        self.cfg = cfg
        self.exec_fn = exec_fn or _exec

    def command(self, ws: str, prompt: str) -> List[str]:
        cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.worker_sys:
            cmd += ["--append-system-prompt", self.cfg.worker_sys]
        cmd += ["--add-dir", ws]
        extra = os.environ.get("RALPH_WORKER_EXTRA_ARGS", "").split()
        return cmd + extra

    def env(self) -> dict:
        env = dict(os.environ)
        env["RALPH_GH"] = "1"
        # A worker must never inherit an interactive-only setting that makes
        # `claude -p` wait on a terminal.
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        return env

    def run(self, ws: str, title: str, prompt: str, logf: str) -> int:
        return self.exec_fn(self.command(ws, prompt), ws, logf,
                            self.cfg.session_timeout, self.env())


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_HUMAN_TEXT = ("PLAIN ENGLISH, never compressed, never any other language — a "
               "human reads it")


def _ci_hint() -> str:
    return ("Find the project's build and test commands by reading "
            ".github/workflows/*.yml (the `run:` steps of the CI job) plus the "
            "Makefile / package.json / pom.xml / build.gradle / pyproject.toml "
            "that exist, and run THE SAME commands locally")


def _skill(name: str, what: str) -> str:
    return (f"Use the /{name} skill for {what}; if that skill is not available "
            f"in this session, follow the same discipline by hand")


def _markers(commit_msg: str, branch: str) -> str:
    return (
        f"ONLY when the full build and all tests pass locally, write a file "
        f"named BUILD_OK at the repo root containing the exact commands you "
        f"ran. If you cannot reach green, write BUILD_FAIL instead — "
        f"{_HUMAN_TEXT} — four short lines: what you tried, the commands you "
        f"ran, the single most decisive error line verbatim, and what a human "
        f"must decide or fix. The orchestrator reads ONLY these files; a "
        f"session that leaves neither is treated as dead. "
        f"Commit ALL work to the current git branch ({branch}) with a "
        f"Conventional Commit message like `{commit_msg}`. Do NOT push and do "
        f"NOT open, merge or comment on a pull request — the orchestrator does "
        f"that. BUILD_OK, BUILD_FAIL and every .ralph-*.md file are "
        f"git-excluded: never commit them, never force-add them.")


def implement_prompt(cfg: Config, key: str, number: int, summary: str,
                     full: str, branch: str, target: str) -> str:
    return (
        f"You are implementing GitHub issue {key} (\"{summary}\") in the "
        f"repository {full}, on branch {branch}, which was cut from {target} "
        f"and whose pull request will target {target}.\n\n"
        f"1. Read {TICKET} at the repo root: the full issue (description, "
        f"references, acceptance criteria). Read {SIBLINGS} too if it exists — "
        f"one line per other item of the same project, CONTEXT ONLY: implement "
        f"{key} and nothing else, but before you add a loop, a scheduled "
        f"rewrite or an event emission, check whether a sibling item consumes "
        f"what you write and size it for real production volume, not the test "
        f"fixture. You may read the issue and its comments with "
        f"`gh issue view {number} --repo {key.split('#')[0]} --comments`.\n"
        f"2. {_skill(cfg.skill_implement, 'test-first implementation (red-green-refactor) at sensible seams')}. "
        f"Acceptance criteria describe one actor doing one thing; if the code "
        f"has a second writer in production (a poller, a job, a second "
        f"replica), write the red test for the race and make the write "
        f"conditional yourself.\n"
        f"3. {_ci_hint()}; typecheck/compile often, the full suite at the end.\n"
        f"4. {_markers(f'feat(#{number}): <summary>', branch)}\n"
        f"5. In the SAME session that reached green, write the pull request "
        f"description to a file named {PR_BODY} at the repo root (the "
        f"orchestrator reads it when it opens the PR; a missing file means the "
        f"PR gets no description). Markdown, {_HUMAN_TEXT}. Exactly these "
        f"sections:\n"
        f"   ## What this does — what the change does and why: a few sentences "
        f"plus one bullet per moving part naming the files/classes a reviewer "
        f"should open first. Do not just restate the issue title.\n"
        f"   ## Design notes — every decision a reviewer would otherwise "
        f"reverse-engineer: tradeoffs, what a sibling item or an existing "
        f"convention forced on you, what an acceptance criterion made you do "
        f"differently. Drop the section only if there is genuinely nothing.\n"
        f"   ## How to test manually — a numbered recipe a human can paste "
        f"from a fresh checkout of this branch, using THE COMMANDS THAT ARE "
        f"ACTUALLY IN THE REPO (Makefile, docker-compose.yml, package.json "
        f"scripts, README) — never an invented command. Cover: bringing the "
        f"service and its dependencies up, any migration/seed/env var needed, "
        f"the exact request (curl with a real body) or UI click-path that "
        f"exercises the change, and the response/log/screen state that PROVES "
        f"it works. If a part cannot run locally, say so and give the closest "
        f"local substitute.\n"
        f"   ## Automated checks — the build and test commands you ran with "
        f"their result, then each new or changed test by name and the "
        f"behaviour it pins down.\n"
        f"6. Do NOT merge, rebase or cherry-pick from any other branch: "
        f"{branch} was cut from {target} and the PR targets {target}, so "
        f"anything pulled from elsewhere shows up as somebody else's commits. "
        f"If {target} lacks code you need (empty or stub branch, missing "
        f"module), do NOT fetch it from another branch — write BUILD_FAIL "
        f"saying which branch the work belongs on and why."
    )


def pipeline_fix_prompt(cfg: Config, key: str, number: int, full: str,
                        branch: str, pr: str) -> str:
    return (
        f"The GitHub Actions checks for pull request #{pr} (repository {full}, "
        f"branch {branch}, issue {key}) failed.\n\n"
        f"1. Find the failing run and its log with the gh CLI: "
        f"`gh run list --repo {full} --branch {branch} --limit 10`, then "
        f"`gh run view <run-id> --repo {full} --log-failed`; "
        f"`gh pr checks {pr} --repo {full}` lists every check. "
        f"Legacy commit statuses show in `gh api repos/{full}/commits/$(git rev-parse HEAD)/status`.\n"
        f"2. Diagnose and fix the failure in this working copy. "
        f"{_skill(cfg.skill_implement, 'any code change (pin the fix with a test)')}.\n"
        f"3. {_ci_hint()}.\n"
        f"4. {_markers(f'fix(#{number}): make CI green', branch)} For BUILD_FAIL "
        f"the four lines are: which check failed, the decisive error line "
        f"verbatim, what you tried, what a human must decide."
    )


def review_prompt(cfg: Config, key: str, number: int, pr: str, full: str,
                  branch: str, target: str) -> str:
    return (
        f"Review pull request #{pr} in {full} (branch {branch} -> {target}), "
        f"the implementation of issue {key}.\n\n"
        f"1. Fetch the diff with `gh pr diff {pr} --repo {full}`; the full "
        f"checked-out code is in the working directory for context, and "
        f"`gh issue view {number} --repo {key.split('#')[0]}` gives the "
        f"acceptance criteria.\n"
        f"2. {_skill(cfg.skill_review, 'concise, actionable findings: correctness, tests, fit against the acceptance criteria')}. "
        f"Skip pure style nits.\n"
        f"3. Post EACH finding as its own comment. For a finding tied to a "
        f"line use an inline review comment: `gh api repos/{full}/pulls/{pr}/comments "
        f"-f body='...' -f commit_id=$(git rev-parse HEAD) -f path='<file>' "
        f"-F line=<line> -f side=RIGHT`. For a general finding use "
        f"`gh pr comment {pr} --repo {full} --body '...'`. Write comments in "
        f"{_HUMAN_TEXT}.\n"
        f"4. If there are no substantive issues, post exactly one comment: "
        f"`LGTM — no blocking issues.`\n"
        f"5. Change no code, commit nothing, push nothing: this session only "
        f"reviews."
    )


def address_prompt(cfg: Config, key: str, number: int, pr: str, full: str,
                   branch: str) -> str:
    return (
        f"Pull request #{pr} in {full} (issue {key}, branch {branch}) has "
        f"review comments.\n\n"
        f"1. Read {PR_COMMENTS} at the repo root: every comment as of now, "
        f"with ids. The live view is `gh api repos/{full}/pulls/{pr}/comments` "
        f"(inline) and `gh pr view {pr} --repo {full} --comments`.\n"
        f"2. For EACH actionable comment do exactly one of: (a) implement the "
        f"fix in this working copy, or (b) reply explaining why it does not "
        f"apply — inline threads via "
        f"`gh api repos/{full}/pulls/{pr}/comments/<id>/replies -f body='...'`, "
        f"conversation comments via `gh pr comment {pr} --repo {full} --body '...'`. "
        f"Answer every comment one way or the other; replies in {_HUMAN_TEXT}. "
        f"`LGTM` needs no answer.\n"
        f"3. {_skill(cfg.skill_implement, 'every code change (a fix arrives with the test that pins it)')}.\n"
        f"4. If you changed code: {_ci_hint()}, and then {_markers(f'fix(#{number}): address review', branch)} "
        f"If no code change was needed, do NOT create BUILD_OK."
    )


def resync_prompt(cfg: Config, key: str, number: int, pr: str, full: str,
                  branch: str, reason: str) -> str:
    return (
        f"Issue {key} is already implemented on branch {branch}, with pull "
        f"request #{pr} open in {full}. Since the branch's last commit, "
        f"{reason}.\n\n"
        f"1. Re-read {TICKET} at the repo root: the issue AS IT IS NOW — "
        f"acceptance criteria may have been added or changed after the "
        f"implementation. `gh issue view {number} --repo {key.split('#')[0]} "
        f"--comments` shows the discussion.\n"
        f"2. List every acceptance criterion and, for EACH one, point at the "
        f"code or test on this branch that satisfies it. Anything you cannot "
        f"point at is the gap.\n"
        f"3. Close ONLY that gap, test-first "
        f"({_skill(cfg.skill_implement, 'it')}). Do not refactor, do not "
        f"re-implement what already works, do not touch anything outside the "
        f"gap.\n"
        f"4. If there is no gap, change NOTHING and do not commit: post one "
        f"comment on the PR (`gh pr comment {pr} --repo {full} --body '...'`) "
        f"saying which criteria you re-checked and that the branch already "
        f"satisfies them.\n"
        f"5. If you changed code: {_ci_hint()}, and then "
        f"{_markers(f'fix(#{number}): resync with the updated issue', branch)} "
        f"Do NOT rebase onto another branch."
    )


def rebase_resolve_prompt(branch: str, target: str) -> str:
    return (
        f"You are in the middle of a `git rebase` of branch {branch} onto "
        f"{target} that has STOPPED on merge conflicts. Use the "
        f"/resolving-merge-conflicts skill if it is available. Resolve every "
        f"conflict preserving both sides' intent, stage the results and run "
        f"`git rebase --continue` until the rebase is FULLY complete (`git "
        f"status` shows no rebase in progress). Do NOT run `git rebase "
        f"--abort`. Do NOT push. If a conflict is genuinely unresolvable, still "
        f"do not abort — leave it as it is and stop."
    )
