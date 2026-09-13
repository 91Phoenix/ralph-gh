"""The project's ready frontier: which items an agent may pick up now.

A ticket is ready when it is an OPEN issue in the project whose Status is a
Ready column, that carries the agent label, is unassigned, does not carry
the needs-human label, and whose blockers — GitHub's native "blocked by"
dependencies plus any "Blocked by" section in the body — are all closed."""

from typing import Dict, List

from .config import Config
from .text import branch_field, parse_repo, prose_blockers, repo_field, st_is


def is_ready(child: dict, cfg: Config) -> bool:
    if child.get("state") != "OPEN":
        return False
    if not st_is(child.get("status"), cfg.st_ready):
        return False
    labels = child.get("labels") or []
    if cfg.agent_label and cfg.agent_label not in labels:
        return False
    if cfg.needs_human_label in labels:
        return False
    if child.get("assignees"):
        return False
    return not child.get("blockedByOpen")


def fetch(tracker, cfg: Config) -> Dict[str, List[dict]]:
    """Project items enriched with blockers, repo and target branch, plus the
    readiness verdict. One GraphQL page walk plus one dependencies call per
    open, unassigned item."""
    children = []
    state_cache: Dict[str, str] = {}
    items = tracker.items()
    for it in items:
        state_cache[it["key"]] = it.get("state", "")
    for it in items:
        c = dict(it)
        body = c.get("body") or ""
        # `repo` stays the issue's own repo name; `targetRepo` is where the
        # work lands — the Repository heading when present, else the same repo.
        c["targetRepo"] = (parse_repo(repo_field(body), c["owner"], cfg.git_host)
                           or f"{c['owner']}/{c['repo']}")
        c["targetBranch"] = branch_field(body)
        blockers = set(prose_blockers(body, c["owner"], c["repo"]))
        # Native dependencies cost one call each; only ask for items that
        # could otherwise be picked up.
        if c.get("state") == "OPEN" and not c.get("assignees"):
            for k, st in tracker.gh.issue_blockers(c["key"]):
                blockers.add(k)
                state_cache[k] = st
        blocked_open = []
        for k in sorted(blockers):
            st = state_cache.get(k)
            if st is None:
                st = tracker.gh.issue_state(k)
                state_cache[k] = st
            if st != "CLOSED":
                blocked_open.append(k)
        c["blockedBy"] = sorted(blockers)
        c["blockedByOpen"] = blocked_open
        c["ready"] = is_ready(c, cfg)
        children.append(c)
    children.sort(key=lambda x: (x["owner"], x["repo"], x["number"]))
    return {"children": children}


def idle_reasons(data: dict, cfg: Config) -> List[str]:
    """When nothing is grabbable, one line per open item saying why."""
    children = data.get("children", [])
    if any(c.get("ready") for c in children):
        return []
    out = []
    for c in children:
        if c.get("state") != "OPEN" or st_is(c.get("status"), cfg.st_done):
            continue
        why = []
        if not st_is(c.get("status"), cfg.st_ready):
            why.append(f"status={c.get('status') or 'none'}")
        labels = c.get("labels") or []
        if cfg.needs_human_label in labels:
            why.append(cfg.needs_human_label)
        elif cfg.agent_label and cfg.agent_label not in labels:
            why.append(f"no {cfg.agent_label} label")
        if c.get("assignees"):
            why.append("assigned:" + ",".join(c["assignees"]))
        if c.get("blockedByOpen"):
            why.append("blocked-by " + ",".join(c["blockedByOpen"]))
        out.append(f"idle: {c['key']} — {'; '.join(why) or 'unknown'}")
    return out


def siblings_md(data: dict) -> str:
    lines = [f"- {c['key']} ({c.get('status') or c.get('state', '')}) — "
             f"{c.get('summary', '')}" for c in data.get("children", [])]
    return "\n".join(lines) + ("\n" if lines else "")
