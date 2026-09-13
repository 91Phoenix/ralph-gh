"""Seam: ralph_gh.frontier — readiness rule, blockers, idle reasons."""

from ralph_gh.config import Config
from ralph_gh.frontier import fetch, idle_reasons, is_ready, siblings_md


def item(n, **kw):
    d = {"key": f"o/r#{n}", "item_id": f"I{n}", "owner": "o", "repo": "r",
         "number": n, "summary": f"S{n}", "body": "", "state": "OPEN",
         "url": "", "updated": 1, "labels": ["ready-for-agent"],
         "assignees": [], "status": "Ready"}
    d.update(kw)
    return d


class FakeGH:
    def __init__(self, deps=None, states=None):
        self.deps = deps or {}
        self.states = states or {}
        self.dep_calls = []

    def issue_blockers(self, key):
        self.dep_calls.append(key)
        return self.deps.get(key, [])

    def issue_state(self, key):
        return self.states.get(key, "OPEN")


class FakeTracker:
    def __init__(self, items, gh=None):
        self._items = items
        self.gh = gh or FakeGH()

    def items(self):
        return self._items


CFG = Config.from_env("o/1", env={})


class TestIsReady:
    def test_ready(self):
        assert is_ready(item(1), CFG)

    def test_closed_not_ready(self):
        assert not is_ready(item(1, state="CLOSED"), CFG)

    def test_status_not_ready(self):
        assert not is_ready(item(1, status="In Progress"), CFG)

    def test_status_alias(self):
        assert is_ready(item(1, status="To Do"), CFG)

    def test_label_required_unless_blank(self):
        assert not is_ready(item(1, labels=[]), CFG)
        cfg = Config.from_env("o/1", env={"AGENT_LABEL": ""})
        assert is_ready(item(1, labels=[]), cfg)

    def test_needs_human_blocks(self):
        assert not is_ready(item(1, labels=["ready-for-agent", "needs-human"]), CFG)

    def test_assigned_blocks(self):
        assert not is_ready(item(1, assignees=["me"]), CFG)

    def test_open_blockers_block(self):
        assert not is_ready(item(1, blockedByOpen=["o/r#2"]), CFG)


class TestFetch:
    def test_native_and_prose_blockers_merge(self):
        gh = FakeGH(deps={"o/r#1": [("o/r#2", "OPEN")]}, states={"x/y#9": "CLOSED"})
        t = FakeTracker([item(1, body="## Blocked by\n#3 https://github.com/x/y/issues/9"),
                         item(2), item(3, state="CLOSED", status="Done")], gh)
        data = fetch(t, CFG)
        c = {x["key"]: x for x in data["children"]}
        assert c["o/r#1"]["blockedBy"] == ["o/r#2", "o/r#3", "x/y#9"]
        assert c["o/r#1"]["blockedByOpen"] == ["o/r#2"]
        assert not c["o/r#1"]["ready"]
        assert c["o/r#2"]["ready"]
        # closed items never cost a dependencies call
        assert "o/r#3" not in gh.dep_calls

    def test_repo_and_branch_from_body(self):
        t = FakeTracker([item(1, body="## Repository\nacme/app\n## Target branch\ndev")])
        c = fetch(t, CFG)["children"][0]
        assert c["targetRepo"] == "acme/app" and c["targetBranch"] == "dev"
        assert c["repo"] == "r"

    def test_default_repo_is_the_issues_own(self):
        c = fetch(FakeTracker([item(1)]), CFG)["children"][0]
        assert c["targetRepo"] == "o/r" and c["targetBranch"] == ""

    def test_sorted_by_repo_then_number(self):
        t = FakeTracker([item(3), item(1), item(2, repo="a", key="o/a#2")])
        assert [c["key"] for c in fetch(t, CFG)["children"]] == ["o/a#2", "o/r#1", "o/r#3"]


class TestIdleReasons:
    def test_empty_when_something_ready(self):
        data = {"children": [item(1, ready=True), item(2, ready=False, status="X")]}
        assert idle_reasons(data, CFG) == []

    def test_reasons_per_item(self):
        data = {"children": [
            item(1, ready=False, status="In Progress", assignees=["me"]),
            item(2, ready=False, labels=[]),
            item(3, ready=False, labels=["needs-human"]),
            item(4, ready=False, blockedByOpen=["o/r#1"]),
            item(5, ready=False, state="CLOSED"),
            item(6, ready=False, status="Done")]}
        out = idle_reasons(data, CFG)
        assert out == ["idle: o/r#1 — status=In Progress; assigned:me",
                       "idle: o/r#2 — no ready-for-agent label",
                       "idle: o/r#3 — needs-human",
                       "idle: o/r#4 — blocked-by o/r#1"]


def test_siblings_md():
    data = {"children": [item(1), item(2, status="")]}
    assert siblings_md(data) == "- o/r#1 (Ready) — S1\n- o/r#2 (OPEN) — S2\n"
    assert siblings_md({"children": []}) == ""
