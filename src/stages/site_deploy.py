"""Site-deploy stage — dispatch WRFrontiersDB-Site's GitHub Pages workflow.

The orchestrator publishes data to WRFrontiersDB-Data but does not push the Site
repo, so nothing auto-triggers the Site's Pages deploy. This stage fires it
explicitly, mirroring the news-scraper's `gh workflow run` dispatch pattern.

It runs AFTER PARSE (data pushed to `main`), so the Pages build — which checks
out Surxe/WRFrontiersDB-Data@main — bakes in the fresh data. We always target
`--ref main` explicitly: the deploy is a main-branch concern, independent of the
Site repo's default branch.

Auth: uses the ambient `gh` (authed as dev on the home server), which must have
Actions-dispatch rights on the Site repo. `gh workflow run` exits 0 once the run
is queued; a non-zero exit fails the pipeline so a missed dispatch is loud.
"""

from __future__ import annotations

from logging_stream import RunLogger, run_streamed
from repos import Repos

SITE_REPO = "Surxe/WRFrontiersDB-Site"
SITE_WORKFLOW = "pages.yaml"
SITE_DEPLOY_REF = "main"


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    return run_streamed(
        ["gh", "workflow", "run", SITE_WORKFLOW, "-R", SITE_REPO, "--ref", SITE_DEPLOY_REF],
        cwd=repos.site_dir,
        stage="site-deploy",
        log_path=runlog.stage_log_path("site-deploy"),
    )
