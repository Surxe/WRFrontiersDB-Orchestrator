"""Site-deploy stage: dispatch WRFrontiersDB-Site's CI workflow, then follow it.

The orchestrator publishes data to WRFrontiersDB-Data but does not push the Site
repo, so nothing auto-triggers the Site's Pages deploy. This stage fires it
explicitly, mirroring the news-scraper's `gh workflow run` dispatch pattern.

It runs AFTER PARSE and INDEX (data and slug map pushed to `main`), so the Pages
build, which checks out Surxe/WRFrontiersDB-Data@main, bakes in the fresh data.
We always target `--ref main` explicitly: the deploy is a main-branch concern,
independent of the Site repo's default branch.

Then it waits for that CI run to finish (its own `site-ci` step log). Consumers
link to pages from the data repo's slug map as soon as it is pushed, so a deploy
that fails or never finishes leaves them pointing at pages that don't exist yet:
that fails this stage, and the run report says why.

A successful deploy is recorded in `data/site_deploy_state.json` (the CI run id,
which the Site bakes into its build outputs as `build_id`). Consumers of those
outputs watch the file: the Discord bot re-fetches `/meta_descriptions.json`
when it changes, so it refreshes once per deploy and can tell a stale CDN copy
from the new build.

Auth: uses the ambient `gh` (authed as dev on the home server), which must have
Actions-dispatch rights on the Site repo. `gh workflow run` exits 0 once the run
is queued; a non-zero exit fails the pipeline so a missed dispatch is loud.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

import gh_runs
from logging_stream import RunLogger, run_streamed
from repos import Repos

SITE_REPO = "Surxe/WRFrontiersDB-Site"
# ci.yaml (check -> test -> build -> deploy) replaced pages.yaml in Site #122.
# Its deploy job runs for any non-PR event on main, so a workflow_dispatch on
# --ref main builds against the fresh data and publishes GitHub Pages.
SITE_WORKFLOW = "ci.yaml"
SITE_DEPLOY_REF = "main"
# A dispatched run takes about 2 minutes.
CI_TIMEOUT_SECONDS = 1200
STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "site_deploy_state.json"


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    dispatched_since = time.time()
    rc = run_streamed(
        ["gh", "workflow", "run", SITE_WORKFLOW, "-R", SITE_REPO, "--ref", SITE_DEPLOY_REF],
        cwd=repos.site_dir,
        stage="site-deploy",
        log_path=runlog.stage_log_path("site-deploy"),
    )
    if rc != 0:
        return rc

    with runlog.stage_sink("site-ci"):
        result = follow(SITE_REPO, SITE_WORKFLOW, dispatched_since, label="Site CI run",
                        timeout=CI_TIMEOUT_SECONDS)
        if not result.ok:
            return 1
        return record_deploy(result, game_version)


def record_deploy(result: gh_runs.WorkflowRun, game_version: str) -> int:
    """Write STATE_FILE for the deployed run; 1 if it can't be written."""
    state = {
        "site_run_id": result.run_id,
        "site_run_url": result.url,
        "game_version": game_version,
        "deployed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, STATE_FILE)  # consumers never see a half-written file
    except OSError as exc:
        logger.error(f"Deployed, but could not record it in {STATE_FILE}: {exc}; "
                     "consumers will not pick up this deploy")
        return 1
    logger.info(f"Recorded deploy of run {result.run_id} in {STATE_FILE}")
    return 0


# Indirection so tests can stub the GitHub polling.
follow = gh_runs.follow_dispatched_run
