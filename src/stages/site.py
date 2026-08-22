"""Site stage — builds WRFrontiersDB-Site (Astro) against the updated data repo.

The site consumes WRFrontiersDB-Data via its in-repo symlink, so once the Parser
has pushed/updated that checkout, a plain production build bakes in the new data.
"""

from __future__ import annotations

from logging_stream import RunLogger, run_streamed
from repos import Repos


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    return run_streamed(
        ["npm", "run", "build"],
        cwd=repos.site_dir,
        stage="site",
        log_path=runlog.stage_log_path("site"),
    )
