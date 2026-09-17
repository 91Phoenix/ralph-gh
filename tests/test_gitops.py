"""Seam: ralph_gh.gitops — workspace lifecycle against a local bare origin."""

import os

import pytest

from ralph_gh.config import Config
from ralph_gh.gitops import EXCLUDED_ARTIFACTS, GitOps
from tests.test_gitrepo import ENV, commit, sh

KEY = "o/r#1"
BRANCH = "feature/issue-1"


@pytest.fixture()
def origin(tmp_path):
    seed = str(tmp_path / "seed")
    os.makedirs(seed)
    sh(seed, "init", "-q", "-b", "main")
    commit(seed, "A")
    sh(seed, "checkout", "-q", "-b", "development")
    commit(seed, "B")
    bare = str(tmp_path / "origin.git")
    sh(seed, "clone", "-q", "--bare", seed, bare)
    return bare


@pytest.fixture()
def ops(tmp_path, monkeypatch):
    # GitOps runs git with the process environment; a CI runner has no
    # global identity, and a rebase creates commits.
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
              "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, ENV[k])
    cfg = Config.from_env("o/1", env={"RALPH_WORKSPACES": str(tmp_path / "w")})
    return GitOps(cfg)


class TestPrepare:
    def test_cuts_branch_from_target(self, ops, origin):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "development", explicit=True)
        assert prep and prep.target == "development" and prep.corrected_from == ""
        assert sh(prep.ws, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
        assert prep.base == sh(prep.ws, "rev-parse", "origin/development")

    def test_default_stub_target_corrected(self, ops, origin):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "main", explicit=False)
        assert prep.target == "development" and prep.corrected_from == "main"

    def test_explicit_target_never_corrected(self, ops, origin):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "main", explicit=True)
        assert prep.target == "main"

    def test_missing_target_is_none(self, ops, origin):
        assert ops.prepare_workspace(KEY, origin, BRANCH, "nope", explicit=True) is None

    def test_reprepare_discards_previous_work(self, ops, origin):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "development", True)
        commit(prep.ws, "junk")
        again = ops.prepare_workspace(KEY, origin, BRANCH, "development", True)
        assert again.base == prep.base
        assert sh(again.ws, "rev-parse", "HEAD") == prep.base

    def test_excludes_written_once(self, ops, origin):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "development", True)
        ops.ensure_excludes(prep.ws); ops.ensure_excludes(prep.ws)
        with open(os.path.join(prep.ws, ".git", "info", "exclude")) as f:
            lines = [l.strip() for l in f if l.strip() in EXCLUDED_ARTIFACTS]
        assert sorted(lines) == sorted(EXCLUDED_ARTIFACTS)


class TestPublishAndInspect:
    def test_push_and_head_helpers(self, ops, origin):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "development", True)
        assert not ops.has_new_commits(prep.ws, BRANCH, prep.base)
        sha = commit(prep.ws, "C")
        assert ops.has_new_commits(prep.ws, BRANCH, prep.base)
        assert ops.push(KEY, BRANCH)
        assert sh(prep.ws, "ls-remote", "origin", BRANCH).startswith(sha)
        assert ops.head_sha(prep.ws, BRANCH) == sha
        assert ops.commits_shown(prep.ws, BRANCH, "development") == "1"
        assert ops.head_author_epoch(prep.ws, BRANCH) > 0
        assert ops.pr_base_ok(prep.ws, BRANCH, "development", prep.base)

    def test_behind_and_rebase(self, ops, origin, tmp_path):
        prep = ops.prepare_workspace(KEY, origin, BRANCH, "development", True)
        commit(prep.ws, "C")
        other = str(tmp_path / "other")
        sh(prep.ws, "clone", "-q", "-b", "development", origin, other)
        commit(other, "D")
        sh(other, "push", "-q", "origin", "development")
        assert ops.fetch(prep.ws)
        assert ops.behind_target(prep.ws, BRANCH, "development")
        assert ops.rebase_onto(prep.ws, "development")
        assert not ops.behind_target(prep.ws, BRANCH, "development")
        assert ops.rebase_finished_clean(prep.ws)
        # After the rebase the branch's base is the target's tip, and the
        # guard holds against THAT base, not the pre-rebase one.
        new_base = ops.remote_sha(prep.ws, "development")
        assert new_base and new_base != prep.base
        assert ops.pr_base_ok(prep.ws, BRANCH, "development", new_base)
