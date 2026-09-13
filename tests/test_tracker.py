"""Seam: ralph_gh.tracker — the Project-backed tracker facade."""

from ralph_gh.github import GhError, ProjectInfo
from ralph_gh.tracker import Tracker


class FakeGitHub:
    def __init__(self):
        self.project = ProjectInfo(id="P", title="T", url="U", owner_kind="users",
                                   status_field_id="F",
                                   status_options={"Todo": "o1", "In Progress": "o2",
                                                   "Done": "o3"})
        self.items_data = [{"key": "o/r#1", "item_id": "I1"}]
        self.calls = []
        self.state = "OPEN"
        self.fail_assign = False

    def resolve_project(self, kind, owner, number):
        return self.project

    def project_items(self, pid):
        self.calls.append(("items", pid))
        return self.items_data

    def viewer_login(self):
        return "me"

    def set_item_status(self, pid, item, field, option):
        self.calls.append(("status", item, option))

    def comment(self, key, body):
        self.calls.append(("comment", key, body))

    def assign(self, key, login):
        if self.fail_assign:
            raise GhError("nope")
        self.calls.append(("assign", key, login))

    def unassign(self, key, login):
        self.calls.append(("unassign", key, login))

    def add_labels(self, key, labels):
        self.calls.append(("labels", key, tuple(labels)))

    def issue_state(self, key):
        return self.state

    def close_issue(self, key):
        self.calls.append(("close", key))

    def issue_updated_epoch(self, key):
        return 42

    def issue(self, key):
        return {"title": "T", "html_url": "u", "state": "open",
                "labels": [{"name": "a"}], "assignees": [{"login": "me"}],
                "body": "## Repository\nx/y\n"}


def tracker():
    gh = FakeGitHub()
    t = Tracker(gh, "users", "o", 1, log=lambda m: None)
    return t, gh


class TestStatus:
    def test_option_matching_is_alias_aware(self):
        t, _ = tracker()
        assert t.option_for("Ready|Todo") == "Todo"
        assert t.option_for("In Review") is None

    def test_set_status_uses_item_map(self):
        t, gh = tracker()
        t.items()
        assert t.set_status("o/r#1", "todo", "hi")
        assert ("status", "I1", "o1") in gh.calls
        assert ("comment", "o/r#1", "hi") in gh.calls

    def test_set_status_refreshes_empty_map(self):
        t, gh = tracker()
        assert t.set_status("o/r#1", "Done")
        assert ("items", "P") in gh.calls and ("status", "I1", "o3") in gh.calls

    def test_unknown_item_or_column_is_false_not_crash(self):
        t, gh = tracker()
        assert not t.set_status("o/r#9", "Done")
        assert not t.set_status("o/r#1", "In Review")
        assert not any(c[0] == "status" for c in gh.calls)

    def test_on_write_fires(self):
        t, _ = tracker()
        seen = []
        t.on_write = seen.append
        t.comment("o/r#1", "x")
        t.set_status("o/r#1", "Done")
        t.label_needs_human("o/r#1")
        assert seen == ["o/r#1"] * 3


class TestHigherLevel:
    def test_claim(self):
        t, gh = tracker()
        t.claim("o/r#1", "In Progress")
        assert ("assign", "o/r#1", "me") in gh.calls
        assert ("status", "I1", "o2") in gh.calls
        assert any(c[0] == "comment" and "claimed" in c[2] for c in gh.calls)

    def test_claim_survives_assign_failure(self):
        t, gh = tracker()
        gh.fail_assign = True
        t.claim("o/r#1", "In Progress")
        assert ("status", "I1", "o2") in gh.calls

    def test_requeue_unassigns(self):
        t, gh = tracker()
        t.requeue("o/r#1", "Todo", "back")
        assert ("unassign", "o/r#1", "me") in gh.calls
        assert ("status", "I1", "o1") in gh.calls

    def test_label_needs_human(self):
        t, gh = tracker()
        t.label_needs_human("o/r#1")
        assert ("labels", "o/r#1", ("needs-human",)) in gh.calls

    def test_close_done_closes_open_issue_only(self):
        t, gh = tracker()
        t.close_done("o/r#1", "Done", "merged")
        assert ("close", "o/r#1") in gh.calls
        gh.calls.clear(); gh.state = "CLOSED"
        t.close_done("o/r#1", "Done")
        assert ("close", "o/r#1") not in gh.calls

    def test_issue_md(self):
        t, _ = tracker()
        md = t.issue_md("o/r#1")
        assert md.startswith("# o/r#1: T")
        assert "Labels: a" in md and "## Repository" in md
