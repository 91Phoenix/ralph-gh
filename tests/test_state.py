"""Seam: ralph_gh.state — per-ticket flat-file state, project scoping."""

from ralph_gh.state import StateStore, decode_key, encode_key


def store(tmp_path, ref="octo/3"):
    return StateStore(str(tmp_path), project_ref=ref)


class TestKeyEncoding:
    def test_roundtrip(self):
        k = "acme/app#12"
        assert encode_key(k) == "acme__app--12"
        assert decode_key(encode_key(k)) == k
        assert "/" not in encode_key(k) and "#" not in encode_key(k)


class TestStateStore:
    def test_get_missing_is_empty(self, tmp_path):
        assert store(tmp_path).get("a/b#1", "state") == ""

    def test_set_then_get_and_reload(self, tmp_path):
        store(tmp_path).set("a/b#1", "pr", "42")
        assert store(tmp_path).get("a/b#1", "pr") == "42"

    def test_ownership(self, tmp_path):
        s = store(tmp_path)
        s.set("a/b#1", "state", "running")
        assert not s.owned_by_project("a/b#1")
        s.mark_project("a/b#1")
        assert s.owned_by_project("a/b#1")
        assert not store(tmp_path, "other/9").owned_by_project("a/b#1")

    def test_owned_keys_filters_foreign(self, tmp_path):
        s = store(tmp_path)
        s.set("a/b#1", "state", "pr_open"); s.mark_project("a/b#1")
        s.set("a/b#2", "state", "pr_open")
        store(tmp_path, "x/1").set("a/b#2", "project", "x/1")
        assert s.owned_keys() == ["a/b#1"]

    def test_adopt_only_unowned_with_state(self, tmp_path):
        s = store(tmp_path)
        s.set("a/b#1", "state", "pr_open")            # unowned, adopted
        store(tmp_path, "x/1").set("a/b#2", "state", "pr_open")
        store(tmp_path, "x/1").mark_project("a/b#2")   # foreign, kept
        s.adopt_project_items(["a/b#1", "a/b#2", "a/b#3", ""])
        assert s.owned_by_project("a/b#1")
        assert not s.owned_by_project("a/b#2")
        assert s.get("a/b#3", "project") == ""

    def test_count_running(self, tmp_path):
        s = store(tmp_path)
        s.set("a/b#1", "pid", "10"); s.mark_project("a/b#1")
        s.set("a/b#2", "pid", "20"); s.mark_project("a/b#2")
        s.set("a/b#3", "pid", "30")   # foreign
        assert s.count_running(lambda p: p in ("10", "30")) == 1
