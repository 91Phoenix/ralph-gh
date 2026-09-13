"""Pure parsing of ticket text and repo references.

GitHub issue bodies are Markdown, so section structure is already explicit:
a "## Repository" or "## Target branch" heading followed by its value. Only a
real heading counts — prose that merely contains "Repository: x" is ignored,
because a sentence is not a field."""

import re
from typing import List, Optional, Tuple

_KEY_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$")


def st_is(name: Optional[str], spec: Optional[str]) -> bool:
    """Case-insensitive match of a status name against a pipe-separated alias
    list ("Ready|Todo|To Do")."""
    n = (name or "").strip().casefold()
    if not n:
        return False
    return any(n == a.strip().casefold()
               for a in (spec or "").split("|") if a.strip())


def parse_repo(val: Optional[str], default_owner: str,
               host: str = "github.com") -> str:
    """Normalize a repository reference to "owner/repo".

    Accepts a bare name (default owner assumed), "owner/repo", or a full
    GitHub URL (https or ssh). "none"/"n/a" mark manual tickets and yield ""
    like an absent value."""
    val = (val or "").strip()
    if not val or val.lower() in ("none", "n/a"):
        return ""
    if host in val:
        val = re.sub(r"^.*" + re.escape(host) + r"[:/]", "", val)
        val = re.sub(r"(\.git)?/?$", "", val)
        # drop anything after owner/repo (a /issues/12 tail, say)
        parts = val.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else ""
    val = re.sub(r"\.git$", "", val).strip("/")
    if "/" in val:
        return val
    return f"{default_owner}/{val}" if default_owner else val


def make_key(owner: str, repo: str, number: int) -> str:
    return f"{owner}/{repo}#{number}"


def split_key(key: str) -> Tuple[str, str, int]:
    """"owner/repo#12" -> ("owner", "repo", 12). Raises ValueError otherwise."""
    m = _KEY_RE.match((key or "").strip())
    if not m:
        raise ValueError(f"not a ticket key: {key!r}")
    return m.group(1), m.group(2), int(m.group(3))


def key_from_url(url: str) -> str:
    """https://github.com/o/r/issues/12 -> "o/r#12"; "" when it is not one."""
    m = re.search(r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url or "")
    return make_key(m.group(1), m.group(2), int(m.group(3))) if m else ""


def branch_for(key: str, full: str) -> str:
    """The feature branch for a ticket. Same-repo issues get feature/issue-N;
    an issue implemented in a different repo (a planning repo's issue landing
    in a code repo) carries the issue's repo name so numbers cannot collide."""
    owner, repo, n = split_key(key)
    if full and full.casefold() == f"{owner}/{repo}".casefold():
        return f"feature/issue-{n}"
    return f"feature/{repo}-{n}"


def unmark(val: str) -> str:
    """Strip inline Markdown decoration from a field value."""
    val = (val or "").strip()
    val = re.sub(r"^`+|`+$", "", val)
    val = re.sub(r"^\*+|\*+$", "", val)
    val = re.sub(r"^_+|_+$", "", val)
    return val.strip()


def desc_field_line(txt: str, name: str) -> str:
    """The whole value of a heading-marked field: either on the heading line
    itself after a colon ("## Repository: foo") or the first non-empty,
    non-heading line after the heading."""
    lines = (txt or "").splitlines()
    inline = re.compile(rf"(?i)^#+\s*{name}\s*:\s*(.+?)\s*$")
    bare = re.compile(rf"(?i)^#+\s*{name}\s*:?\s*$")
    for i, line in enumerate(lines):
        m = inline.match(line)
        if m:
            return m.group(1).strip()
        if bare.match(line):
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    break
                return re.sub(r"^[-*]\s+", "", s)
            return ""
    return ""


def desc_field(txt: str, name: str) -> str:
    """First token of a heading-marked field, decoration stripped; "none" and
    "n/a" read as absent."""
    line = desc_field_line(txt, name)
    if not line:
        return ""
    first = unmark(line.split()[0].rstrip(".,;"))
    if first.lower() in ("none", "n/a"):
        return ""
    return first


def repo_field(txt: str) -> str:
    return desc_field(txt, "repository")


def branch_field(txt: str) -> str:
    """The trunk the ticket names: a "Target branch" heading, or "trunk
    <branch>" on the Repository line."""
    explicit = desc_field(txt, r"target\s*branch")
    if explicit:
        return explicit
    line = desc_field_line(txt, "repository")
    m = re.search(r"\btrunk\b\s*:?\s*(\S+)", line or "")
    return unmark(m.group(1)) if m else ""


def prose_blockers(txt: str, owner: str, repo: str) -> List[str]:
    """Ticket keys named in a "Blocked by" section: "#12", "owner/repo#12"
    or full issue URLs. "none" in the section means an explicit empty list.
    Bare "#12" resolves against the ticket's own repo."""
    m = re.search(r"(?is)^#+\s*blocked\s*by\s*:?\s*(.*?)(?=^#+\s|\Z)",
                  txt or "", re.M)
    if not m:
        return []
    section = m.group(1)
    if re.search(r"(?i)\bnone\b", section):
        return []
    out = set()
    for u in re.findall(r"https?://github\.com/\S+", section):
        k = key_from_url(u)
        if k:
            out.add(k)
    for o, r, n in re.findall(r"([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)#(\d+)", section):
        out.add(make_key(o, r, int(n)))
    for n in re.findall(r"(?<![\w/])#(\d+)\b", section):
        out.add(make_key(owner, repo, int(n)))
    return sorted(out)


def iso_epoch(ts: Optional[str]) -> Optional[int]:
    """GitHub ISO-8601 ("2026-09-13T10:00:00Z") -> epoch seconds."""
    if not ts:
        return None
    from datetime import datetime, timezone
    s = ts.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None
