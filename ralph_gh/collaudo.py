"""Collaudo gate — decides whether a ticket's freshly reviewed PR gets an
automatic local acceptance run before the address phase.

The collaudo runs AFTER the PR is open and reviewed, on purpose: its findings
land as ordinary `collaudo:` PR comments that the existing address pass fixes
together with the review, and the local app being down can never block a PR
from existing — it only costs the collaudo, and the issue says so.

The gate is cheap and orchestrator-side: probing the app here costs one shell
command, probing it inside a worker costs a whole agent session that ends in
COLLAUDO_FAIL. The browser, when one is wanted, is the Playwright MCP server:
the Kilo CLI and OpenCode load it from their config, Claude Code from the
--mcp-config the runner writes — headless sessions cannot load the
claude-in-chrome extension, so that is not an option here."""

import shutil
import subprocess
from typing import Callable, Tuple

from .config import Config

PROBE_TIMEOUT = 20  # seconds


def repo_applicable(cfg: Config, full: str) -> bool:
    """RALPH_COLLAUDO_REPOS narrows the collaudo to the repos the local
    environment actually runs; accepts owner/repo or bare names."""
    allowed = cfg.collaudo_repos.split()
    if not allowed:
        return True
    name = full.rsplit("/", 1)[-1]
    return any(a.casefold() in (full.casefold(), name.casefold()) for a in allowed)


def availability(cfg: Config,
                 run: Callable = subprocess.run) -> Tuple[bool, str]:
    """(True, "") when a collaudo worker has a fighting chance, else
    (False, why-not) — the reason is written for the issue comment a human
    reads, so it says what to change."""
    if not cfg.collaudo:
        return False, "disabled (RALPH_COLLAUDO=0)"
    if not shutil.which(cfg.collaudo_agent_command):
        return False, (f"the collaudo agent `{cfg.collaudo_agent_command}` "
                       f"is not on PATH (RALPH_COLLAUDO_AGENT / "
                       f"RALPH_COLLAUDO_AGENT_BIN)")
    if cfg.collaudo_browser == "playwright" and not shutil.which("npx"):
        return False, ("npx is not on PATH, so the Playwright MCP browser "
                       "cannot start (install Node, or RALPH_COLLAUDO_BROWSER=none)")
    if cfg.collaudo_probe:
        try:
            proc = run(cfg.collaudo_probe, shell=True, capture_output=True,
                       timeout=PROBE_TIMEOUT)
        except Exception as e:
            return False, (f"the local app probe `{cfg.collaudo_probe}` "
                           f"failed ({e.__class__.__name__})")
        if proc.returncode != 0:
            return False, (f"the local app probe `{cfg.collaudo_probe}` "
                           f"exited {proc.returncode} — the app is not up")
    return True, ""
