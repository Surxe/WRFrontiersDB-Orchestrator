"""Site stage — builds WRFrontiersDB-Site (Astro) against the updated data repo.

The site consumes WRFrontiersDB-Data via its in-repo symlink, so once the Parser
has pushed/updated that checkout, a plain production build bakes in the new data.

We `npm ci` first so a dependency the site added since the last run is present:
the checkout's node_modules is otherwise stale, and the build fails to resolve
the new import (CI installs deps every run, so it never hit this). `npm ci` is
lockfile-exact and matches the committed package-lock.json.

`npm run build` does NOT chain slug generation, and the build errors without it,
so we run `build:slugs` next — exactly what CI does. The generated slug output
is a build artifact and is not committed (CI regenerates it too).

This is a local pre-flight: a passing build here is the gate before SITE-DEPLOY
dispatches the Pages workflow, so a build break stops the pipeline cheaply.
"""

from __future__ import annotations

from logging_stream import RunLogger, run_streamed
from repos import Repos


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    rc = run_streamed(
        ["npm", "ci"],
        cwd=repos.site_dir,
        stage="site-install",
        log_path=runlog.stage_log_path("site-install"),
    )
    if rc != 0:
        return rc

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
