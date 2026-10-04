"""Follow a GitHub Actions run the pipeline dispatched, until it finishes.

`gh workflow run` (gh 2.46 on the box) doesn't print the run it created, so the
run is found afterwards: the oldest workflow_dispatch run of the workflow created
since the dispatch. A manual run fired in that same window could be picked
instead; rare, and the report links the run it followed.

Logs through loguru, so the caller wraps a call in ``runlog.stage_sink(...)`` to
give it its own step log in the run report.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

# GitHub's createdAt vs this box's clock.
_CLOCK_SKEW = 30
# Error lines pulled from the failed jobs' log into the report (best effort).
_MAX_FAILED_LINES = 10


@dataclass
class WorkflowRun:
    url: str | None = None
    run_id: str | None = None
    status: str = "not found"      # gh status; "not found" / "timed out" are ours
    conclusion: str | None = None  # success / failure / cancelled / ...

    @property
    def ok(self) -> bool:
        return self.conclusion == "success"

    def describe(self) -> str:
        state = self.conclusion or self.status
        return f"{state} - {self.url}" if self.url else state


def gh_json(args: list[str]):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _created_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def find_dispatched_run(repo: str, workflow: str, since: float, *, gh=gh_json,
                        sleep=time.sleep, clock=time.time, find_timeout: float = 180,
                        poll: float = 10) -> dict | None:
    """The run of ``workflow`` dispatched at/after ``since``, waiting for it to show up."""
    deadline = clock() + find_timeout
    while True:
        runs = gh(["run", "list", "-R", repo, "-w", workflow,
                   "-e", "workflow_dispatch", "-L", "10",
                   "--json", "databaseId,createdAt,url,status,conclusion"])
        fresh = [r for r in runs if _created_ts(r["createdAt"]) >= since - _CLOCK_SKEW]
        if fresh:
            return min(fresh, key=lambda r: _created_ts(r["createdAt"]))
        if clock() >= deadline:
            return None
        sleep(poll)


def follow_dispatched_run(repo: str, workflow: str, since: float, *, label: str,
                          gh=gh_json, sleep=time.sleep, clock=time.time,
                          timeout: float = 1800, poll: float = 20) -> WorkflowRun:
    """Wait for the dispatched run to finish; log and return its result.

    Logs the run's URL, its progress, and on failure the failed jobs/steps plus
    any error lines from their logs, so the report shows why without opening
    GitHub. ``label`` names the run in those lines ("Visualizer run", ...).
    """
    result = WorkflowRun()
    try:
        run = find_dispatched_run(repo, workflow, since, gh=gh, sleep=sleep, clock=clock)
        if run is None:
            logger.error(f"No {workflow} run found on {repo} after the dispatch.")
            return result
        run_id = str(run["databaseId"])
        result.run_id = run_id
        result.url = run["url"]
        logger.info(f"Following {label} {result.url}")

        deadline = clock() + timeout
        last_status = None
        while True:
            view = gh(["run", "view", run_id, "-R", repo, "--json", "status,conclusion,jobs"])
            result.status, result.conclusion = view["status"], view.get("conclusion") or None
            if result.status != last_status:
                logger.info(f"{label}: {result.status}")
                last_status = result.status
            if result.status == "completed":
                break
            if clock() >= deadline:
                result.status = "timed out"
                logger.error(f"{label} still {view['status']} after "
                             f"{int(timeout)}s; stopped waiting: {result.url}")
                return result
            sleep(poll)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        logger.error(f"Could not follow the {label}: {str(detail).strip()[:300]}")
        return result

    if result.ok:
        logger.info(f"{label} succeeded: {result.url}")
    else:
        _log_failure(repo, run_id, view, result, label)
    return result


def _log_failure(repo: str, run_id: str, view: dict, result: WorkflowRun, label: str) -> None:
    failed = []
    for job in view.get("jobs", []):
        if job.get("conclusion") in ("success", "skipped", None):
            continue
        steps = [s["name"] for s in job.get("steps", [])
                 if s.get("conclusion") not in ("success", "skipped", None)]
        failed.append(f"{job['name']} ({job['conclusion']})"
                      + (f": {', '.join(steps)}" if steps else ""))
    logger.error(f"{label} {result.conclusion}: {result.url}"
                 + (f" - failed: {'; '.join(failed)}" if failed else ""))
    try:
        log = subprocess.run(["gh", "run", "view", run_id, "-R", repo, "--log-failed"],
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
