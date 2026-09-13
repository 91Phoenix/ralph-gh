"""Seam: ralph_gh.config — project reference parsing and env knobs."""

import pytest

from ralph_gh.config import (Config, parse_project_arg, resolve_target_branch,
                             target_is_explicit)


class TestParseProjectArg:
    def test_user_url(self):
        assert parse_project_arg("https://github.com/users/octo/projects/3") == \
            ("users", "octo", 3)

    def test_org_url_trailing_slash(self):
        assert parse_project_arg("https://github.com/orgs/acme/projects/12/") == \
            ("orgs", "acme", 12)

    def test_short_form(self):
        assert parse_project_arg("octo/3") == ("", "octo", 3)

    def test_garbage(self):
        with pytest.raises(ValueError):
            parse_project_arg("octo")
        with pytest.raises(ValueError):
            parse_project_arg("https://github.com/octo/repo")


class TestConfig:
    def test_defaults(self):
        c = Config.from_env("octo/3", env={})
        assert c.project_ref == "octo/3"
        assert c.default_full_repo == ""
        assert c.target_branch == ""
        assert c.max_concurrent == 2
        assert c.draft_prs is True
        assert c.state_dir.endswith("/.state")
        assert c.skill_implement == "tdd"

    def test_fallback_repo_uses_project_owner(self):
        c = Config.from_env("octo/3", "app", env={})
        assert c.default_full_repo == "octo/app"

    def test_env_overrides(self):
        c = Config.from_env("octo/3", env={"MAX_CONCURRENT": "4",
                                             "RALPH_DRAFT_PRS": "0",
                                             "RALPH_WORKSPACES": "/tmp/w",
                                             "ST_READY": "Backlog",
                                             "RALPH_MODEL": "opus"})
        assert c.max_concurrent == 4
        assert c.draft_prs is False
        assert c.workspace_base == "/tmp/w"
        assert c.st_ready == "Backlog"
        assert c.model == "opus"

    def test_bad_int_falls_back(self):
        assert Config.from_env("o/1", env={"MAX_CONCURRENT": "x"}).max_concurrent == 2


class TestTargetBranch:
    def test_resolution(self):
        assert resolve_target_branch("", "main") == "main"
        assert resolve_target_branch("refs/heads/dev", "main") == "dev"
        assert resolve_target_branch(" dev ", "main") == "dev"

    def test_explicit(self):
        assert target_is_explicit("dev")
        assert not target_is_explicit("")
        assert not target_is_explicit(None)
