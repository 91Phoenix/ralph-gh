"""The per-ticket pipeline and the loop's bookkeeping.

Per ticket (fresh Claude session each phase):
implement (test-first) -> push -> branch-base guard -> open a draft PR ->
verify GitHub checks (auto-fix capped) -> mark ready for review -> review
(comments on the PR) -> address comments (one round) -> leave the PR open
for a human merge, or — with RALPH_AUTO_MERGE=1 — merge it when every review
thread was answered and the checks are green. Merged PRs close their issue, unblock dependants and
rebase the loop's other open PRs on the same repo. Open PRs whose issue was
edited after the branch's last commit get a capped catch-up pass (resync).

Transient infrastructure failures (API stalls, DNS/network blips, a failed
push) REQUEUE the ticket for a later poll; only a genuine BUILD_FAIL or a
persistently failing check run parks it needs-human."""

import os
import time
from typing import Callable, List, Optional

from . import collaudo as collaudo_mod
from . import github as gh_mod
from . import worker
from .config import Config, resolve_target_branch, target_is_explicit
from .gitrepo import remote_url
from .text import branch_for, parse_repo, split_key

# push / verify outcomes
OK = "ok"
PUSH_FAILED = "push-failed"          # git/network problem — retryable
PIPELINE_FAILED = "pipeline-failed"  # the code is red — a human's problem
# bring_up_to_date(): the branch already sat on the target's tip, was rebased
# cleanly, was rebased with conflicts Claude resolved, or could not be rebased
# (the rebase was aborted and the branch left as it was).
FRESH, REBASED, RESOLVED, CONFLICT = "fresh", "rebased", "resolved", "conflict"

# states that must not be relaunched by the poller
ACTIVE_OR_FINISHED = ("running", "resync", "pr_open", "done")

TAG = "ralph-gh"


class Orchestrator:
    def __init__(self, cfg: Config, tracker, gh, ops, state, runner,
                 log: Optional[Callable[[str], None]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time,
                 collaudo_runner=None):
        self.cfg = cfg
        self.tracker = tracker
        self.gh = gh
        self.ops = ops
        self.state = state
        self.runner = runner
        self.collaudo_runner = collaudo_runner or runner
        self.log = log or (lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}",
                                           flush=True))
        self.sleep = sleep
        self.now = now
        tracker.on_write = self.absorb_own_write

    def absorb_own_write(self, key: str) -> None:
        """Move the resync watermark past an edit the loop itself just made.
        Every comment and status change bumps the issue's `updated_at`
        exactly like a human edit would; without this, closing a pipeline
        would spend a resync pass re-reading an issue only the loop touched.
        A real edit lands with a higher `updated_at` than the stamp, so it
        still triggers a pass."""
        if not self.state.state_of(key):
            return
        ep = self.tracker.updated_epoch(key)
        if ep and ep > int(self.state.get(key, "resync_at") or 0):
            self.state.set(key, "resync_at", ep)

    def warn(self, msg: str) -> None:
        self.log(f"WARN: {msg}")

    # -----------------------------------------------------------------------
    # Worker context
    # -----------------------------------------------------------------------
    def write_siblings(self, frontier_data: dict) -> None:
        from .frontier import siblings_md
        path = os.path.join(self.cfg.state_dir, "siblings.md")
        with open(path, "w") as f:
            f.write(siblings_md(frontier_data))

    def dump_ticket_context(self, key: str, ws: str) -> None:
        """The worker's 'what am I building': the issue itself and the
        siblings around it. Both git-excluded in the workspace."""
        with open(os.path.join(ws, worker.TICKET), "w") as f:
            f.write(self.tracker.issue_md(key))
        sib_path = os.path.join(self.cfg.state_dir, "siblings.md")
        try:
            with open(sib_path) as f:
                lines = [l for l in f.read().splitlines()
                         if l and not l.startswith(f"- {key} ")]
        except OSError:
            lines = []
        target = os.path.join(ws, worker.SIBLINGS)
        if lines:
            with open(target, "w") as f:
                f.write(f"# Other items in project {self.cfg.project_ref} — "
                        f"CONTEXT ONLY, implement {key} and nothing else\n\n")
                f.write("\n".join(lines) + "\n")
        else:
            try:
                os.unlink(target)
            except OSError:
                pass

    def _pr_description(self, key: str, full: str, ws: str) -> str:
        """The PR body: the implement worker's own description, stamped with
        where it came from and the closing keyword that ties it to the issue.
        The fallback for a worker that skipped the file says so, instead of
        letting an empty PR look intentional."""
        owner, repo, number = split_key(key)
        link = (f"Closes #{number}" if full.casefold() == f"{owner}/{repo}".casefold()
                else f"Closes {key}")
        head = (f"Automated implementation of {key} by {TAG}, written "
                f"test-first; local build + tests green before the PR was "
                f"opened.\n\n{link}")
        body = worker.pr_body(ws)
        if not body:
            return (f"{head}\n\nThe worker left no `{worker.PR_BODY}`, so "
                    f"this PR has no written summary and no manual-test "
                    f"recipe — read the diff against the issue. Ask for the "
                    f"description before approving.")
        return f"{head}\n\n{body}"

    def _log_path(self, key: str, phase: str) -> str:
        os.makedirs(self.cfg.log_dir, exist_ok=True)
        from .state import encode_key
        return os.path.join(self.cfg.log_dir, f"{encode_key(key)}-{phase}.log")

    def _failure_detail(self, line: str) -> str:
        if not line:
            return ""
        hint = worker.failure_hint(line)
        detail = f" The sessions died on: `{line}`."
        if hint:
            detail += (f" Retrying did not clear it, and it will keep "
                       f"happening until this changes: {hint}.")
        return detail

    def _run_worker_retrying(self, ws: str, title: str, prompt: str,
                             logf: str, runner=None) -> str:
        """Run a worker session, retrying when it died on a retryable API
        error. A terminal failure is not retried — no attempt can get further.
        Returns "" when the session ended cleanly, else the error line."""
        runner = runner or self.runner
        for attempt in range(1 + self.cfg.transient_retries):
            runner.run(ws, title, prompt, logf)
            kind, line = worker.session_failure(logf)
            if kind == worker.CLEAN:
                return ""
            if kind == worker.TERMINAL:
                hint = worker.failure_hint(line)
                self.warn(f"{title}: session failed on a NON-RETRYABLE error "
                          f"— {line}" + (f" ({hint})" if hint else ""))
                return line
            if attempt < self.cfg.transient_retries:
                kept = worker.keep_failed_log(logf, attempt + 1)
                self.warn(f"{title}: session died on a transient error — "
                          f"retrying ({line})"
                          + (f"; failed log kept at {kept}" if kept else ""))
            else:
                self.warn(f"{title}: session still dying after "
                          f"{attempt + 1} attempts — {line}")
                return line
        return ""

    # -----------------------------------------------------------------------
    # Checks gate
    # -----------------------------------------------------------------------
    def verify_checks(self, key: str, full: str, branch: str, pr: str) -> str:
        """Poll the GitHub checks on the branch head, auto-fixing a red run
        up to fix_cap times (each fix re-pushes). Returns OK, PUSH_FAILED or
        PIPELINE_FAILED. A branch with no CI at all passes on the strength of
        the local build gate."""
        ws = self.ops.workspace(key)
        _, _, number = split_key(key)
        attempt = 0
        while True:
            status, polls = gh_mod.NONE, 0
            while polls < self.cfg.checks_poll_max:
                sha = self.ops.head_sha(ws, branch)
                status = self.gh.checks_status(full, sha)
                if status == gh_mod.SUCCESS:
                    self.log(f"{key}: checks SUCCESS")
                    return OK
                if status == gh_mod.FAILED:
                    break
                if status == gh_mod.NONE and polls >= 2:
                    # no CI for this commit — the local gate already vouched
                    return OK
                self.sleep(self.cfg.checks_poll_wait)
                polls += 1
            if status != gh_mod.FAILED:
                self.warn(f"{key}: checks still {status} after "
                          f"{self.cfg.checks_poll_max} polls — treating as failed")
            attempt += 1
            if attempt > self.cfg.fix_cap:
                self.warn(f"{key}: checks still red after "
                          f"{self.cfg.fix_cap} fix attempts")
                return PIPELINE_FAILED
            self.log(f"{key}: checks {status} — auto-fix "
                     f"{attempt}/{self.cfg.fix_cap}")
            worker.clear_markers(ws)
            self.runner.run(ws, f"pipeline-fix {key}",
                            worker.pipeline_fix_prompt(self.cfg, key, number,
                                                       full, branch, pr),
                            self._log_path(key, f"pipelinefix-{attempt}"))
            if not worker.build_ok(ws):
                reason = worker.build_fail_reason(ws)
                self.warn(f"{key}: fix session did not reach green"
                          + (f": {reason}" if reason else ""))
                return PIPELINE_FAILED
            if not self.ops.push(key, branch):
                self.warn(f"{key}: re-push failed")
                return PUSH_FAILED
            self.log(f"{key}: re-pushed after fix {attempt}")

    def bring_up_to_date(self, key: str, ws: str, branch: str, target: str) -> str:
        """Rebase `branch` onto the target's tip when it has fallen behind.

        A branch falls behind whenever a sibling merges while this one is
        being implemented or reviewed, and a PR left that way shows as
        CONFLICTING to the human it is handed to. Fetch first, so "behind" is
        judged against the remote and not this workspace's memory of it. A
        rebase that stops on conflicts gets one Claude session to finish it
        (RALPH_REBASE_RESOLVE); if it still is not clean the rebase is aborted
        and the branch left exactly as it was. Nothing is pushed here: the
        caller knows whether the branch is on the remote yet.

        Returns FRESH, REBASED, RESOLVED or CONFLICT."""
        self.ops.fetch(ws)
        if not self.ops.behind_target(ws, branch, target):
            return FRESH
        self.log(f"{key}: {branch} is behind {target} — rebasing")
        if self.ops.rebase_onto(ws, target):
            return REBASED
        if self.cfg.rebase_resolve:
            self.log(f"{key}: rebase conflicts — attempting Claude resolution")
            self.runner.run(ws, f"rebase-resolve {key}",
                            worker.rebase_resolve_prompt(branch, target),
                            self._log_path(key, "rebase-resolve"))
            if self.ops.rebase_finished_clean(ws) and \
                    not self.ops.behind_target(ws, branch, target):
                return RESOLVED
            self.warn(f"{key}: Claude could not cleanly finish the rebase")
        self.ops.abort_rebase(ws)
        return CONFLICT

    def push_and_verify(self, key: str, full: str, branch: str, pr: str) -> str:
        """Local build gate + push + checks verify. Assumes the current
        worker already committed on `branch` and wrote BUILD_OK."""
        ws = self.ops.workspace(key)
        if not worker.build_ok(ws):
            self.warn(f"{key}: worker did not produce BUILD_OK — not pushing.")
            return PIPELINE_FAILED
        if not self.ops.push(key, branch):
            self.warn(f"{key}: git push failed")
            return PUSH_FAILED
        self.log(f"{key}: pushed {branch}")
        return self.verify_checks(key, full, branch, pr)

    # -----------------------------------------------------------------------
    # Parking / requeueing
    # -----------------------------------------------------------------------
    def _park(self, key: str, msg: str, back_to_ready: bool = True) -> None:
        if back_to_ready:
            self.tracker.set_status(key, self.cfg.st_ready, msg)
        else:
            self.tracker.comment(key, msg)
        self.tracker.label_needs_human(key)
        self.state.set(key, "state", "failed")

    def _transient_requeue(self, key: str, transient: bool, requeue_msg: str,
                           park_msg: str) -> None:
        deaths = int(self.state.get(key, "transient_deaths") or 0) + 1
        self.state.set(key, "transient_deaths", deaths)
        cap = self.cfg.transient_park_cap
        if transient and deaths < cap:
            self.state.set(key, "state", "requeued")
            self.tracker.requeue(key, self.cfg.st_ready,
                                 requeue_msg.format(n=deaths, cap=cap))
            return
        self._park(key, park_msg)

    # -----------------------------------------------------------------------
    # The full per-ticket pipeline
    # -----------------------------------------------------------------------
    def run_pipeline(self, key: str, summary: str, repoval: str,
                     branchval: str = "") -> None:
        cfg = self.cfg
        owner, repo, number = split_key(key)
        full = (parse_repo(repoval, owner, cfg.git_host) or cfg.default_full_repo
                or f"{owner}/{repo}")
        if not self.gh.repo_reachable(full):
            self.warn(f"{key}: repository '{full}' not reachable — skipping")
            self._park(key, f"{TAG}: repository '{full}' (the issue's target "
                            f"repository) is not reachable with the gh CLI's "
                            f"credentials; needs a human.", back_to_ready=False)
            return
        proto = self.ops.detect_protocol(full)
        if not proto:
            self.warn(f"{key}: local git auth cannot reach '{full}' — skipping")
            self._park(key, f"{TAG} preflight: no local git access to '{full}' "
                            f"— tried https (credential helper) and ssh. Test "
                            f"with `git ls-remote`; run `gh auth setup-git` or "
                            f"load an SSH key, then re-run.", back_to_ready=False)
            return
        default_target = cfg.target_branch or self.gh.default_branch(full)
        if not default_target:
            self._park(key, f"{TAG}: could not determine the default branch of "
                            f"{full}; set TARGET_BRANCH or add a `## Target "
                            f"branch` heading to the issue.", back_to_ready=False)
            return
        target = resolve_target_branch(branchval, default_target)
        explicit = target_is_explicit(branchval)
        branch = branch_for(key, full)

        self.state.set(key, "repo", full)
        self.state.set(key, "branch", branch)
        self.state.set(key, "started", int(self.now()))
        self.state.set(key, "state", "running")
        self.state.mark_project(key)

        self.log(f"{key}: claim + prepare workspace ({full} {branch} -> {target})")
        self.tracker.claim(key, cfg.st_inprogress)
        url = remote_url(full, proto, cfg.git_host)

        prep = self._prepare(key, url, branch, target, explicit)
        if prep is None:
            self._park(key, f"{TAG}: could not prepare a workspace based on "
                            f"`{target}` in {full} (branch missing, or the clone "
                            f"could not be put on it). Nothing was implemented; "
                            f"needs a human. Add a `## Target branch` heading to "
                            f"the issue if the trunk is not `{default_target}`.")
            return
        target, ws, base = prep.target, prep.ws, prep.base
        self.ops.ensure_excludes(ws)

        # ---- Phase 1: implement ----
        # A finished worker writes BUILD_OK or BUILD_FAIL. NEITHER means the
        # session died mid-stream: retry from a clean workspace before giving
        # up; a genuine BUILD_FAIL is never retried.
        impl_log = self._log_path(key, "implement")
        kind, line = worker.CLEAN, ""
        for attempt in range(1 + cfg.transient_retries):
            worker.clear_markers(ws)
            worker.clear_pr_body(ws)
            self.dump_ticket_context(key, ws)
            self.log(f"{key}: implement session (attempt {attempt + 1})")
            self.runner.run(ws, f"implement {key}",
                            worker.implement_prompt(cfg, key, number, summary,
                                                    full, branch, target),
                            impl_log)
            if worker.build_failed(ws) or worker.build_ok(ws):
                break
            kind, line = worker.session_failure(impl_log)
            if kind == worker.TERMINAL:
                hint = worker.failure_hint(line)
                self.warn(f"{key}: implement session failed on a NON-RETRYABLE "
                          f"error — {line}" + (f" ({hint})" if hint else ""))
                break
            if attempt < cfg.transient_retries:
                kept = worker.keep_failed_log(impl_log, attempt + 1)
                self.warn(f"{key}: implement session left no marker "
                          f"({kind}: {line or 'no error in tail'}) — retrying"
                          + (f"; failed log kept at {kept}" if kept else ""))

        if worker.build_failed(ws):
            reason = worker.build_fail_reason(ws)
            self.warn(f"{key}: BUILD_FAIL — {reason or 'no reason recorded'}")
            self._park(key, f"{TAG}: implementation failed to reach a green "
                            f"build (BUILD_FAIL); needs a human. Reason: "
                            f"{reason or 'none recorded'} — Log: {impl_log}")
            return
        if not worker.build_ok(ws):
            committed = self.ops.has_new_commits(ws, branch, base)
            extra = (" Committed but unpushed work exists in the workspace "
                     f"({ws}); a human should look before it is discarded."
                     if committed else "")
            self._transient_requeue(
                key, kind == worker.TRANSIENT,
                f"{TAG}: worker session ended on a transient API error "
                f"({{n}}/{{cap}}), not a build failure — re-queued for "
                f"automatic retry, no human action needed.",
                f"{TAG}: implement sessions ended without BUILD_OK or "
                f"BUILD_FAIL; needs a human.{self._failure_detail(line)}{extra}"
                f" Log: {impl_log}")
            return

        # ---- Freshness ----
        # Siblings merge while a session runs; a branch cut before that merge
        # would open a CONFLICTING PR. Rebase now, while nothing is pushed and
        # no PR exists, and judge the base guard against the new base.
        fresh = self.bring_up_to_date(key, ws, branch, target)
        if fresh == CONFLICT:
            if self.ops.push(key, branch):
                self.log(f"{key}: pushed {branch} as it is, for a human")
            self._park(key, f"{TAG}: the implementation on `{branch}` conflicts "
                            f"with `{target}`, which moved on while it was being "
                            f"written, and automatic resolution did not "
                            f"succeed. The rebase was aborted and the branch is "
                            f"pushed as it was; no PR was opened. Resolve "
                            f"manually: `git rebase origin/{target}` on "
                            f"`{branch}`, then re-push and open the PR. Needs a "
                            f"human.")
            return
        if fresh != FRESH:
            base = self.ops.remote_sha(ws, target) or base
            self.log(f"{key}: rebased {branch} onto {target} before the PR ({fresh})")
        if fresh == RESOLVED:
            self.tracker.comment(
                key, f"{TAG}: `{branch}` was rebased onto `{target}`, which "
                     f"moved on while this issue was being implemented; merge "
                     f"conflicts were resolved automatically. Please "
                     f"sanity-check the PR diff.")

        # ---- Push ----
        if not self.ops.push(key, branch):
            self.warn(f"{key}: git push failed")
            self._transient_requeue(
                key, True,
                f"{TAG}: git push failed ({{n}}/{{cap}}), probably a network "
                f"blip — re-queued for automatic retry.",
                f"{TAG}: git push kept failing; needs a human. The work is "
                f"committed in {ws} on {branch}.")
            return
        self.log(f"{key}: pushed {branch}")

        # ---- Branch-base guard ----
        if not self.ops.pr_base_ok(ws, branch, target, base):
            retarget = None if explicit else self.ops.pr_retarget_candidate(
                ws, branch, target, base)
            if retarget:
                self.tracker.comment(
                    key, f"{TAG}: `{branch}` is not cleanly based on `{target}` "
                         f"(the default target — this issue names no branch), "
                         f"but it IS cleanly based on `{retarget}`, which shows "
                         f"only this issue's own commits. The PR targets "
                         f"`{retarget}`. Add a `## Target branch` heading to "
                         f"future issues on {full} to state this explicitly.")
                target = retarget
                self.state.set(key, "target", target)
            else:
                shown = self.ops.commits_shown(ws, branch, target)
                foreign = self.ops.pr_foreign_count(ws, target, branch)
                self._park(key, f"{TAG}: refusing to open a PR — `{branch}` is "
                                f"not cleanly based on `{target}`, so the PR "
                                f"would show {shown or '?'} commits and "
                                f"{foreign if foreign >= 0 else '?'} of them are "
                                f"already on another branch (other people's "
                                f"work). No other branch is an unambiguous "
                                f"destination either. The implementation is "
                                f"pushed to `{branch}`; a human should re-target "
                                f"the PR or rebase the branch. Needs a human.")
                return

        # ---- Open the PR (draft until the checks are green) ----
        existing = self.gh.find_pr(full, branch, "OPEN")
        if existing:
            _, pr, pr_url = existing
            self.log(f"{key}: reusing open PR #{pr}")
        else:
            created = self.gh.create_pr(full, branch, target,
                                        f"{summary} (#{number})"
                                        if full.casefold() == f"{owner}/{repo}".casefold()
                                        else f"{key}: {summary}",
                                        self._pr_description(key, full, ws),
                                        draft=cfg.draft_prs)
            if not created:
                self._park(key, f"{TAG}: implementation pushed to `{branch}` but "
                                f"the pull request could not be created "
                                f"(`gh api repos/{full}/pulls` failed); needs a "
                                f"human to open it against `{target}`.")
                return
            pr, pr_url = created
            self.log(f"{key}: opened PR #{pr} {pr_url}")
        self.state.set(key, "pr", pr)
        self.tracker.set_status(key, cfg.st_review,
                                f"{TAG}: pull request opened: {pr_url}")

        # ---- Verify checks ----
        res = self.verify_checks(key, full, branch, pr)
        if res == PUSH_FAILED:
            self._transient_requeue(
                key, True,
                f"{TAG}: re-push after a CI fix failed ({{n}}/{{cap}}) — "
                f"re-queued for automatic retry.",
                f"{TAG}: re-push kept failing; needs a human. PR #{pr}.")
            return
        if res == PIPELINE_FAILED:
            self.state.set(key, "state", "failed")
            self._park(key, f"{TAG}: the GitHub checks on PR #{pr} stayed red "
                            f"after {cfg.fix_cap} automatic fix attempts (or "
                            f"never finished). The PR is left as a draft; needs "
                            f"a human. Logs: "
                            f"{self._log_path(key, 'pipelinefix-*')}",
                       back_to_ready=False)
            return
        if cfg.draft_prs:
            info = self.gh.pr_info(full, pr)
            if info.get("draft"):
                if self.gh.mark_ready(info.get("node_id", "")):
                    self.log(f"{key}: PR #{pr} marked ready for review")
                else:
                    self.warn(f"{key}: could not mark PR #{pr} ready for review")

        # ---- Phase 2: review ----
        review_log = self._log_path(key, "review")
        self.log(f"{key}: review session")
        review_err = self._run_worker_retrying(
            ws, f"review {key}",
            worker.review_prompt(cfg, key, number, pr, full, branch, target),
            review_log)
        count = self.gh.pr_comment_count(full, pr)
        if count == 0:
            self.warn(f"{key}: review posted no comments — PR unreviewed")
            self.tracker.comment(
                key, f"{TAG}: PR #{pr} is open but the automated review pass "
                     f"posted no comments (worker sessions kept dying) — the PR "
                     f"is UNREVIEWED. A human should review it."
                     f"{self._failure_detail(review_err)} Log: {review_log}")
            self.state.set(key, "state", "pr_open")
            return

        # ---- Phase 2b: collaudo (findings become PR comments) ----
        self.run_collaudo(key, number, pr, full, ws, branch)

        # ---- Phase 3: address ----
        green = True   # checks on the head that will be handed over / merged
        worker.clear_markers(ws)
        worker.write_pr_comments(ws, self.gh.pr_comments_md(full, pr))
        address_log = self._log_path(key, "address")
        self.log(f"{key}: address-comments session")
        address_err = self._run_worker_retrying(
            ws, f"address {key}",
            worker.address_prompt(cfg, key, number, pr, full, branch),
            address_log)
        if worker.build_ok(ws):
            res = self.push_and_verify(key, full, branch, pr)
            green = res == OK
            self.log(f"{key}: address pass pushed ({res})")
            if res == PIPELINE_FAILED:
                self.tracker.comment(
                    key, f"{TAG}: the review fixes on PR #{pr} did not go green "
                         f"(checks red or BUILD_FAIL); a human should look. "
                         f"Log: {address_log}")
        elif address_err:
            self.tracker.comment(
                key, f"{TAG}: PR #{pr} was reviewed but the address pass never "
                     f"completed, so the review comments are UNADDRESSED and a "
                     f"human has to work through them."
                     f"{self._failure_detail(address_err)} Log: {address_log}")
            self.state.set(key, "state", "pr_open")
            return
        else:
            self.log(f"{key}: address pass made no code change")

        # ---- Freshness, again ----
        # Review, collaudo and address take long enough for a sibling to
        # merge in the meantime. A PR handed over as "ready" must be
        # mergeable at that moment, not as of when it was opened.
        fresh = self.bring_up_to_date(key, ws, branch, target)
        if fresh == CONFLICT:
            self.tracker.comment(
                key, f"{TAG}: PR #{pr} conflicts with `{target}`, which moved "
                     f"on while it was under review, and automatic resolution "
                     f"did not succeed. The rebase was aborted (branch left "
                     f"untouched). Resolve manually: `git rebase "
                     f"origin/{target}` on `{branch}`, then re-push. Needs a "
                     f"human.")
            self.tracker.label_needs_human(key)
            self.state.set(key, "state", "pr_open")
            return
        if fresh != FRESH:
            if not self.ops.force_push(ws, branch):
                self.tracker.comment(
                    key, f"{TAG}: `{branch}` was rebased onto `{target}` but "
                         f"the push failed; PR #{pr} is stale until a human "
                         f"pushes the rebased branch from {ws}.")
                self.tracker.label_needs_human(key)
                self.state.set(key, "state", "pr_open")
                return
            self.log(f"{key}: rebased {branch} onto {target} before the "
                     f"hand-off ({fresh}) — verifying checks again")
            res = self.verify_checks(key, full, branch, pr)
            green = res == OK
            if res != OK:
                self.tracker.comment(
                    key, f"{TAG}: PR #{pr} was rebased onto `{target}` but the "
                         f"checks did not go green afterwards; a human should "
                         f"look.")
            if fresh == RESOLVED:
                self.tracker.comment(
                    key, f"{TAG}: rebased `{branch}` onto `{target}` after a "
                         f"sibling merged; merge conflicts were resolved "
                         f"automatically. Please sanity-check the PR diff.")
        if cfg.auto_merge:
            if not green:
                self.tracker.comment(
                    key, f"{TAG}: auto-merge skipped — the checks on PR #{pr} "
                         f"are not green. Ready for a human to look at.")
            elif self.try_auto_merge(key, full, pr):
                return
            self.state.set(key, "state", "pr_open")
            self.log(f"{key}: pipeline complete — PR #{pr} awaits a human merge")
            return
        self.tracker.comment(key, f"{TAG}: review + fixes done; PR #{pr} is "
                                  f"ready for a human to merge.")
        self.state.set(key, "state", "pr_open")
        self.log(f"{key}: pipeline complete — PR #{pr} awaits a human merge")

    # -----------------------------------------------------------------------
    # Auto-merge
    # -----------------------------------------------------------------------
    MERGEABLE_STATES = ("clean", "has_hooks")
    MERGEABILITY_POLLS = 10

    def _merge_declined(self, key: str, pr: str, why: str) -> None:
        self.warn(f"{key}: auto-merge declined — {why}")
        self.tracker.comment(
            key, f"{TAG}: auto-merge declined — {why}. PR #{pr} is left open; "
                 f"ready for a human to merge.")

    def try_auto_merge(self, key: str, full: str, pr: str) -> bool:
        """Merge PR #pr now that the address pass ran and the checks are
        green (the caller vouches for both). Every remaining doubt — a review
        thread nobody answered, GitHub not calling the PR clean, the merge
        API refusing — hands the PR to a human instead of forcing it. On
        success the issue is closed here; the sibling rebase is left to the
        poller (detect_merges) because this runs in a child process and no
        two processes may touch one workspace."""
        cfg = self.cfg
        unanswered = self.gh.unanswered_review_threads(full, pr)
        if unanswered < 0:
            self._merge_declined(key, pr, "the review threads could not be read")
            return False
        if unanswered:
            self._merge_declined(
                key, pr, f"{unanswered} review thread(s) got neither a fix "
                         f"nor a reply")
            return False
        info: dict = {}
        for _ in range(self.MERGEABILITY_POLLS):
            info = self.gh.pr_info(full, pr)
            if not info or info.get("mergeable_state") != "unknown":
                break
            self.sleep(cfg.checks_poll_wait)   # GitHub computes it after a push
        if not info or info.get("state") != "OPEN":
            self._merge_declined(key, pr, "the PR is not open any more")
            return False
        if info.get("draft"):
            self._merge_declined(key, pr, "the PR is still a draft")
            return False
        mstate = info.get("mergeable_state", "unknown")
        if mstate not in self.MERGEABLE_STATES:
            self._merge_declined(
                key, pr, f"GitHub reports the PR as `{mstate}`, not `clean` "
                         f"(branch protection, a conflict or a required "
                         f"review can cause this)")
            return False
        ok, detail = self.gh.merge_pr(full, pr, cfg.merge_method)
        if not ok:
            self._merge_declined(key, pr, f"GitHub refused the merge: {detail}")
            return False
        self.log(f"{key}: PR #{pr} auto-merged ({cfg.merge_method})")
        self.state.set(key, "rebase_pending", "1")
        self._close_merged(
            key, pr, f"{TAG}: review comments addressed and checks green — "
                     f"PR #{pr} merged automatically ({cfg.merge_method}). "
                     f"Closing.")
        return True

    def run_collaudo(self, key: str, number: int, pr: str, full: str,
                     ws: str, branch: str) -> None:
        """Acceptance run of the open PR against the locally running app.
        Nothing here can block the pipeline: a collaudo that could not run
        leaves a comment saying a human should run it by hand."""
        cfg = self.cfg
        if not cfg.collaudo:
            return
        if not collaudo_mod.repo_applicable(cfg, full):
            self.log(f"{key}: collaudo skipped — {full} not in RALPH_COLLAUDO_REPOS")
            return
        ok, why = collaudo_mod.availability(cfg)
        if not ok:
            self.log(f"{key}: collaudo skipped — {why}")
            self.tracker.comment(
                key, f"{TAG}: PR #{pr} was NOT collaudato locally — {why}. A "
                     f"human should run the acceptance test by hand.")
            return
        worker.clear_collaudo_markers(ws)
        logf = self._log_path(key, "collaudo")
        self.log(f"{key}: collaudo session ({cfg.collaudo_agent}, browser="
                 f"{cfg.collaudo_browser})")
        err = self._run_worker_retrying(
            ws, f"collaudo {key}",
            worker.collaudo_prompt(cfg, key, number, pr, full, branch), logf,
            runner=self.collaudo_runner)
        summary = worker.collaudo_summary(ws)
        if worker.collaudo_ok(ws):
            self.log(f"{key}: collaudo complete — {summary}")
            return
        self.warn(f"{key}: collaudo did not run to completion"
                  + (f": {summary}" if summary else ""))
        detail = f" {summary}" if summary else self._failure_detail(err)
        self.tracker.comment(
            key, f"{TAG}: PR #{pr} is open and reviewed, but the automatic "
                 f"local collaudo did not run to completion, so the PR is "
                 f"untested against the running app.{detail} A human should "
                 f"collaudare it manually. Log: {logf}")

    def _prepare(self, key: str, url: str, branch: str, target: str,
                 explicit: bool):
        prep = self.ops.prepare_workspace(key, url, branch, target, explicit)
        if prep is None:
            return None
        if prep.corrected_from:
            self.warn(f"{key}: default target '{prep.corrected_from}' is a "
                      f"stub of '{prep.target}' — using '{prep.target}'")
            self.tracker.comment(
                key, f"{TAG}: the default branch `{prep.corrected_from}` is a "
                     f"stale stub of `{prep.target}` in this repo, so the work "
                     f"is based on `{prep.target}` and the PR will target it. "
                     f"Add a `## Target branch` heading to future issues on "
                     f"this repo to say so explicitly.")
        self.state.set(key, "base", prep.base)
        self.state.set(key, "target", prep.target)
        return prep

    # -----------------------------------------------------------------------
    # PR state truth
    # -----------------------------------------------------------------------
    def verify_pr_open(self, key: str, full: str, branch: str) -> bool:
        """Is the PR the state calls open really open on GitHub? A merged one
        closes the ticket instead; a vanished one is left alone."""
        if self.gh.find_pr(full, branch, "OPEN"):
            return True
        merged = self.gh.find_pr(full, branch, "MERGED")
        if merged:
            self._close_merged(key, merged[1])
            return False
        self.warn(f"{key}: no open or merged PR found for {branch} in {full}")
        return False

    def _close_merged(self, key: str, pr: str, msg: str = "") -> None:
        self.state.set(key, "state", "done")
        self.state.set(key, "pr", pr)
        self.tracker.close_done(key, self.cfg.st_done,
                                msg or f"{TAG}: PR #{pr} merged. Closing.")
        self.log(f"{key}: PR #{pr} merged — done")

    def detect_merges(self) -> None:
        merged_pairs = []
        for key in self.state.owned_keys():
            st = self.state.state_of(key)
            if st == "done" and self.state.get(key, "rebase_pending"):
                # auto-merged by a child pipeline; it left the sibling
                # rebase to us so only the poller touches idle workspaces
                self.state.set(key, "rebase_pending", "")
                merged_pairs.append((key, self.state.get(key, "repo")))
                continue
            if st != "pr_open":
                continue
            full = self.state.get(key, "repo")
            branch = self.state.get(key, "branch")
            if not full or not branch:
                continue
            m = self.gh.find_pr(full, branch, "MERGED")
            if m:
                self._close_merged(key, m[1])
                merged_pairs.append((key, full))
        for merged_key, full in merged_pairs:
            self.rebase_sibling_prs(merged_key, full)

    def rebase_sibling_prs(self, merged_key: str, full: str) -> None:
        """After a merge, bring the loop's other open PRs on the same repo up
        to date so their diffs stay honest and their checks re-run."""
        for key in self.state.owned_keys():
            if key == merged_key or self.state.state_of(key) != "pr_open":
                continue
            if self.state.get(key, "repo") != full:
                continue
            if not self.ops.workspace_exists(key):
                continue
            branch = self.state.get(key, "branch")
            target = self.state.get(key, "target")
            if not branch or not target:
                continue
            if not self.verify_pr_open(key, full, branch):
                continue
            ws = self.ops.workspace(key)
            self.ops.fetch(ws)
            if not self.ops.checkout(ws, branch):
                continue
            fresh = self.bring_up_to_date(key, ws, branch, target)
            if fresh == FRESH:
                continue
            if fresh != CONFLICT:
                if not self.ops.force_push(ws, branch):
                    self.warn(f"{key}: rebase clean but push failed")
                    continue
                self.log(f"{key}: rebased + force-pushed after {merged_key} merged")
                if fresh == RESOLVED:
                    self.tracker.comment(
                        key, f"{TAG}: rebased `{branch}` onto `{target}` after "
                             f"{merged_key} merged; merge conflicts were resolved "
                             f"automatically. Please sanity-check the PR diff.")
                continue
            self.tracker.comment(
                key, f"{TAG}: this issue's PR conflicts with `{target}` after "
                     f"{merged_key} merged, and automatic resolution did not "
                     f"succeed. The rebase was aborted (branch left untouched). "
                     f"Resolve manually: `git rebase origin/{target}` on "
                     f"`{branch}`, then re-push.")
            self.tracker.label_needs_human(key)

    def sync_done_from_prs(self, frontier_data: dict) -> None:
        """Items whose PR merged while no loop was tracking them (a human
        merged between runs): close them so dependants unblock."""
        cfg = self.cfg
        from .text import st_is
        for c in frontier_data.get("children", []):
            key = c.get("key", "")
            if not key or self.state.state_of(key) == "done":
                continue
            if not (st_is(c.get("status"), cfg.st_inprogress)
                    or st_is(c.get("status"), cfg.st_review)):
                continue
            full = self.state.get(key, "repo") or c.get("targetRepo", "")
            branch = self.state.get(key, "branch") or branch_for(key, full)
            if not full:
                continue
            m = self.gh.find_pr(full, branch, "MERGED")
            if m:
                self.state.mark_project(key)
                self._close_merged(key, m[1])

    # -----------------------------------------------------------------------
    # Resync — the issue moved under an open PR
    # -----------------------------------------------------------------------
    def resync_stale_prs(self, slots_free: Optional[Callable[[], bool]] = None
                         ) -> None:
        for key in self.state.owned_keys():
            if self.state.state_of(key) != "pr_open":
                continue
            if not self.ops.workspace_exists(key):
                continue
            ws = self.ops.workspace(key)
            branch = self.state.get(key, "branch")
            head = self.ops.head_author_epoch(ws, branch)
            if not head:
                continue
            upd = self.tracker.updated_epoch(key)
            if not upd or upd <= head + self.cfg.resync_grace:
                continue
            trigger = upd
            reason = "the issue was edited after the branch's last commit"
            if trigger <= int(self.state.get(key, "resync_at") or 0):
                continue
            full = self.state.get(key, "repo")
            if not self.verify_pr_open(key, full, branch):
                continue
            count = int(self.state.get(key, "resync_count") or 0)
            pr = self.state.get(key, "pr")
            if count >= self.cfg.resync_cap:
                self.tracker.comment(
                    key, f"{TAG}: {reason}, and this PR has already been "
                         f"resynced {count} time(s) (cap {self.cfg.resync_cap}). "
                         f"A human should check PR #{pr} against the current "
                         f"acceptance criteria.")
                self.state.set(key, "resync_at", trigger)
                continue
            if slots_free is not None and not slots_free():
                self.log(f"{key}: resync deferred — no free slot")
                continue
            self.run_resync(key, trigger, reason)

    def run_resync(self, key: str, trigger: int, reason: str) -> None:
        ws = self.ops.workspace(key)
        full = self.state.get(key, "repo")
        branch = self.state.get(key, "branch")
        pr = self.state.get(key, "pr")
        _, _, number = split_key(key)
        count = int(self.state.get(key, "resync_count") or 0)
        self.state.set(key, "state", "resync")
        self.state.set(key, "started", int(self.now()))
        self.state.set(key, "resync_at", trigger)
        self.state.set(key, "resync_count", count + 1)
        self.dump_ticket_context(key, ws)
        worker.clear_markers(ws)
        logf = self._log_path(key, f"resync-{count + 1}")
        self.log(f"{key}: resync session ({reason})")
        self.runner.run(ws, f"resync {key}",
                        worker.resync_prompt(self.cfg, key, number, pr, full,
                                             branch, reason), logf)
        if worker.build_ok(ws):
            res = self.push_and_verify(key, full, branch, pr)
            if res == OK:
                self.tracker.comment(
                    key, f"{TAG}: {reason}, so the loop ran a catch-up pass over "
                         f"`{branch}` and pushed the missing work to PR #{pr}. "
                         f"Log: {logf}")
            elif res == PUSH_FAILED:
                self.warn(f"{key}: resync push failed — will retry next poll")
                self.state.set(key, "resync_at", 0)
                self.state.set(key, "resync_count", count)
            else:
                self.tracker.comment(
                    key, f"{TAG}: tried to resync `{branch}` with the updated "
                         f"issue, but the build or the checks did not go green; "
                         f"needs a human. Log: {logf}")
                self.tracker.label_needs_human(key)
        else:
            self.log(f"{key}: resync made no code change")
        self.state.set(key, "state", "pr_open")

    # -----------------------------------------------------------------------
    # Launch plan
    # -----------------------------------------------------------------------
    def launch_plan(self, frontier_data: dict, slots: int) -> List[dict]:
        out = []
        for c in frontier_data.get("children", []):
            if len(out) >= slots:
                break
            if not c.get("ready"):
                continue
            if self.state.state_of(c["key"]) in ACTIVE_OR_FINISHED:
                continue
            out.append(c)
        return out
