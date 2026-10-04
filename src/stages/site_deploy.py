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

Auth: uses the ambient `gh` (authed as dev on the home server), which must have
Actions-dispatch rights on the Site repo. `gh workflow run` exits 0 once the run
is queued; a non-zero exit fails the pipeline so a missed dispatch is loud.
"""

from __future__ import annotations

import time

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
    return 0 if result.ok else 1


# Indirection so tests can stub the GitHub polling.
follow = gh_runs.follow_dispatched_run
