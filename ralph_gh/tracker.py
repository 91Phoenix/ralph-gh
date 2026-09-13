"""The ticket tracker seen by the orchestrator: a GitHub Project (v2) whose
Status field is the state machine, plus the issues behind its items.

Everything the loop writes goes through here so one place can (a) skip a
status the project does not define instead of crashing, (b) fire `on_write`
so the resync watermark moves past the loop's own edits."""

from typing import Callable, Dict, List, Optional

from .github import GitHubClient, GhError, ProjectInfo
from .text import st_is


class Tracker:
    def __init__(self, gh: GitHubClient, owner_kind: str, owner: str,
                 number: int, needs_human_label: str = "needs-human",
                 log: Optional[Callable[[str], None]] = None):
        self.gh = gh
        self.owner_kind = owner_kind
        self.owner = owner
        self.number = number
        self.needs_human_label = needs_human_label
        self.log = log or (lambda m: None)
        self.on_write: Optional[Callable[[str], None]] = None
        self._project: Optional[ProjectInfo] = None
        self._items: Dict[str, str] = {}        # key -> project item id
        self._viewer: str = ""

    # -- project -------------------------------------------------------------
    @property
    def project(self) -> ProjectInfo:
        if self._project is None:
            self._project = self.gh.resolve_project(self.owner_kind, self.owner,
                                                    self.number)
        return self._project

    def items(self) -> List[dict]:
        """Fresh project items; also refreshes the key -> item-id map every
        write needs."""
        items = self.gh.project_items(self.project.id)
        self._items = {it["key"]: it["item_id"] for it in items}
        return items

    def remember_item(self, key: str, item_id: str) -> None:
        self._items[key] = item_id

    def viewer(self) -> str:
        if not self._viewer:
            self._viewer = self.gh.viewer_login()
        return self._viewer

    def _wrote(self, key: str) -> None:
        if self.on_write:
            self.on_write(key)

    def option_for(self, spec: str) -> Optional[str]:
        """The project's Status option matching a pipe-separated alias list,
        or None when the project defines no such column."""
        for name in self.project.status_options:
            if st_is(name, spec):
                return name
        return None

    # -- reads ---------------------------------------------------------------
    def updated_epoch(self, key: str) -> Optional[int]:
        return self.gh.issue_updated_epoch(key)

    def issue_md(self, key: str) -> str:
        """The ticket as the worker reads it."""
        d = self.gh.issue(key)
        labels = ", ".join(l.get("name", "") for l in d.get("labels", []) or [])
        assignees = ", ".join("@" + a.get("login", "")
                              for a in d.get("assignees", []) or [])
        head = [f"# {key}: {d.get('title', '')}", "",
                f"URL: {d.get('html_url', '')}",
                f"State: {(d.get('state') or '').upper()}",
                f"Labels: {labels}",
                f"Assignees: {assignees}", ""]
        return "\n".join(head) + (d.get("body") or "").rstrip() + "\n"

    # -- writes --------------------------------------------------------------
    def comment(self, key: str, text: str) -> None:
        self.gh.comment(key, text)
        self._wrote(key)

    def set_status(self, key: str, spec: str, comment: str = "") -> bool:
        """Move the item's Status to the option matching `spec`. False (and
        a log line) when the item is not in the project or the project has
        no such column; a missing column is a setup problem, not a crash."""
        ok = False
        item = self._items.get(key)
        if not item:
            # A child process starts with an empty map; one refresh is cheap.
            try:
                self.items()
            except GhError:
                pass
            item = self._items.get(key)
        name = self.option_for(spec)
        if not item:
            self.log(f"WARN: {key}: not an item of the project — status "
                     f"'{spec}' not set")
        elif not name:
            self.log(f"WARN: project has no Status option matching '{spec}' "
                     f"(has: {', '.join(self.project.status_options) or 'none'})")
        else:
            try:
                self.gh.set_item_status(self.project.id, item,
                                        self.project.status_field_id,
                                        self.project.status_options[name])
                ok = True
            except GhError as e:
                self.log(f"WARN: {key}: status '{name}' not set: {e}")
        if comment:
            self.comment(key, comment)
        else:
            self._wrote(key)
        return ok

    def claim(self, key: str, st_inprogress: str) -> None:
        try:
            self.gh.assign(key, self.viewer())
        except GhError as e:
            self.log(f"WARN: {key}: could not assign: {e}")
        self.set_status(key, st_inprogress, "ralph-gh: claimed, implementing.")

    def label_needs_human(self, key: str) -> None:
        try:
            self.gh.add_labels(key, [self.needs_human_label])
        except GhError as e:
            self.log(f"WARN: {key}: could not label: {e}")
        self._wrote(key)

    def requeue(self, key: str, st_ready: str, comment: str = "") -> None:
        """Back to the frontier: unassigned, Ready, labels untouched."""
        try:
            self.gh.unassign(key, self.viewer())
        except GhError as e:
            self.log(f"WARN: {key}: could not unassign: {e}")
        self.set_status(key, st_ready, comment)

    def close_done(self, key: str, st_done: str, comment: str = "") -> None:
        """PR merged: Status Done and the issue closed (a same-repo PR whose
        body says `Closes #N` already closed it; closing again is a no-op)."""
        self.set_status(key, st_done, comment)
        if self.gh.issue_state(key) == "OPEN":
            try:
                self.gh.close_issue(key)
            except GhError as e:
                self.log(f"WARN: {key}: could not close: {e}")
