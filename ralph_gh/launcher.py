"""Client wiring and the multiprocessing child entry point.

This lives OUTSIDE __main__.py on purpose: the loop is started with
`python3 -m ralph_gh`, which executes __main__.py as the unimportable
`__main__` module — a spawn child that tries to unpickle a target defined
there dies. Everything a child process must re-import lands here."""

import os
import signal
import time

from .config import Config
from .github import GitHubClient
from .gitops import GitOps
from .orchestrator import Orchestrator
from .state import StateStore
from .tracker import Tracker
from . import worker
from .worker import WorkerRunner


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_orchestrator(cfg: Config) -> Orchestrator:
    gh = GitHubClient()
    tracker = Tracker(gh, cfg.project_owner_kind, cfg.project_owner,
                      cfg.project_number, cfg.needs_human_label, log=log)
    state = StateStore(cfg.state_dir, cfg.project_ref)
    return Orchestrator(cfg, tracker, gh, GitOps(cfg), state,
                        WorkerRunner(cfg), log=log,
                        collaudo_runner=WorkerRunner(cfg, profile="collaudo"))


def _stop_pipeline(signum, frame) -> None:
    """The loop was stopped or the watchdog fired: end the agent session
    this pipeline is running and exit. The state stays `running`; the loop
    releases the ticket once this process is gone."""
    worker.terminate_current_worker()
    os._exit(128 + signum)


def child_pipeline(cfg: Config, key: str, summary: str, repoval: str,
                   branchval: str, item_id: str = "") -> None:
    """One ticket pipeline, run in a spawned child process. Builds its own
    clients — nothing network-y is shared across the fork boundary."""
    signal.signal(signal.SIGTERM, _stop_pipeline)
    signal.signal(signal.SIGINT, _stop_pipeline)
    orch = build_orchestrator(cfg)
    if item_id:
        orch.tracker.remember_item(key, item_id)
    orch.run_pipeline(key, summary, repoval, branchval)
