"""ralph-gh — CLI entry point, preflight and the polling main loop.

Usage:
    python3 -m ralph_gh <PROJECT-URL | owner/number> [DEFAULT-REPO]

The project is a GitHub Project (v2); its issue items are the tickets and
its Status field is the state machine. Each issue is implemented in its own
repository unless its body carries a "## Repository" heading naming another
one. The optional 2nd argument is only a FALLBACK for that heading.

Auth: whatever `gh auth login` configured. The token needs the `project`
scope on top of `repo` (`gh auth refresh -h github.com -s project`).

Worker agent: RALPH_AGENT=claude (default, `claude -p`) or RALPH_AGENT=opencode
(`opencode run --auto`; RALPH_AGENT_BIN=kilo for the Kilo CLI).
"""

import multiprocessing
import os
import shutil
import signal
import sys
import time

from . import frontier as frontier_mod
from .config import Config
from .github import GhError
from .launcher import build_orchestrator, child_pipeline, log
from .orchestrator import Orchestrator


def warn(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] WARN: {msg}", file=sys.stderr,
          flush=True)


# ---------------------------------------------------------------------------
# Preflight — refuse to start unless the whole toolchain is actually usable.
# ---------------------------------------------------------------------------
def preflight(cfg: Config, orch: Orchestrator) -> dict:
    log("Preflight checks...")
    for tool in (cfg.agent_command, "git", "gh"):
        if not shutil.which(tool):
            sys.exit(f"FATAL: '{tool}' not on PATH"
                     + (f" (RALPH_AGENT={cfg.agent})." if tool == cfg.agent_command
                        else "."))
    if not orch.gh.gh.auth_ok():
        sys.exit("FATAL: `gh auth status` failed — run `gh auth login` first.")

    try:
        project = orch.tracker.project
    except GhError as e:
        if e.insufficient_scopes:
            sys.exit("FATAL: the gh token lacks the `project` scope, which "
                     "GitHub Projects (v2) require.\n"
                     "  Fix: gh auth refresh -h github.com -s project\n"
                     f"  ({e})")
        sys.exit(f"FATAL: cannot resolve project {cfg.project_ref}: {e}")
    log(f"  Project OK: {project.title} ({project.url})")
    if not project.status_field_id:
        sys.exit("FATAL: the project has no single-select 'Status' field; "
                 "the loop drives the Status column.")
    for label, spec in (("ready", cfg.st_ready), ("in-progress", cfg.st_inprogress),
                        ("review", cfg.st_review), ("done", cfg.st_done)):
        if not orch.tracker.option_for(spec):
            warn(f"project Status has no option matching {label} spec "
                 f"'{spec}' (options: {', '.join(project.status_options)}). "
                 f"Set ST_{label.upper().replace('-', '')} to one of them.")
    if not orch.tracker.option_for(cfg.st_ready):
        sys.exit("FATAL: no Ready column — nothing could ever be picked up.")

    log("  Fetching the frontier once (validates issue access)...")
    data = frontier_mod.fetch(orch.tracker, cfg)
    repos = []
    for c in data.get("children", []):
        r = c.get("targetRepo")
        if r and r not in repos:
            repos.append(r)
    if not repos:
        warn("the project has no issue items yet — the loop will idle until "
             "some appear")
    for full in repos[:3]:
        proto = orch.ops.detect_protocol(full)
        if proto:
            log(f"  Local git auth OK ({proto} -> {full})")
        else:
            sys.exit(
                f"FATAL: local git auth cannot reach {full} over https OR ssh.\n"
                "  The loop clones/pushes with YOUR credentials.\n"
                "  Fix one of:\n"
                "    - gh auth setup-git   (HTTPS through the gh credential helper)\n"
                "    - load an SSH key for github.com into ssh-agent "
                "(must work with BatchMode=yes)\n"
                f"  Test with: git ls-remote https://github.com/{full}.git")
    log("Preflight passed.")
    return data


def _pid_alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _kill_pid(pid: str) -> None:
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (OSError, ValueError):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit("Usage: ralph-gh <PROJECT-URL | owner/number> [DEFAULT-REPO]\n"
                 "  e.g. ralph-gh https://github.com/users/octocat/projects/3\n"
                 "       ralph-gh octocat/3 octocat/my-repo")
    try:
        cfg = Config.from_env(argv[0], argv[1] if len(argv) > 1 else "")
    except ValueError as e:
        sys.exit(f"FATAL: {e}")
    os.makedirs(cfg.workspace_base, exist_ok=True)
    os.makedirs(cfg.state_dir, exist_ok=True)
    os.makedirs(cfg.log_dir, exist_ok=True)

    orch = build_orchestrator(cfg)
    data = preflight(cfg, orch)
    state = orch.state
    ctx = multiprocessing.get_context("spawn")
    procs = {}

    deadline = time.time() + cfg.max_runtime
    log("=== ralph-gh ===")
    fallback_repo = cfg.default_full_repo or "the issue's own repo"
    log(f"Project: {cfg.project_ref} | Repo: per-issue (fallback: "
        f"{fallback_repo}) -> per-issue "
        f"target branch (fallback: {cfg.target_branch or 'repo default'})")
    log(f"Concurrency: {cfg.max_concurrent} | Poll: {cfg.poll_seconds}s | "
        f"Runtime: {cfg.max_runtime}s")
    log(f"Agent: {cfg.agent} ({cfg.agent_command}) | "
        f"Model: {cfg.model or '(CLI default)'} | Draft PRs: {cfg.draft_prs} | "
        f"Resync: {cfg.resync} | Workspaces: {cfg.workspace_base}")
    if cfg.collaudo:
        log(f"Collaudo: on — agent {cfg.collaudo_agent} ({cfg.collaudo_agent_command}), "
            f"browser {cfg.collaudo_browser}, repos [{cfg.collaudo_repos or 'all'}], "
            f"probe {cfg.collaudo_probe or 'none'}")
    else:
        log("Collaudo: off (RALPH_COLLAUDO=1 enables the local acceptance run)")
    log("================")

    def running() -> int:
        return state.count_running(_pid_alive)

    first = True
    while time.time() < deadline:
        # Every poll step is network-facing; a blip must cost one poll, not
        # the loop (and never a needs-human ticket).
        try:
            # 1. close any merged tickets (unblocks the frontier)
            orch.detect_merges()

            # 2. watchdog: kill overrunning pipelines
            now = time.time()
            for key in list(procs):
                if not procs[key].is_alive():
                    procs.pop(key)
            for key in state.owned_keys():
                pid = state.get(key, "pid")
                if not pid or not _pid_alive(pid):
                    continue
                started = int(state.get(key, "started") or now)
                age = int(now - started)
                if age > cfg.session_timeout * 3:
                    warn(f"{key}: pipeline exceeded the watchdog ({age}s) — "
                         "killing")
                    _kill_pid(pid)
                    state.set(key, "state", "failed")

            # 3. fetch the frontier (the preflight already fetched one)
            log(f"Polling frontier ({running()}/{cfg.max_concurrent} "
                "running)...")
            if not first:
                data = frontier_mod.fetch(orch.tracker, cfg)
            first = False
        except Exception as e:
            warn(f"poll failed ({e.__class__.__name__}: {e}) — will retry "
                 "next poll")
            time.sleep(cfg.poll_seconds)
            continue

        try:
            # 4. adopt unowned state entries, close items whose PR merged
            #    outside this run's tracking, refresh sibling context,
            #    resync stale open PRs
            state.adopt_project_items(
                [c.get("key", "") for c in data.get("children", [])])
            orch.sync_done_from_prs(data)
            orch.write_siblings(data)
            if cfg.resync:
                orch.resync_stale_prs(
                    slots_free=lambda: running() < cfg.max_concurrent)

            # 4b. when NOTHING is grabbable, log WHY per item
            for line in frontier_mod.idle_reasons(data, cfg):
                log(f"  {line}")

            # 5. launch pipelines for ready + unclaimed items
            slots = max(0, cfg.max_concurrent - running())
            for c in orch.launch_plan(data, slots):
                key = c["key"]
                log(f"  launching pipeline: {key} "
                    f"[{c.get('targetRepo') or cfg.default_full_repo}"
                    f" -> {c.get('targetBranch') or 'repo default'}] — "
                    f"{c.get('summary', '')}")
                p = ctx.Process(target=child_pipeline,
                                args=(cfg, key, c.get("summary", ""),
                                      c.get("targetRepo", ""),
                                      c.get("targetBranch", ""),
                                      c.get("item_id", "")),
                                daemon=False)
                p.start()
                procs[key] = p
                state.set(key, "pid", p.pid)
                state.mark_project(key)
                time.sleep(2)
        except Exception as e:
            warn(f"poll bookkeeping failed ({e.__class__.__name__}: {e}) — "
                 "will retry next poll")

        log(f"Sleeping {cfg.poll_seconds}s ({running()}/{cfg.max_concurrent} "
            "running)...")
        time.sleep(cfg.poll_seconds)

    log("=== Time limit reached — shutting down ===")
    for key in state.owned_keys():
        pid = state.get(key, "pid")
        if pid and _pid_alive(pid):
            warn(f"killing {key} ({pid})")
            _kill_pid(pid)
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
