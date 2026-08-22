"""Parse stage — drives WRFrontiersDB-Parser.

Reads the Exporter's JSON (EXPORT_DIR == the Exporter's OUTPUT_DATA_DIR, both
derived from WRF_ROOT), parses it, and optionally pushes the result to the data
repo. The data-repo PAT is passed via the environment, not argv.
"""

from __future__ import annotations

from logging_stream import RunLogger, run_streamed
from repos import Repos
from stages import b


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    py = repos.venv_python(repos.parser_dir)

    cmd = [
        str(py), "src/run.py",
        "--log-level", options.log_level,
        "--should-parse", b(options.should_parse),
        "--game-name", "WRFrontiers",
        "--export-dir", str(repos.exports_dir),
        "--output-dir", str(repos.parsed_dir),
        "--texture-output-dir", str(repos.textures_dir),
        "--should-push-data", b(options.should_push_data),
        "--game-version", game_version,
        "--target-branch", options.target_branch,
        "--should-reclone", "true",
        "--gh-data-repo-dir", str(repos.data_dir),
        "--should-push-textures", b(options.should_export_textures),
    ]

    env: dict[str, str] = {}
    if options.gh_data_repo_pat:
        env["GH_DATA_REPO_PAT"] = options.gh_data_repo_pat

    return run_streamed(
        cmd,
        cwd=repos.parser_dir,
        stage="parse",
        log_path=runlog.stage_log_path("parse"),
        env=env,
    )
