"""Seam: ralph_gh.orchestrator — the per-ticket pipeline and the poller's
bookkeeping, driven through fakes of every collaborator."""

import os

import pytest

from ralph_gh import worker
from ralph_gh.config import Config
from ralph_gh.gitops import PreparedWorkspace
from ralph_gh.orchestrator import (OK, PIPELINE_FAILED, PUSH_FAILED,
                                   Orchestrator)
from ralph_gh.state import StateStore, encode_key

KEY = "o/r#1"
FULL = "o/r"
BRANCH = "feature/issue-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeTracker:
    def __init__(self):
        self.comments = []      # (key, text)
        self.statuses = []      # (key, spec)
        self.labels = []        # key
        self.claims = []
        self.requeues = []      # (key, spec)
        self.closed = []        # (key, spec)
        self.updated = {}       # key -> epoch
        self.items_remembered = {}
        self.on_write = None

    def _wrote(self, key):
        if self.on_write:
            self.on_write(key)

    def remember_item(self, key, item_id):
        self.items_remembered[key] = item_id

    def items(self):
        return []

    def option_for(self, spec):
        return spec.split("|")[0]

    def viewer(self):
        return "me"

    def updated_epoch(self, key):
        return self.updated.get(key)

    def issue_md(self, key):
        return f"# {key}: ticket\n\nbody\n"

    def comment(self, key, text):
        self.comments.append((key, text))
        self._wrote(key)

    def set_status(self, key, spec, comment=""):
        self.statuses.append((key, spec))
        if comment:
            self.comment(key, comment)
        else:
            self._wrote(key)
        return True

    def claim(self, key, st):
        self.claims.append(key)
        self.set_status(key, st, "claimed")

    def label_needs_human(self, key):
        self.labels.append(key)
        self._wrote(key)

    def requeue(self, key, st, comment=""):
        self.requeues.append((key, st))
        self.set_status(key, st, comment)

    def close_done(self, key, st, comment=""):
        self.closed.append((key, st))
        self.set_status(key, st, comment)


class FakeGh:
    def __init__(self):
        self.reachable = True
        self.default = "main"
        self.prs = {}               # (branch, state) -> tuple or [tuples]
        self.created = []           # (full, head, base, title, body, draft)
        self.create_result = ("7", "https://gh/pr/7")
        self.checks = []            # scripted statuses, popped per poll
        self.comment_counts = [2]
        self.infos = {}             # pr -> dict
        self.ready = []
        self.comments_md = "# comments\n"

    def repo_reachable(self, full):
        return self.reachable

    def default_branch(self, full):
        return self.default

    def find_pr(self, full, branch, state):
        v = self.prs.get((branch, state))
        if isinstance(v, list):
            return v.pop(0) if v else None
        return v

    def create_pr(self, full, head, base, title, body, draft=True):
        self.created.append((full, head, base, title, body, draft))
        return self.create_result

    def pr_info(self, full, number):
        return self.infos.get(number, {"draft": True, "node_id": "N" + number})

    def mark_ready(self, node_id):
        self.ready.append(node_id)
        return True

    def pr_comment_count(self, full, number):
        return self.comment_counts.pop(0) if len(self.comment_counts) > 1 \
            else self.comment_counts[0]

    def pr_comments_md(self, full, number):
        return self.comments_md

    def checks_status(self, full, sha):
        return self.checks.pop(0) if self.checks else "NONE"


class FakeOps:
    def __init__(self, base):
        self.base = base
        self.proto = "https"
        self.prepare_ok = True
        self.corrected_from = ""
        self.base_ok = True
        self.retarget = None
        self.foreign = 0
        self.push_results = []      # scripted, popped; default True
        self.pushes = 0
        self.events = []
        self.behind = True
        self.rebase_ok = True
        self.rebase_clean_after_worker = False
        self.head_epoch = 1000
        self.committed_work = False
        self.exists = True

    def detect_protocol(self, full):
        return self.proto

    def workspace(self, key):
        ws = os.path.join(self.base, encode_key(key))
        os.makedirs(ws, exist_ok=True)
        return ws

    def workspace_exists(self, key):
        return self.exists

    def prepare_workspace(self, key, url, branch, target, explicit):
        if not self.prepare_ok:
            return None
        return PreparedWorkspace(ws=self.workspace(key), target=target,
                                 base="base-sha")

    def ensure_excludes(self, ws):
        pass

    def push(self, key, branch):
        self.pushes += 1
        return self.push_results.pop(0) if self.push_results else True

    def force_push(self, ws, branch):
        self.events.append(("force_push", branch))
        return True

    def fetch(self, ws):
        return True

    def head_sha(self, ws, branch):
        return "sha"

    def checkout(self, ws, branch):
        return True

    def behind_target(self, ws, branch, target):
        return self.behind

    def rebase_onto(self, ws, target):
        self.events.append(("rebase", target))
        return self.rebase_ok

    def abort_rebase(self, ws):
        self.events.append(("abort", ws))

    def rebase_finished_clean(self, ws):
        return self.rebase_clean_after_worker

    def pr_base_ok(self, ws, branch, target, base):
        return self.base_ok

    def pr_retarget_candidate(self, ws, branch, current, base):
        return self.retarget

    def pr_foreign_count(self, ws, target, branch):
        return self.foreign

    def commits_shown(self, ws, branch, target):
        return "5"

    def has_new_commits(self, ws, branch, base):
        return self.committed_work

    def head_author_epoch(self, ws, branch):
        return self.head_epoch


class FakeRunner:
    def __init__(self):
        self.sessions = []      # titles in order
        self.behavior = {}      # title prefix -> callable(ws, logf)

    def run(self, ws, title, prompt, logf):
        self.sessions.append(title)
        with open(logf, "w") as f:
            f.write("done\n")
        for prefix, fn in self.behavior.items():
            if title.startswith(prefix):
                fn(ws, logf)
        return 0


def write_marker(name, content="ok"):
    def fn(ws, logf):
        with open(os.path.join(ws, name), "w") as f:
            f.write(content)
    return fn


def write_impl_output(body="## What this does\nstuff\n"):
    def fn(ws, logf):
        write_marker("BUILD_OK", "npm test")(ws, logf)
        with open(os.path.join(ws, worker.PR_BODY), "w") as f:
            f.write(body)
    return fn


def no_marker(err_line=""):
    def fn(ws, logf):
        if err_line:
            with open(logf, "a") as f:
                f.write(err_line + "\n")
    return fn


def seq(*fns):
    """Different behaviour per attempt."""
    it = list(fns)

    def fn(ws, logf):
        (it.pop(0) if len(it) > 1 else it[0])(ws, logf)
    return fn


@pytest.fixture()
def env(tmp_path):
    cfg = Config.from_env("o/1", env={"RALPH_WORKSPACES": str(tmp_path / "w"),
                                       "CHECKS_POLL_WAIT": "0",
                                       "POLL_SECONDS": "0"})
    os.makedirs(cfg.state_dir, exist_ok=True)
    tracker, gh, runner = FakeTracker(), FakeGh(), FakeRunner()
    ops = FakeOps(str(tmp_path / "w"))
    state = StateStore(cfg.state_dir, cfg.project_ref)
    orch = Orchestrator(cfg, tracker, gh, ops, state, runner,
                        log=lambda m: None, sleep=lambda s: None,
                        now=lambda: 5000.0)
    runner.behavior["implement"] = write_impl_output()
    return orch, tracker, gh, ops, state, runner


def run(orch, branchval=""):
    orch.run_pipeline(KEY, "Do the thing", FULL, branchval)


def comments(tracker):
    return " || ".join(t for _, t in tracker.comments)


# ---------------------------------------------------------------------------
# Checks gate
# ---------------------------------------------------------------------------
class TestVerifyChecks:
    def test_green(self, env):
        orch, _, gh, ops, _, _ = env
        gh.checks = ["PENDING", "SUCCESS"]
        assert orch.verify_checks(KEY, FULL, BRANCH, "7") == OK

    def test_no_ci_trusts_local_gate(self, env):
        orch, _, gh, _, _, runner = env
        gh.checks = ["NONE"] * 5
        assert orch.verify_checks(KEY, FULL, BRANCH, "7") == OK
        assert runner.sessions == []

    def test_failed_fixed_and_repushed(self, env):
        orch, _, gh, ops, _, runner = env
        gh.checks = ["FAILED", "PENDING", "SUCCESS"]
        runner.behavior["pipeline-fix"] = write_marker("BUILD_OK")
        assert orch.verify_checks(KEY, FULL, BRANCH, "7") == OK
        assert runner.sessions == [f"pipeline-fix {KEY}"] and ops.pushes == 1

    def test_fix_cap_exhausted(self, env):
        orch, _, gh, _, _, runner = env
        gh.checks = ["FAILED"] * 10
        runner.behavior["pipeline-fix"] = write_marker("BUILD_OK")
        assert orch.verify_checks(KEY, FULL, BRANCH, "7") == PIPELINE_FAILED
        assert len(runner.sessions) == 2

    def test_fix_session_not_green_stops(self, env):
        orch, _, gh, ops, _, runner = env
        gh.checks = ["FAILED"] * 10
        runner.behavior["pipeline-fix"] = write_marker("BUILD_FAIL", "nope")
        assert orch.verify_checks(KEY, FULL, BRANCH, "7") == PIPELINE_FAILED
        assert ops.pushes == 0

    def test_repush_failure(self, env):
        orch, _, gh, ops, _, runner = env
        gh.checks = ["FAILED"]
        runner.behavior["pipeline-fix"] = write_marker("BUILD_OK")
        ops.push_results = [False]
        assert orch.verify_checks(KEY, FULL, BRANCH, "7") == PUSH_FAILED

    def test_push_and_verify_needs_build_ok(self, env):
        orch, _, _, ops, _, _ = env
        assert orch.push_and_verify(KEY, FULL, BRANCH, "7") == PIPELINE_FAILED
        assert ops.pushes == 0


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
class TestRunPipeline:
    def test_happy_path(self, env):
        orch, tracker, gh, ops, state, runner = env
        run(orch)
        assert tracker.claims == [KEY]
        assert runner.sessions == [f"implement {KEY}", f"review {KEY}", f"address {KEY}"]
        assert len(gh.created) == 1
        full, head, base, title, body, draft = gh.created[0]
        assert (full, head, base, draft) == (FULL, BRANCH, "main", True)
        assert title == "Do the thing (#1)"
        assert "Closes #1" in body and "## What this does" in body
        assert gh.ready == ["N7"]
        assert state.state_of(KEY) == "pr_open" and state.get(KEY, "pr") == "7"
        assert state.get(KEY, "branch") == BRANCH and state.get(KEY, "repo") == FULL
        assert ("o/r#1", "In Review|Review") in tracker.statuses
        assert os.path.exists(os.path.join(ops.workspace(KEY), worker.TICKET))
        assert os.path.exists(os.path.join(ops.workspace(KEY), worker.PR_COMMENTS))
        assert "ready for a human to merge" in comments(tracker)
        assert tracker.labels == []

    def test_cross_repo_issue(self, env):
        orch, _, gh, _, state, _ = env
        orch.run_pipeline("o/plan#4", "Cross", FULL, "")
        _, head, _, title, body, _ = gh.created[0]
        assert head == "feature/plan-4" and title == "o/plan#4: Cross"
        assert "Closes o/plan#4" in body

    def test_missing_pr_body_says_so(self, env):
        orch, _, gh, _, _, runner = env
        runner.behavior["implement"] = write_marker("BUILD_OK")
        run(orch)
        assert "left no `.ralph-pr-body.md`" in gh.created[0][4]

    def test_non_draft_mode(self, env):
        orch, _, gh, _, _, _ = env
        orch.cfg = Config.from_env("o/1", env={"RALPH_WORKSPACES": orch.cfg.workspace_base,
                                                "RALPH_DRAFT_PRS": "0", "CHECKS_POLL_WAIT": "0"})
        run(orch)
        assert gh.created[0][5] is False and gh.ready == []

    def test_unreachable_repo_parks_without_claim(self, env):
        orch, tracker, gh, _, state, _ = env
        gh.reachable = False
        run(orch)
        assert state.state_of(KEY) == "failed" and tracker.labels == [KEY]
        assert tracker.claims == []

    def test_no_git_access_parks(self, env):
        orch, tracker, _, ops, state, _ = env
        ops.proto = None
        run(orch)
        assert state.state_of(KEY) == "failed" and "gh auth setup-git" in comments(tracker)

    def test_no_default_branch_parks(self, env):
        orch, tracker, gh, _, state, _ = env
        gh.default = ""
        run(orch)
        assert state.state_of(KEY) == "failed" and "TARGET_BRANCH" in comments(tracker)

    def test_workspace_failure_parks_back_to_ready(self, env):
        orch, tracker, _, ops, state, runner = env
        ops.prepare_ok = False
        run(orch)
        assert state.state_of(KEY) == "failed"
        assert ("o/r#1", orch.cfg.st_ready) in tracker.statuses
        assert runner.sessions == []

    def test_explicit_branch_from_ticket(self, env):
        orch, _, gh, _, state, _ = env
        run(orch, "release/1")
        assert gh.created[0][2] == "release/1" and state.get(KEY, "target") == "release/1"

    def test_build_fail_parks_with_reason(self, env):
        orch, tracker, gh, _, state, runner = env
        runner.behavior["implement"] = write_marker("BUILD_FAIL", "tests red\nbecause")
        run(orch)
        assert state.state_of(KEY) == "failed" and tracker.labels == [KEY]
        assert "tests red because" in comments(tracker)
        assert gh.created == [] and len(runner.sessions) == 1

    def test_transient_death_requeues(self, env):
        orch, tracker, _, _, state, runner = env
        runner.behavior["implement"] = no_marker("API Error: 529 overloaded")
        run(orch)
        assert runner.sessions == [f"implement {KEY}"] * 2
        assert state.state_of(KEY) == "requeued" and tracker.requeues == [(KEY, orch.cfg.st_ready)]
        assert tracker.labels == [] and state.get(KEY, "transient_deaths") == "1"

    def test_transient_recovers_on_retry(self, env):
        orch, _, gh, _, state, runner = env
        runner.behavior["implement"] = seq(no_marker("fetch failed"), write_impl_output())
        run(orch)
        assert state.state_of(KEY) == "pr_open" and len(gh.created) == 1

    def test_persistent_transient_parks_after_cap(self, env):
        orch, tracker, _, _, state, runner = env
        state.set(KEY, "transient_deaths", "2")
        runner.behavior["implement"] = no_marker("API Error: 503")
        run(orch)
        assert state.state_of(KEY) == "failed" and tracker.labels == [KEY]

    def test_terminal_implement_failure_not_retried(self, env):
        orch, tracker, _, _, state, runner = env
        runner.behavior["implement"] = no_marker("API Error: 401 authentication_error")
        run(orch)
        assert runner.sessions == [f"implement {KEY}"]
        assert state.state_of(KEY) == "failed" and "authentication_error" in comments(tracker)

    def test_no_marker_names_committed_work(self, env):
        orch, tracker, _, ops, state, runner = env
        runner.behavior["implement"] = no_marker()
        ops.committed_work = True
        run(orch)
        assert state.state_of(KEY) == "failed" and "unpushed work" in comments(tracker)

    def test_push_failure_requeues(self, env):
        orch, tracker, gh, ops, state, _ = env
        ops.push_results = [False]
        run(orch)
        assert state.state_of(KEY) == "requeued" and gh.created == []
        assert "push failed" in comments(tracker)

    def test_existing_open_pr_reused(self, env):
        orch, _, gh, _, state, _ = env
        gh.prs[(BRANCH, "OPEN")] = ("OPEN", "3", "u3")
        run(orch)
        assert gh.created == [] and state.get(KEY, "pr") == "3"

    def test_dirty_base_retargets_default_target(self, env):
        orch, tracker, gh, ops, state, _ = env
        ops.base_ok = False
        ops.retarget = "development"
        run(orch)
        assert gh.created[0][2] == "development" and state.get(KEY, "target") == "development"
        assert "## Target branch" in comments(tracker) and tracker.labels == []

    def test_dirty_base_without_candidate_parks(self, env):
        orch, tracker, gh, ops, state, _ = env
        ops.base_ok = False
        ops.foreign = 62
        run(orch)
        assert gh.created == [] and state.state_of(KEY) == "failed"
        assert "62 of them" in comments(tracker)

    def test_explicit_target_never_retargeted(self, env):
        orch, _, gh, ops, state, _ = env
        ops.base_ok = False
        ops.retarget = "development"
        run(orch, "main")
        assert gh.created == [] and state.state_of(KEY) == "failed"

    def test_pr_create_failure_parks(self, env):
        orch, tracker, gh, _, state, _ = env
        gh.create_result = None
        run(orch)
        assert state.state_of(KEY) == "failed" and "could not be created" in comments(tracker)

    def test_red_checks_park_and_leave_draft(self, env):
        orch, tracker, gh, _, state, runner = env
        gh.checks = ["FAILED"] * 20
        runner.behavior["pipeline-fix"] = write_marker("BUILD_OK")
        run(orch)
        assert state.state_of(KEY) == "failed" and gh.ready == []
        assert "left as a draft" in comments(tracker)
        assert f"review {KEY}" not in runner.sessions
        # not put back to Ready: the PR exists
        assert (KEY, orch.cfg.st_ready) not in tracker.statuses

    def test_zero_review_comments_reported(self, env):
        orch, tracker, gh, _, state, runner = env
        gh.comment_counts = [0]
        run(orch)
        assert "UNREVIEWED" in comments(tracker) and state.state_of(KEY) == "pr_open"
        assert f"address {KEY}" not in runner.sessions

    def test_unknown_comment_count_not_unreviewed(self, env):
        orch, tracker, gh, _, _, runner = env
        gh.comment_counts = [-1]
        run(orch)
        assert "UNREVIEWED" not in comments(tracker) and f"address {KEY}" in runner.sessions

    def test_review_transient_retried(self, env):
        orch, _, _, _, _, runner = env
        runner.behavior["review"] = seq(no_marker("API Error: 529"), no_marker())
        run(orch)
        assert runner.sessions.count(f"review {KEY}") == 2

    def test_terminal_review_not_retried_and_quoted(self, env):
        orch, tracker, gh, _, _, runner = env
        gh.comment_counts = [0]
        runner.behavior["review"] = no_marker("API Error: 403 permission_error")
        run(orch)
        assert runner.sessions.count(f"review {KEY}") == 1
        assert "permission_error" in comments(tracker)

    def test_dead_address_reported_unaddressed(self, env):
        orch, tracker, _, _, state, runner = env
        runner.behavior["address"] = no_marker("Could not resolve host")
        run(orch)
        assert "UNADDRESSED" in comments(tracker) and state.state_of(KEY) == "pr_open"
        assert "ready for a human to merge" not in comments(tracker)

    def test_address_changes_are_pushed_and_verified(self, env):
        orch, tracker, gh, ops, _, runner = env
        runner.behavior["address"] = write_marker("BUILD_OK")
        gh.checks = ["SUCCESS", "SUCCESS"]
        run(orch)
        assert ops.pushes == 2 and "ready for a human to merge" in comments(tracker)

    def test_address_red_checks_commented(self, env):
        orch, tracker, gh, _, state, runner = env
        runner.behavior["address"] = write_marker("BUILD_OK")
        runner.behavior["pipeline-fix"] = write_marker("BUILD_FAIL", "still red")
        gh.checks = ["SUCCESS"] + ["FAILED"] * 10
        run(orch)
        assert "did not go green" in comments(tracker) and state.state_of(KEY) == "pr_open"

    def test_corrected_default_target_commented(self, env):
        orch, tracker, gh, ops, _, _ = env
        ops.prepare_workspace = lambda key, url, branch, target, explicit: PreparedWorkspace(
            ws=ops.workspace(key), target="development", base="b", corrected_from=target)
        run(orch)
        assert gh.created[0][2] == "development" and "stale stub" in comments(tracker)


# ---------------------------------------------------------------------------
# Collaudo
# ---------------------------------------------------------------------------
def collaudo_env(env, monkeypatch, **extra):
    """The env fixture with the collaudo on and its gate answering yes."""
    orch, tracker, gh, ops, state, runner = env
    e = {"RALPH_WORKSPACES": orch.cfg.workspace_base, "CHECKS_POLL_WAIT": "0",
         "RALPH_COLLAUDO": "1"}
    e.update(extra)
    orch.cfg = Config.from_env("o/1", env=e)
    monkeypatch.setattr("ralph_gh.collaudo.availability", lambda cfg: (True, ""))
    return orch, tracker, gh, ops, state, runner


class TestCollaudo:
    def test_runs_between_review_and_address_with_its_own_runner(self, env, monkeypatch):
        orch, tracker, gh, ops, state, runner = collaudo_env(env, monkeypatch)
        crunner = FakeRunner()
        crunner.behavior["collaudo"] = write_marker("COLLAUDO_OK", "PASS\n- t1 ok")
        orch.collaudo_runner = crunner
        run(orch)
        assert runner.sessions == [f"implement {KEY}", f"review {KEY}", f"address {KEY}"]
        assert crunner.sessions == [f"collaudo {KEY}"]
        assert "NOT collaudato" not in comments(tracker) and "untested" not in comments(tracker)
        assert state.state_of(KEY) == "pr_open"

    def test_issues_found_is_still_a_completed_collaudo(self, env, monkeypatch):
        orch, tracker, _, _, _, runner = collaudo_env(env, monkeypatch)
        runner.behavior["collaudo"] = write_marker("COLLAUDO_OK", "ISSUES 2\n- t1 fail")
        run(orch)
        assert "untested" not in comments(tracker) and tracker.labels == []

    def test_collaudo_fail_comments_and_continues(self, env, monkeypatch):
        orch, tracker, _, _, state, runner = collaudo_env(env, monkeypatch)
        runner.behavior["collaudo"] = write_marker("COLLAUDO_FAIL", "no free slot")
        run(orch)
        assert "untested against the running app. no free slot" in comments(tracker)
        assert f"address {KEY}" in runner.sessions and state.state_of(KEY) == "pr_open"
        assert tracker.labels == []

    def test_dead_collaudo_session_retried_then_reported(self, env, monkeypatch):
        orch, tracker, _, _, _, runner = collaudo_env(env, monkeypatch)
        runner.behavior["collaudo"] = no_marker("API Error: 529")
        run(orch)
        assert runner.sessions.count(f"collaudo {KEY}") == 2
        assert "did not run to completion" in comments(tracker) and "529" in comments(tracker)

    def test_unavailable_tells_human(self, env, monkeypatch):
        orch, tracker, _, _, _, runner = collaudo_env(env, monkeypatch)
        monkeypatch.setattr("ralph_gh.collaudo.availability",
                            lambda cfg: (False, "the local app probe `curl` exited 7"))
        run(orch)
        assert f"collaudo {KEY}" not in runner.sessions
        assert "NOT collaudato locally — the local app probe" in comments(tracker)

    def test_inapplicable_repo_skips_silently(self, env, monkeypatch):
        orch, tracker, _, _, _, runner = collaudo_env(env, monkeypatch,
                                                      RALPH_COLLAUDO_REPOS="other")
        run(orch)
        assert f"collaudo {KEY}" not in runner.sessions and "collaudato" not in comments(tracker)

    def test_disabled_by_default_is_silent(self, env):
        orch, tracker, _, _, _, runner = env
        run(orch)
        assert f"collaudo {KEY}" not in runner.sessions and "collaudato" not in comments(tracker)

    def test_not_run_when_review_posted_nothing(self, env, monkeypatch):
        orch, _, gh, _, _, runner = collaudo_env(env, monkeypatch)
        gh.comment_counts = [0]
        run(orch)
        assert f"collaudo {KEY}" not in runner.sessions


# ---------------------------------------------------------------------------
# PR state truth, merges, sibling rebases
# ---------------------------------------------------------------------------
def pr_open_state(state, key, full=FULL, branch=None, target="main", pr="7"):
    state.set(key, "state", "pr_open")
    state.set(key, "repo", full)
    state.set(key, "branch", branch or f"feature/issue-{key.split('#')[1]}")
    state.set(key, "target", target)
    state.set(key, "pr", pr)
    state.mark_project(key)


class TestPrStateTruth:
    def test_verify_open(self, env):
        orch, _, gh, _, _, _ = env
        gh.prs[(BRANCH, "OPEN")] = ("OPEN", "7", "u")
        assert orch.verify_pr_open(KEY, FULL, BRANCH)

    def test_verify_merged_closes(self, env):
        orch, tracker, gh, _, state, _ = env
        pr_open_state(state, KEY)
        gh.prs[(BRANCH, "MERGED")] = ("MERGED", "7", "u")
        assert not orch.verify_pr_open(KEY, FULL, BRANCH)
        assert state.state_of(KEY) == "done" and tracker.closed == [(KEY, "Done")]

    def test_verify_vanished_leaves_state(self, env):
        orch, _, _, _, state, _ = env
        pr_open_state(state, KEY)
        assert not orch.verify_pr_open(KEY, FULL, BRANCH)
        assert state.state_of(KEY) == "pr_open"

    def test_detect_merges_then_rebases_sibling(self, env):
        orch, tracker, gh, ops, state, _ = env
        pr_open_state(state, KEY)
        pr_open_state(state, "o/r#2", pr="8")
        gh.prs[(BRANCH, "MERGED")] = ("MERGED", "7", "u")
        gh.prs[("feature/issue-2", "OPEN")] = ("OPEN", "8", "u")
        orch.detect_merges()
        assert state.state_of(KEY) == "done" and state.state_of("o/r#2") == "pr_open"
        assert ("rebase", "main") in ops.events and ("force_push", "feature/issue-2") in ops.events

    def test_sibling_not_behind_untouched(self, env):
        orch, _, gh, ops, state, _ = env
        pr_open_state(state, KEY); pr_open_state(state, "o/r#2", pr="8")
        gh.prs[(BRANCH, "MERGED")] = ("MERGED", "7", "u")
        gh.prs[("feature/issue-2", "OPEN")] = ("OPEN", "8", "u")
        ops.behind = False
        orch.detect_merges()
        assert ops.events == []

    def test_foreign_project_states_untouched(self, env):
        orch, _, gh, _, state, _ = env
        pr_open_state(state, KEY)
        state.set(KEY, "project", "other/9")
        gh.prs[(BRANCH, "MERGED")] = ("MERGED", "7", "u")
        orch.detect_merges()
        assert state.state_of(KEY) == "pr_open"

    def test_rebase_conflict_resolved_by_claude(self, env):
        orch, tracker, gh, ops, state, runner = env
        pr_open_state(state, "o/r#2", pr="8")
        gh.prs[("feature/issue-2", "OPEN")] = ("OPEN", "8", "u")
        ops.rebase_ok = False
        ops.rebase_clean_after_worker = True
        ops.behind_target = lambda ws, b, t: not ops.rebase_clean_after_worker or len(runner.sessions) == 0
        orch.rebase_sibling_prs(KEY, FULL)
        assert runner.sessions == ["rebase-resolve o/r#2"]
        assert ("force_push", "feature/issue-2") in ops.events
        assert "resolved automatically" in comments(tracker)

    def test_rebase_conflict_unresolved_aborts_and_labels(self, env):
        orch, tracker, gh, ops, state, runner = env
        pr_open_state(state, "o/r#2", pr="8")
        gh.prs[("feature/issue-2", "OPEN")] = ("OPEN", "8", "u")
        ops.rebase_ok = False
        orch.rebase_sibling_prs(KEY, FULL)
        assert any(e[0] == "abort" for e in ops.events)
        assert tracker.labels == ["o/r#2"] and "Resolve manually" in comments(tracker)

    def test_sync_done_from_prs(self, env):
        orch, tracker, gh, _, state, _ = env
        gh.prs[(BRANCH, "MERGED")] = ("MERGED", "7", "u")
        data = {"children": [{"key": KEY, "status": "In Review", "targetRepo": FULL},
                             {"key": "o/r#2", "status": "Ready", "targetRepo": FULL}]}
        orch.sync_done_from_prs(data)
        assert state.state_of(KEY) == "done" and state.owned_by_project(KEY)
        assert state.state_of("o/r#2") == ""


# ---------------------------------------------------------------------------
# Resync
# ---------------------------------------------------------------------------
class TestResync:
    def _open(self, env):
        orch, tracker, gh, ops, state, runner = env
        pr_open_state(state, KEY)
        gh.prs[(BRANCH, "OPEN")] = ("OPEN", "7", "u")
        ops.head_epoch = 1000
        return orch, tracker, gh, ops, state, runner

    def test_edit_after_branch_triggers(self, env):
        orch, tracker, gh, ops, state, runner = self._open(env)
        tracker.updated[KEY] = 5000
        runner.behavior["resync"] = write_marker("BUILD_OK")
        gh.checks = ["SUCCESS"]
        orch.resync_stale_prs()
        assert runner.sessions == [f"resync {KEY}"] and ops.pushes == 1
        assert state.get(KEY, "resync_count") == "1" and state.state_of(KEY) == "pr_open"
        assert "catch-up pass" in comments(tracker)

    def test_own_write_does_not_trigger(self, env):
        orch, tracker, _, _, state, runner = self._open(env)
        tracker.updated[KEY] = 5000
        tracker.comment(KEY, "loop wrote this")   # absorbs the watermark
        orch.resync_stale_prs()
        assert runner.sessions == []

    def test_human_edit_after_own_write_still_resyncs(self, env):
        orch, tracker, _, _, _, runner = self._open(env)
        tracker.updated[KEY] = 5000
        tracker.comment(KEY, "loop")
        tracker.updated[KEY] = 6000
        orch.resync_stale_prs()
        assert runner.sessions == [f"resync {KEY}"]

    def test_within_grace_not_resynced(self, env):
        orch, tracker, _, _, _, runner = self._open(env)
        tracker.updated[KEY] = 1000 + orch.cfg.resync_grace
        orch.resync_stale_prs()
        assert runner.sessions == []

    def test_same_edit_not_twice(self, env):
        orch, tracker, _, _, state, runner = self._open(env)
        tracker.updated[KEY] = 5000
        orch.resync_stale_prs(); orch.resync_stale_prs()
        assert runner.sessions == [f"resync {KEY}"]

    def test_cap_spent_comments_only(self, env):
        orch, tracker, _, _, state, runner = self._open(env)
        state.set(KEY, "resync_count", "2")
        tracker.updated[KEY] = 5000
        orch.resync_stale_prs()
        assert runner.sessions == [] and "cap 2" in comments(tracker)

    def test_no_slot_defers(self, env):
        orch, tracker, _, _, _, runner = self._open(env)
        tracker.updated[KEY] = 5000
        orch.resync_stale_prs(slots_free=lambda: False)
        assert runner.sessions == []

    def test_no_gap_no_push(self, env):
        orch, _, _, ops, _, runner = self._open(env)
        env[1].updated[KEY] = 5000
        orch.resync_stale_prs()
        assert runner.sessions == [f"resync {KEY}"] and ops.pushes == 0

    def test_write_to_unowned_ticket_leaves_no_state(self, env):
        orch, tracker, _, _, state, _ = env
        tracker.updated["o/r#9"] = 5
        tracker.comment("o/r#9", "x")
        assert state.get("o/r#9", "resync_at") == ""


# ---------------------------------------------------------------------------
# Launch plan
# ---------------------------------------------------------------------------
class TestLaunchPlan:
    def _data(self, *keys):
        return {"children": [{"key": k, "ready": True} for k in keys]}

    def test_skips_active_and_finished(self, env):
        orch, _, _, _, state, _ = env
        for k, st in (("o/r#1", "running"), ("o/r#2", "pr_open"),
                      ("o/r#3", "done"), ("o/r#4", "resync")):
            state.set(k, "state", st)
        plan = orch.launch_plan(self._data("o/r#1", "o/r#2", "o/r#3", "o/r#4", "o/r#5"), 5)
        assert [c["key"] for c in plan] == ["o/r#5"]

    def test_failed_and_requeued_retryable(self, env):
        orch, _, _, _, state, _ = env
        state.set("o/r#1", "state", "failed"); state.set("o/r#2", "state", "requeued")
        assert len(orch.launch_plan(self._data("o/r#1", "o/r#2"), 5)) == 2

    def test_slots_and_readiness(self, env):
        orch, _, _, _, _, _ = env
        data = self._data("o/r#1", "o/r#2", "o/r#3")
        data["children"][1]["ready"] = False
        assert [c["key"] for c in orch.launch_plan(data, 1)] == ["o/r#1"]
        assert [c["key"] for c in orch.launch_plan(data, 5)] == ["o/r#1", "o/r#3"]
