"""Flat-file state per ticket, one value per file, so a restart picks up
where the previous run left off. Files (KEY is the ticket key
"owner/repo#123", encoded for the filesystem):

    <STATE_DIR>/<KEY>.state     running|resync|pr_open|done|failed|requeued
    <STATE_DIR>/<KEY>.pid       pipeline subprocess pid
    <STATE_DIR>/<KEY>.pr        pull request number
    <STATE_DIR>/<KEY>.repo      owner/repo the ticket is implemented in
    <STATE_DIR>/<KEY>.base      commit the feature branch was cut from
    <STATE_DIR>/<KEY>.target    branch the work is based on / PR destination
    <STATE_DIR>/<KEY>.started   epoch
    <STATE_DIR>/<KEY>.project   project that launched the work (ownership)
    <STATE_DIR>/<KEY>.resync_at / .resync_count / .transient_deaths
"""

import os
from typing import Callable, Iterable, List

_SEP_SLASH = "__"
_SEP_HASH = "--"


def encode_key(key: str) -> str:
    """"owner/repo#12" -> "owner__repo--12": safe as a file or directory name."""
    return key.replace("/", _SEP_SLASH).replace("#", _SEP_HASH)


def decode_key(name: str) -> str:
    return name.replace(_SEP_HASH, "#").replace(_SEP_SLASH, "/")


class StateStore:
    def __init__(self, state_dir: str, project_ref: str):
        self.state_dir = state_dir
        self.project_ref = project_ref
        os.makedirs(state_dir, exist_ok=True)

    def _path(self, key: str, attr: str) -> str:
        return os.path.join(self.state_dir, f"{encode_key(key)}.{attr}")

    def set(self, key: str, attr: str, value) -> None:
        with open(self._path(key, attr), "w") as f:
            f.write(str(value))

    def get(self, key: str, attr: str) -> str:
        try:
            with open(self._path(key, attr)) as f:
                return f.read().strip()
        except OSError:
            return ""

    def state_of(self, key: str) -> str:
        return self.get(key, "state")

    def mark_project(self, key: str) -> None:
        self.set(key, "project", self.project_ref)

    def owned_by_project(self, key: str) -> bool:
        """State entries are scoped to the project that launched them; the
        state dir outlives runs and is shared by every project ever looped
        on the machine."""
        return self.get(key, "project") == self.project_ref

    def adopt_project_items(self, keys: Iterable[str]) -> None:
        """Stamp unowned state entries that belong to this project's items.
        Only keys that already have a .state file and no .project marker are
        adopted; everything else keeps its owner."""
        for key in keys:
            if not key:
                continue
            if not os.path.exists(self._path(key, "state")):
                continue
            if not os.path.exists(self._path(key, "project")):
                self.mark_project(key)

    def owned_keys(self) -> List[str]:
        """Keys with a .state file owned by this project."""
        out = []
        for name in sorted(os.listdir(self.state_dir)):
            if not name.endswith(".state"):
                continue
            key = decode_key(name[: -len(".state")])
            if self.owned_by_project(key):
                out.append(key)
        return out

    def count_running(self, pid_alive: Callable[[str], bool]) -> int:
        n = 0
        for name in os.listdir(self.state_dir):
            if not name.endswith(".pid"):
                continue
            key = decode_key(name[: -len(".pid")])
            if not self.owned_by_project(key):
                continue
            pid = self.get(key, "pid")
            if pid and pid_alive(pid):
                n += 1
        return n
