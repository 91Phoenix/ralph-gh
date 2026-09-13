"""Seam: ralph_gh.gitrepo — branch-base invariants on REAL git repos."""

import os
import subprocess

import pytest

from ralph_gh import gitrepo

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
       "GIT_CONFIG_NOSYSTEM": "1", "HOME": os.environ.get("HOME", "/tmp")}


def sh(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       env=ENV)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(cwd, name):
    with open(os.path.join(cwd, name), "w") as f:
        f.write(name)
    sh(cwd, "add", name)
    sh(cwd, "commit", "-q", "-m", name)
    return sh(cwd, "rev-parse", "HEAD")


BRANCH = "feature/issue-1"


@pytest.fixture()
def repos(tmp_path):
    """origin: main (A) is a stale stub of development (A,B); ws has
    feature/issue-1 cut from development with one commit C."""
    seed = str(tmp_path / "seed")
    os.makedirs(seed)
    sh(seed, "init", "-q", "-b", "main")
    commit(seed, "A")
    sh(seed, "checkout", "-q", "-b", "development")
    base = commit(seed, "B")
    sh(seed, "checkout", "-q", "main")
    origin = str(tmp_path / "origin.git")
    sh(seed, "clone", "-q", "--bare", seed, origin)
    ws = str(tmp_path / "ws")
    sh(seed, "clone", "-q", origin, ws)
    sh(ws, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    sh(ws, "checkout", "-q", "-B", BRANCH, "origin/development")
    commit(ws, "C")
    return {"origin": origin, "ws": ws, "base": base, "tmp": str(tmp_path)}


class TestInferTrunk:
    def test_stub_main_corrected_to_development(self, repos):
        assert gitrepo.infer_trunk(repos["ws"], "main", "development main") == "development"

    def test_real_trunk_kept(self, repos):
        assert gitrepo.infer_trunk(repos["ws"], "development", "development main") == "development"

    def test_missing_target_passthrough(self, repos):
        assert gitrepo.infer_trunk(repos["ws"], "nope", "development") == "nope"


class TestForeignCommits:
    def test_against_stub_main_counts_development_commit(self, repos):
        assert gitrepo.pr_foreign_count(repos["ws"], "main", BRANCH) == 1

    def test_against_development_is_clean(self, repos):
        assert gitrepo.pr_foreign_count(repos["ws"], "development", BRANCH) == 0

    def test_uncountable_fails_closed(self, repos):
        assert gitrepo.pr_foreign_count(repos["ws"], "nope", BRANCH) == -1


class TestPrBaseOk:
    def test_ok_on_true_base(self, repos):
        assert gitrepo.pr_base_ok(repos["ws"], BRANCH, "development", repos["base"])

    def test_not_ok_on_stub_target(self, repos):
        assert not gitrepo.pr_base_ok(repos["ws"], BRANCH, "main", repos["base"])

    def test_empty_base_not_ok(self, repos):
        assert not gitrepo.pr_base_ok(repos["ws"], BRANCH, "development", "")


class TestRetargetCandidate:
    def test_finds_development(self, repos):
        assert gitrepo.pr_retarget_candidate(repos["ws"], BRANCH, "main",
                                             repos["base"]) == "development"

    def test_topic_branches_never_candidates(self, repos):
        # feature/other = development + D: by ancestry alone it would be a
        # (tighter) candidate; the topic-branch filter must skip it.
        ws = repos["ws"]
        other = os.path.join(repos["tmp"], "other")
        sh(ws, "clone", "-q", "-b", "development", repos["origin"], other)
        commit(other, "D")
        sh(other, "push", "-q", "origin", "HEAD:refs/heads/feature/other")
        sh(ws, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
        assert gitrepo.pr_base_ok(ws, BRANCH, "feature/other", repos["base"])
        assert gitrepo.pr_retarget_candidate(ws, BRANCH, "main",
                                             repos["base"]) == "development"

    def test_same_commit_on_another_branch_is_foreign(self, repos):
        ws = repos["ws"]
        sh(ws, "push", "-q", "origin", f"{BRANCH}:refs/heads/feature/other")
        sh(ws, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
        assert gitrepo.pr_foreign_count(ws, "development", BRANCH) == 1
        assert gitrepo.pr_retarget_candidate(ws, BRANCH, "main", repos["base"]) is None


class TestRebaseFinishedClean:
    def test_clean(self, repos):
        assert gitrepo.rebase_finished_clean(repos["ws"])

    def test_in_progress_marker(self, repos):
        os.makedirs(os.path.join(repos["ws"], ".git", "rebase-merge"))
        assert not gitrepo.rebase_finished_clean(repos["ws"])


class TestRemoteUrl:
    def test_https_default(self):
        assert gitrepo.remote_url("o/r", env={}) == "https://github.com/o/r.git"

    def test_ssh(self):
        assert gitrepo.remote_url("o/r", "ssh", env={}) == "git@github.com:o/r.git"

    def test_detect_protocol_order_and_explicit(self):
        tried = []
        assert gitrepo.detect_protocol("o/r", env={}, probe=lambda u: (tried.append(u), False)[1]) is None
        assert tried == ["https://github.com/o/r.git", "git@github.com:o/r.git"]
        assert gitrepo.detect_protocol("o/r", env={"RALPH_GIT_PROTOCOL": "ssh"},
                                       probe=lambda u: False) == "ssh"
