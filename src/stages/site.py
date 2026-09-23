"""Site stage — builds WRFrontiersDB-Site (Astro) against the updated data repo.

The site consumes WRFrontiersDB-Data via its in-repo symlink, so once the Parser
has pushed/updated that checkout, a plain production build bakes in the new data.

`npm run build` does NOT chain slug generation, and the build errors without it,
so we run `build:slugs` first — exactly what CI does. The generated slug output
is a build artifact and is not committed (CI regenerates it too).

This is a local pre-flight: a passing build here is the gate before SITE-DEPLOY
dispatches the Pages workflow, so a build break stops the pipeline cheaply.
"""

from __future__ import annotations

from logging_stream import RunLogger, run_streamed
from repos import Repos


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    rc = run_streamed(
        ["npm", "run", "build:slugs"],
        cwd=repos.site_dir,
        stage="site-slugs",
        log_path=runlog.stage_log_path("site-slugs"),
    )
    if rc != 0:
        return rc

    return run_streamed(
        ["npm", "run", "build"],
        cwd=repos.site_dir,
        stage="site",
        log_path=runlog.stage_log_path("site"),
    )
