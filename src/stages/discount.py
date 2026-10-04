"""Discount-run stages — wrap WRFrontiers-News-Scraper's two-step pipeline.

The scraper owns the logic (scrape the latest news posts, detect a new discount
week, dispatch the Discount-Visualizer workflow) and stays standard-library only;
the orchestrator just runs its scripts through the tee runner so each step gets a
per-stage log the run report can count and attach.

* SCRAPE — ``scripts/archive.py --latest N``: persist the N newest posts.
* WATCH  — ``scripts/watch_discount.py --latest N``: detect + dispatch. It prints
  one JSON object per line (``{"event": ...}``); :func:`read_events` reads them
  back from the stage log so the run can tell a new week from a no-op poll.
* VISUALIZER — after a real dispatch, find the visualizer's GitHub Actions run
  and wait for it to finish (:func:`follow_visualizer`), so the report carries
  the run's result, not just "dispatched".

Dispatch stays the scraper's opt-in: ``WRF_DISPATCH=1`` in the environment (set
by the systemd unit) passes straight through to the child.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import config
import gh_runs
from logging_stream import RunLogger, run_streamed
from repos import Repos

# The orchestrator's secrets: none of them are the scraper's business.
_WITHHELD = config.SECRET_KEYS + config.EMAIL_SECRET_KEYS


def _run_script(repos: Repos, runlog: RunLogger, stage: str, script: str,
                latest: int) -> int:
    scraper = repos.news_scraper_dir
    # Run from the clone so the scripts' relative defaults (archive/, data/)
    # resolve inside it. They're stdlib-only, so this interpreter will do.
    return run_streamed(
        [sys.executable, str(scraper / "scripts" / script), "--latest", str(latest)],
        cwd=scraper,
        stage=stage,
        log_path=runlog.stage_log_path(stage),
        unset_env=_WITHHELD,
    )


def scrape(repos: Repos, runlog: RunLogger, latest: int) -> int:
    return _run_script(repos, runlog, "scrape", "archive.py", latest)


def watch(repos: Repos, runlog: RunLogger, latest: int) -> int:
    return _run_script(repos, runlog, "watch", "watch_discount.py", latest)


def read_events(log_path: Path) -> list[dict]:
    """The JSON events watch_discount.py printed, in order (unreadable -> [])."""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "event" in obj:
            events.append(obj)
    return events


# --- VISUALIZER: follow the run the WATCH step dispatched ---------------------

VIS_REPO = "Surxe/WRFrontiers-Discount-Visualizer"
VIS_WORKFLOW = "all.yml"

VisualizerRun = gh_runs.WorkflowRun


def follow_visualizer(since: float, runlog: RunLogger, *, gh=gh_runs.gh_json,
                      sleep=time.sleep, clock=time.time, timeout: float = 1800,
                      poll: float = 20) -> VisualizerRun:
    """Wait for the dispatched visualizer run to finish; log and return its result.

    In-process step with its own loguru log (``NN-visualizer.log``).
    """
    with runlog.stage_sink("visualizer"):
        return gh_runs.follow_dispatched_run(
            VIS_REPO, VIS_WORKFLOW, since, label="Visualizer run",
            gh=gh, sleep=sleep, clock=clock, timeout=timeout, poll=poll)
