"""Pin the orchestrator's collaborators to the real classes: a fake that
grows a method the real class lacks, or a call site naming a method that
does not exist, fails here instead of in production."""

import os
import re

from ralph_gh.github import GitHubClient
from ralph_gh.gitops import GitOps
from ralph_gh.tracker import Tracker
from ralph_gh.worker import WorkerRunner
from tests.test_orchestrator import FakeGh, FakeOps, FakeRunner, FakeTracker

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "ralph_gh")


def public_methods(cls):
    return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))}


def source_of(*names):
    return "\n".join(open(os.path.join(PKG, n)).read() for n in names)


class TestFakesConformToRealClasses:
    def test_fake_ops(self):
        assert public_methods(FakeOps) - public_methods(GitOps) == set()

    def test_fake_tracker(self):
        assert public_methods(FakeTracker) - public_methods(Tracker) == set()

    def test_fake_gh(self):
        assert public_methods(FakeGh) - public_methods(GitHubClient) == set()

    def test_fake_runner(self):
        assert public_methods(FakeRunner) - public_methods(WorkerRunner) == set()


class TestCallSitesExistOnRealClasses:
    SRC = source_of("orchestrator.py", "__main__.py", "launcher.py", "frontier.py")

    def _calls(self, attr):
        return set(re.findall(rf"\b(?:self|orch)\.{attr}\.(\w+)\(", self.SRC))

    def test_ops(self):
        assert self._calls("ops") - public_methods(GitOps) == set()

    def test_tracker(self):
        missing = self._calls("tracker") - public_methods(Tracker) - {"project"}
        assert missing == set()

    def test_gh(self):
        missing = self._calls("gh") - public_methods(GitHubClient) - {"gh"}
        assert missing == set()

    def test_frontier_uses_real_gh_methods(self):
        used = set(re.findall(r"tracker\.gh\.(\w+)\(", self.SRC))
        assert used - public_methods(GitHubClient) == set()
