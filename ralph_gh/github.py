"""GitHub access through the `gh` CLI — no MCP server, no token handling of
our own: whatever `gh auth login` set up is what the loop (and every worker
session it spawns) uses.

Two layers: `Gh` runs the CLI and parses JSON; `GitHubClient` knows the REST
and GraphQL shapes the loop needs (Projects v2 items and Status field,
issues, dependencies, pull requests, checks)."""

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .text import iso_epoch, make_key, split_key

# checks_status outcomes
SUCCESS = "SUCCESS"
FAILED = "FAILED"
PENDING = "PENDING"
NONE = "NONE"

_FAILING_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required",
                        "startup_failure", "stale"}


class GhError(Exception):
    def __init__(self, msg: str, status: int = 0, scopes: bool = False):
        super().__init__(msg)
        self.status = status
        self.insufficient_scopes = scopes


class Gh:
    """Thin runner around `gh`. `run` is injectable for tests."""

    def __init__(self, run: Callable = subprocess.run, timeout: int = 60):
        self._run = run
        self.timeout = timeout

    def _exec(self, args: List[str], stdin: Optional[str] = None
              ) -> Tuple[int, str, str]:
        try:
            p = self._run(["gh"] + args, capture_output=True, text=True,
                          input=stdin, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise GhError(f"gh timed out after {self.timeout}s: {' '.join(args[:3])}")
        except OSError as e:
            raise GhError(f"cannot run gh: {e}")
        return p.returncode, p.stdout or "", p.stderr or ""

    @staticmethod
    def _status_of(stderr: str) -> int:
        m = re.search(r"\(HTTP (\d{3})\)", stderr or "")
        return int(m.group(1)) if m else 0

    def api(self, path: str, method: str = "GET",
            body: Optional[dict] = None,
            params: Optional[Dict[str, Any]] = None,
            paginate: bool = False) -> Any:
        """REST call. GET params go on the query string; a body is sent as
        JSON on stdin. Returns parsed JSON (None for an empty response) and
        raises GhError on a non-2xx status."""
        args = ["api", "-X", method, path.lstrip("/")]
        for k, v in (params or {}).items():
            args += ["-f" if isinstance(v, str) else "-F", f"{k}={v}"]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        if paginate:
            args += ["--paginate", "--slurp"]
        rc, out, err = self._exec(args, stdin)
        if rc != 0:
            raise GhError(f"gh api {method} {path}: {err.strip() or out.strip()}",
                          status=self._status_of(err))
        if not out.strip():
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise GhError(f"gh api {method} {path}: non-JSON response")
        if paginate and isinstance(data, list) and data and all(
                isinstance(x, list) for x in data):
            data = [item for page in data for item in page]
        return data

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        """GraphQL call; returns the `data` object or raises GhError carrying
        the error messages (and a flag when the token lacks a scope)."""
        rc, out, err = self._exec(
            ["api", "graphql", "--input", "-"],
            json.dumps({"query": query, "variables": variables or {}}))
        data: dict = {}
        try:
            data = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            data = {}
        errors = data.get("errors") if isinstance(data, dict) else None
        if rc != 0 or errors:
            msgs = "; ".join(e.get("message", "") for e in (errors or [])) \
                or err.strip() or "unknown GraphQL error"
            scopes = any(e.get("type") == "INSUFFICIENT_SCOPES"
                         for e in (errors or [])) or "INSUFFICIENT_SCOPES" in err
            raise GhError(f"gh api graphql: {msgs}", scopes=scopes)
        return data.get("data") or {}

    def auth_ok(self) -> bool:
        rc, _, _ = self._exec(["auth", "status"])
        return rc == 0


@dataclass
class ProjectInfo:
    id: str
    title: str
    url: str
    owner_kind: str                       # "users" | "orgs"
    status_field_id: str
    status_options: Dict[str, str] = field(default_factory=dict)  # name -> id


_PROJECT_Q = """
query($login: String!, $number: Int!) {
  %s(login: $login) {
    projectV2(number: $number) {
      id title url
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}"""

_ITEMS_Q = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue {
              id number title body state url updatedAt
              repository { nameWithOwner }
              labels(first: 50) { nodes { name } }
              assignees(first: 10) { nodes { login } }
            }
          }
        }
      }
    }
  }
}"""

_SET_STATUS_M = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { singleSelectOptionId: $option }
  }) { projectV2Item { id } }
}"""

_READY_M = """
mutation($id: ID!) {
  markPullRequestReadyForReview(input: { pullRequestId: $id }) {
    pullRequest { id isDraft }
  }
}"""


class GitHubClient:
    def __init__(self, gh: Optional[Gh] = None):
        self.gh = gh or Gh()

    # -- identity / repos ------------------------------------------------------
    def viewer_login(self) -> str:
        d = self.gh.api("user")
        return (d or {}).get("login", "") if isinstance(d, dict) else ""

    def repo_reachable(self, full: str) -> bool:
        try:
            d = self.gh.api(f"repos/{full}")
        except GhError:
            return False
        return isinstance(d, dict) and bool(d.get("full_name"))

    def default_branch(self, full: str) -> str:
        try:
            d = self.gh.api(f"repos/{full}")
        except GhError:
            return ""
        return (d or {}).get("default_branch", "") if isinstance(d, dict) else ""

    # -- project ---------------------------------------------------------------
    def resolve_project(self, owner_kind: str, owner: str, number: int
                        ) -> ProjectInfo:
        kinds = [owner_kind] if owner_kind in ("users", "orgs") else ["users", "orgs"]
        last: Optional[GhError] = None
        for kind in kinds:
            root = "user" if kind == "users" else "organization"
            try:
                data = self.gh.graphql(_PROJECT_Q % root,
                                       {"login": owner, "number": number})
            except GhError as e:
                if e.insufficient_scopes:
                    raise
                last = e
                continue
            node = (data.get(root) or {}).get("projectV2")
            if not node:
                continue
            fld = node.get("field") or {}
            return ProjectInfo(
                id=node["id"], title=node.get("title", ""),
                url=node.get("url", ""), owner_kind=kind,
                status_field_id=fld.get("id", ""),
                status_options={o["name"]: o["id"]
                                for o in fld.get("options", []) or []})
        raise GhError(f"project {owner}/{number} not found"
                      + (f" ({last})" if last else ""))

    def project_items(self, project_id: str) -> List[dict]:
        """Every ISSUE item of the project, normalized. Draft items and pull
        requests are skipped: the loop implements issues."""
        out: List[dict] = []
        after = None
        while True:
            data = self.gh.graphql(_ITEMS_Q, {"id": project_id, "after": after})
            items = ((data.get("node") or {}).get("items") or {})
            for n in items.get("nodes") or []:
                c = n.get("content") or {}
                if c.get("__typename") != "Issue":
                    continue
                full = (c.get("repository") or {}).get("nameWithOwner", "")
                if "/" not in full:
                    continue
                owner, repo = full.split("/", 1)
                status = (n.get("fieldValueByName") or {}).get("name") or ""
                out.append({
                    "key": make_key(owner, repo, int(c["number"])),
                    "item_id": n["id"],
                    "issue_id": c.get("id", ""),
                    "owner": owner, "repo": repo, "number": int(c["number"]),
                    "summary": c.get("title", ""),
                    "body": c.get("body") or "",
                    "state": (c.get("state") or "").upper(),
                    "url": c.get("url", ""),
                    "updated": iso_epoch(c.get("updatedAt")),
                    "labels": [l["name"] for l in (c.get("labels") or {}).get("nodes", [])],
                    "assignees": [a["login"] for a in (c.get("assignees") or {}).get("nodes", [])],
                    "status": status,
                })
            info = items.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        return out

    def set_item_status(self, project_id: str, item_id: str, field_id: str,
                        option_id: str) -> None:
        self.gh.graphql(_SET_STATUS_M, {"project": project_id, "item": item_id,
                                        "field": field_id, "option": option_id})

    # -- issues ----------------------------------------------------------------
    def issue(self, key: str) -> dict:
        o, r, n = split_key(key)
        d = self.gh.api(f"repos/{o}/{r}/issues/{n}")
        return d if isinstance(d, dict) else {}

    def issue_updated_epoch(self, key: str) -> Optional[int]:
        try:
            return iso_epoch(self.issue(key).get("updated_at"))
        except GhError:
            return None

    def issue_state(self, key: str) -> str:
        """"OPEN" / "CLOSED" / "" when unreadable."""
        try:
            return (self.issue(key).get("state") or "").upper()
        except GhError:
            return ""

    def issue_blockers(self, key: str) -> List[Tuple[str, str]]:
        """GitHub's native issue dependencies: the issues this one is blocked
        by, as (key, "OPEN"/"CLOSED"). Empty when the endpoint is unavailable
        (older GHES) — prose blockers still apply."""
        o, r, n = split_key(key)
        try:
            d = self.gh.api(f"repos/{o}/{r}/issues/{n}/dependencies/blocked_by",
                            paginate=True)
        except GhError:
            return []
        out = []
        for it in d or []:
            m = re.search(r"/repos/([^/]+)/([^/]+)$", it.get("repository_url", ""))
            if not m:
                continue
            out.append((make_key(m.group(1), m.group(2), int(it["number"])),
                        (it.get("state") or "").upper()))
        return out

    def comment(self, key: str, body: str) -> None:
        o, r, n = split_key(key)
        self.gh.api(f"repos/{o}/{r}/issues/{n}/comments", "POST",
                    body={"body": body})

    def add_labels(self, key: str, labels: List[str]) -> None:
        o, r, n = split_key(key)
        self.gh.api(f"repos/{o}/{r}/issues/{n}/labels", "POST",
                    body={"labels": labels})

    def remove_label(self, key: str, label: str) -> None:
        o, r, n = split_key(key)
        try:
            self.gh.api(f"repos/{o}/{r}/issues/{n}/labels/{label}", "DELETE")
        except GhError as e:
            if e.status != 404:
                raise

    def assign(self, key: str, login: str) -> None:
        o, r, n = split_key(key)
        self.gh.api(f"repos/{o}/{r}/issues/{n}/assignees", "POST",
                    body={"assignees": [login]})

    def unassign(self, key: str, login: str) -> None:
        o, r, n = split_key(key)
        self.gh.api(f"repos/{o}/{r}/issues/{n}/assignees", "DELETE",
                    body={"assignees": [login]})

    def close_issue(self, key: str) -> None:
        o, r, n = split_key(key)
        self.gh.api(f"repos/{o}/{r}/issues/{n}", "PATCH",
                    body={"state": "closed", "state_reason": "completed"})

    # -- pull requests -----------------------------------------------------------
    def find_pr(self, full: str, branch: str, state: str
                ) -> Optional[Tuple[str, str, str]]:
        """The PR from `branch` in `state` ("OPEN" | "MERGED" | "CLOSED"),
        as (state, number, url), or None. Newest first."""
        owner = full.split("/", 1)[0]
        try:
            prs = self.gh.api(f"repos/{full}/pulls",
                              params={"head": f"{owner}:{branch}", "state": "all",
                                      "sort": "created", "direction": "desc"})
        except GhError:
            return None
        for pr in prs or []:
            st = _pr_state(pr)
            if st == state:
                return st, str(pr["number"]), pr.get("html_url", "")
        return None

    def create_pr(self, full: str, head: str, base: str, title: str,
                  body: str, draft: bool = True) -> Optional[Tuple[str, str]]:
        try:
            d = self.gh.api(f"repos/{full}/pulls", "POST",
                            body={"title": title, "head": head, "base": base,
                                  "body": body, "draft": draft})
        except GhError:
            return None
        if not isinstance(d, dict) or "number" not in d:
            return None
        return str(d["number"]), d.get("html_url", "")

    def pr_info(self, full: str, number: str) -> dict:
        """{state, number, url, head_sha, node_id, draft} or {} when unreadable."""
        try:
            d = self.gh.api(f"repos/{full}/pulls/{number}")
        except GhError:
            return {}
        if not isinstance(d, dict):
            return {}
        return {"state": _pr_state(d), "number": str(d.get("number", number)),
                "url": d.get("html_url", ""),
                "head_sha": (d.get("head") or {}).get("sha", ""),
                "node_id": d.get("node_id", ""), "draft": bool(d.get("draft"))}

    def mark_ready(self, node_id: str) -> bool:
        try:
            self.gh.graphql(_READY_M, {"id": node_id})
            return True
        except GhError:
            return False

    def pr_comment_count(self, full: str, number: str) -> int:
        """Review comments + conversation comments + non-empty review bodies.
        -1 when it cannot be determined (never mistaken for zero)."""
        try:
            review_comments = self.gh.api(f"repos/{full}/pulls/{number}/comments",
                                          paginate=True) or []
            issue_comments = self.gh.api(f"repos/{full}/issues/{number}/comments",
                                         paginate=True) or []
            reviews = self.gh.api(f"repos/{full}/pulls/{number}/reviews",
                                  paginate=True) or []
        except GhError:
            return -1
        return (len(review_comments) + len(issue_comments)
                + sum(1 for r in reviews if (r.get("body") or "").strip()))

    def pr_comments_md(self, full: str, number: str) -> str:
        """Every comment on the PR, rendered for a worker: inline review
        comments with their file/line and id (so the worker can reply in
        thread), conversation comments, and review summaries."""
        try:
            rc = self.gh.api(f"repos/{full}/pulls/{number}/comments", paginate=True) or []
            ic = self.gh.api(f"repos/{full}/issues/{number}/comments", paginate=True) or []
            rv = self.gh.api(f"repos/{full}/pulls/{number}/reviews", paginate=True) or []
        except GhError:
            return ""
        lines = [f"# Comments on PR #{number} ({full})", ""]
        if rc:
            lines.append("## Inline review comments (reply with "
                         f"`gh api repos/{full}/pulls/{number}/comments/<id>/replies -f body=...`)")
            for c in rc:
                where = c.get("path", "")
                ln = c.get("line") or c.get("original_line")
                lines.append(f"- id {c.get('id')} by @{(c.get('user') or {}).get('login', '?')}"
                             f" on `{where}`" + (f" line {ln}" if ln else "") + ":")
                lines += ["  " + l for l in (c.get("body") or "").splitlines()] + [""]
        if rv:
            lines.append("## Review summaries")
            for r in rv:
                if (r.get("body") or "").strip():
                    lines.append(f"- {r.get('state', '')} by @{(r.get('user') or {}).get('login', '?')}:")
                    lines += ["  " + l for l in r["body"].splitlines()] + [""]
        if ic:
            lines.append("## Conversation comments (reply with "
                         f"`gh pr comment {number} --repo {full} --body ...`)")
            for c in ic:
                lines.append(f"- id {c.get('id')} by @{(c.get('user') or {}).get('login', '?')}:")
                lines += ["  " + l for l in (c.get("body") or "").splitlines()] + [""]
        return "\n".join(lines).rstrip() + "\n"

    def pr_comment(self, full: str, number: str, body: str) -> None:
        self.gh.api(f"repos/{full}/issues/{number}/comments", "POST",
                    body={"body": body})

    # -- checks --------------------------------------------------------------------
    def checks_status(self, full: str, sha: str) -> str:
        """Combined verdict of GitHub Actions check runs and legacy commit
        statuses on `sha`: FAILED as soon as anything failed, PENDING while
        anything is still running, SUCCESS when all finished green, NONE when
        the commit has no CI at all."""
        try:
            runs = self.gh.api(f"repos/{full}/commits/{sha}/check-runs",
                               params={"per_page": 100}) or {}
            status = self.gh.api(f"repos/{full}/commits/{sha}/status") or {}
        except GhError:
            return PENDING
        check_runs = runs.get("check_runs", []) if isinstance(runs, dict) else []
        total = len(check_runs) + int((status or {}).get("total_count") or 0)
        if total == 0:
            return NONE
        for run in check_runs:
            if run.get("status") == "completed" and \
                    (run.get("conclusion") or "") in _FAILING_CONCLUSIONS:
                return FAILED
        combined = (status or {}).get("state", "")
        if combined in ("failure", "error"):
            return FAILED
        if any(run.get("status") != "completed" for run in check_runs):
            return PENDING
        if combined == "pending" and int((status or {}).get("total_count") or 0):
            return PENDING
        return SUCCESS


def _pr_state(pr: dict) -> str:
    if pr.get("merged_at") or pr.get("merged"):
        return "MERGED"
    return "OPEN" if pr.get("state") == "open" else "CLOSED"
