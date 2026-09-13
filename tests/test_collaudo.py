"""Seam: ralph_gh.collaudo — the gate in front of the acceptance run."""

import subprocess

from ralph_gh import collaudo
from ralph_gh.config import Config


def cfg(**env):
    base = {"RALPH_COLLAUDO": "1"}
    base.update(env)
    return Config.from_env("o/1", env=base)


class TestRepoApplicable:
    def test_empty_list_means_all(self):
        assert collaudo.repo_applicable(cfg(), "o/app")

    def test_name_or_full_match(self):
        c = cfg(RALPH_COLLAUDO_REPOS="app other/x")
        assert collaudo.repo_applicable(c, "o/app")
        assert collaudo.repo_applicable(c, "other/x")
        assert not collaudo.repo_applicable(c, "o/x")


class TestAvailability:
    def test_disabled(self):
        ok, why = collaudo.availability(cfg(RALPH_COLLAUDO="0"))
        assert not ok and "RALPH_COLLAUDO=0" in why

    def test_agent_binary_missing(self, monkeypatch):
        monkeypatch.setattr(collaudo.shutil, "which", lambda t: None)
        ok, why = collaudo.availability(cfg())
        assert not ok and "not on PATH" in why and "claude" in why

    def test_npx_required_for_playwright(self, monkeypatch):
        monkeypatch.setattr(collaudo.shutil, "which", lambda t: None if t == "npx" else "/bin/x")
        ok, why = collaudo.availability(cfg())
        assert not ok and "npx" in why
        ok, _ = collaudo.availability(cfg(RALPH_COLLAUDO_BROWSER="none"))
        assert ok

    def test_probe_decides(self, monkeypatch):
        monkeypatch.setattr(collaudo.shutil, "which", lambda t: "/bin/x")
        calls = []

        def run(cmd, shell, capture_output, timeout):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 7)
        ok, why = collaudo.availability(cfg(RALPH_COLLAUDO_PROBE="curl -f http://x"), run=run)
        assert not ok and "exited 7" in why and calls == ["curl -f http://x"]
        ok, _ = collaudo.availability(
            cfg(RALPH_COLLAUDO_PROBE="true"),
            run=lambda *a, **k: subprocess.CompletedProcess(a, 0))
        assert ok

    def test_probe_exception_is_a_reason(self, monkeypatch):
        monkeypatch.setattr(collaudo.shutil, "which", lambda t: "/bin/x")

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("x", 1)
        ok, why = collaudo.availability(cfg(RALPH_COLLAUDO_PROBE="sleep 99"), run=boom)
        assert not ok and "TimeoutExpired" in why

    def test_no_probe_is_available(self, monkeypatch):
        monkeypatch.setattr(collaudo.shutil, "which", lambda t: "/bin/x")
        assert collaudo.availability(cfg()) == (True, "")
