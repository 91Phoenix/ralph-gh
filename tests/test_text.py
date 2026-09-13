"""Seam: ralph_gh.text — pure parsing of issue bodies and repo references."""

from ralph_gh.text import (branch_field, branch_for, desc_field, iso_epoch,
                           key_from_url, make_key, parse_repo, prose_blockers,
                           repo_field, split_key, st_is)


class TestStIs:
    def test_case_insensitive_alias_list(self):
        assert st_is("ready", "Ready|Todo")
        assert st_is("TO DO", "Ready|To Do")
        assert not st_is("Done", "Ready|Todo")
        assert not st_is("", "Ready")
        assert not st_is("Ready", "")


class TestParseRepo:
    def test_bare_name_gets_default_owner(self):
        assert parse_repo("app", "octo") == "octo/app"

    def test_owner_repo_passthrough(self):
        assert parse_repo("acme/app", "octo") == "acme/app"
        assert parse_repo("acme/app.git", "octo") == "acme/app"

    def test_urls(self):
        assert parse_repo("https://github.com/acme/app", "o") == "acme/app"
        assert parse_repo("https://github.com/acme/app.git/", "o") == "acme/app"
        assert parse_repo("https://github.com/acme/app/issues/3", "o") == "acme/app"
        assert parse_repo("git@github.com:acme/app.git", "o") == "acme/app"

    def test_none_and_empty(self):
        assert parse_repo("none", "o") == ""
        assert parse_repo("N/A", "o") == ""
        assert parse_repo("", "o") == ""
        assert parse_repo(None, "o") == ""


class TestKeys:
    def test_roundtrip(self):
        assert split_key(make_key("acme", "app", 12)) == ("acme", "app", 12)

    def test_bad_key(self):
        import pytest
        with pytest.raises(ValueError):
            split_key("nope")

    def test_key_from_url(self):
        assert key_from_url("https://github.com/acme/app/issues/12") == "acme/app#12"
        assert key_from_url("https://github.com/acme/app/pull/4") == "acme/app#4"
        assert key_from_url("https://example.com/x") == ""

    def test_branch_same_repo(self):
        assert branch_for("acme/app#12", "acme/app") == "feature/issue-12"
        assert branch_for("acme/app#12", "ACME/App") == "feature/issue-12"

    def test_branch_cross_repo_carries_issue_repo(self):
        assert branch_for("acme/plan#12", "acme/app") == "feature/plan-12"


BODY = """Some intro that mentions Repository: not-a-field in prose.

## Repository
acme/app — trunk development

## Target branch: release/1.2

### Blocked by
- #3
- acme/other#9 and https://github.com/x/y/issues/11

## Acceptance criteria
- [ ] thing
"""


class TestHeadingFields:
    def test_prose_is_not_a_field(self):
        assert repo_field("Repository: acme/app in a sentence") == ""

    def test_repo_next_line(self):
        assert repo_field(BODY) == "acme/app"

    def test_repo_inline(self):
        assert repo_field("## Repository: `acme/app`") == "acme/app"

    def test_repo_bullet_value(self):
        assert repo_field("## Repository\n\n- acme/app\n") == "acme/app"

    def test_none_reads_absent(self):
        assert repo_field("## Repository\nnone") == ""

    def test_target_branch_heading_wins_over_trunk(self):
        assert branch_field(BODY) == "release/1.2"

    def test_trunk_on_repository_line(self):
        assert branch_field("## Repository\nacme/app — trunk development") == "development"
        assert branch_field("## Repository\nacme/app trunk: `main`") == "main"

    def test_no_branch(self):
        assert branch_field("## Repository\nacme/app") == ""

    def test_desc_field_stops_at_next_heading(self):
        assert desc_field("## Repository\n\n## Other\nacme/app", "repository") == ""


class TestProseBlockers:
    def test_all_forms(self):
        assert prose_blockers(BODY, "acme", "app") == [
            "acme/app#3", "acme/other#9", "x/y#11"]

    def test_none_section(self):
        assert prose_blockers("## Blocked by\nnone\n", "a", "b") == []

    def test_absent(self):
        assert prose_blockers("no section #4", "a", "b") == []

    def test_bounded_by_next_heading(self):
        txt = "## Blocked by\n#1\n## Notes\n#2"
        assert prose_blockers(txt, "a", "b") == ["a/b#1"]


class TestIsoEpoch:
    def test_zulu(self):
        assert iso_epoch("1970-01-01T00:01:00Z") == 60

    def test_offset(self):
        assert iso_epoch("1970-01-01T01:00:00+01:00") == 0

    def test_bad(self):
        assert iso_epoch("nope") is None
        assert iso_epoch(None) is None
