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
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

import config
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
#
# `gh workflow run` (gh 2.46 on the box) doesn't print the run it created, so the
# run is found afterwards: the oldest workflow_dispatch run of the visualizer's
# workflow created since the WATCH step started. A manual run fired in that same
# window could be picked instead; rare, and the report links the run it followed.

VIS_REPO = "Surxe/WRFrontiers-Discount-Visualizer"
VIS_WORKFLOW = "all.yml"
# GitHub's createdAt vs this box's clock.
_CLOCK_SKEW = 30
# Error lines pulled from the failed jobs' log into the report (best effort).
_MAX_FAILED_LINES = 10


@dataclass
class VisualizerRun:
    url: str | None = None
    status: str = "not found"      # gh status; "not found" / "timed out" are ours
    conclusion: str | None = None  # success / failure / cancelled / ...

    @property
    def ok(self) -> bool:
        return self.conclusion == "success"

    def describe(self) -> str:
        state = self.conclusion or self.status
        return f"{state} - {self.url}" if self.url else state


def _gh_json(args: list[str]):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _created_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def find_dispatched_run(since: float, *, gh=_gh_json, sleep=time.sleep,
                        clock=time.time, find_timeout: float = 180,
                        poll: float = 10) -> dict | None:
    """The visualizer run dispatched at/after ``since``, waiting for it to show up."""
    deadline = clock() + find_timeout
    while True:
        runs = gh(["run", "list", "-R", VIS_REPO, "-w", VIS_WORKFLOW,
                   "-e", "workflow_dispatch", "-L", "10",
                   "--json", "databaseId,createdAt,url,status,conclusion"])
        fresh = [r for r in runs if _created_ts(r["createdAt"]) >= since - _CLOCK_SKEW]
        if fresh:
            return min(fresh, key=lambda r: _created_ts(r["createdAt"]))
        if clock() >= deadline:
            return None
        sleep(poll)


def follow_visualizer(since: float, runlog: RunLogger, *, gh=_gh_json,
                      sleep=time.sleep, clock=time.time, timeout: float = 1800,
                      poll: float = 20) -> VisualizerRun:
    """Wait for the dispatched visualizer run to finish; log and return its result.

    In-process step with its own loguru log (``NN-visualizer.log``): the run's
    URL, its progress, and on failure the failed jobs/steps plus any error lines
    from their logs, so the report shows why without opening GitHub.
    """
    result = VisualizerRun()
    with runlog.stage_sink("visualizer"):
        try:
            run = find_dispatched_run(since, gh=gh, sleep=sleep, clock=clock)
            if run is None:
                logger.error(f"No {VIS_WORKFLOW} run found on {VIS_REPO} after the dispatch.")
                return result
            run_id = str(run["databaseId"])
            result.url = run["url"]
            logger.info(f"Following visualizer run {result.url}")

            deadline = clock() + timeout
            last_status = None
            while True:
                view = gh(["run", "view", run_id, "-R", VIS_REPO,
                           "--json", "status,conclusion,jobs"])
                result.status, result.conclusion = view["status"], view.get("conclusion") or None
                if result.status != last_status:
                    logger.info(f"Visualizer run: {result.status}")
                    last_status = result.status
                if result.status == "completed":
                    break
                if clock() >= deadline:
                    result.status = "timed out"
                    logger.error(f"Visualizer run still {view['status']} after "
                                 f"{int(timeout)}s; stopped waiting: {result.url}")
                    return result
                sleep(poll)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            detail = getattr(exc, "stderr", None) or exc
            logger.error(f"Could not follow the visualizer run: {str(detail).strip()[:300]}")
            return result

        if result.ok:
            logger.info(f"Visualizer run succeeded: {result.url}")
        else:
            _log_failure(run_id, view, result)
    return result


def _log_failure(run_id: str, view: dict, result: VisualizerRun) -> None:
    failed = []
    for job in view.get("jobs", []):
        if job.get("conclusion") in ("success", "skipped", None):
            continue
        steps = [s["name"] for s in job.get("steps", [])
                 if s.get("conclusion") not in ("success", "skipped", None)]
        failed.append(f"{job['name']} ({job['conclusion']})"
                      + (f": {', '.join(steps)}" if steps else ""))
    logger.error(f"Visualizer run {result.conclusion}: {result.url}"
                 + (f" - failed: {'; '.join(failed)}" if failed else ""))
    try:
        log = subprocess.run(["gh", "run", "view", run_id, "-R", VIS_REPO, "--log-failed"],
                             check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return
    shown = 0
    for line in log.splitlines():
        # --log-failed lines are "<job>\t<step>\t<timestamp> <text>".
        text = line.split("\t")[-1].split(" ", 1)[-1].strip()
        if ("##[error]" in text or "[ERROR]" in text or "Unable to map" in text):
            logger.error(f"  {text.replace('##[error]', '')[:300]}")
            shown += 1
            if shown >= _MAX_FAILED_LINES:
                break
