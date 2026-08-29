"""Export stage — drives WRFrontiers-Exporter.

Steam download -> mapper (.usmap) -> BatchExport (JSON). All paths are derived
from WRF_ROOT; Steam credentials are passed via the environment, not argv.
The mapper sub-step runs as dev under gamescope headless, which needs the GPU
runtime dir and /usr/games on PATH (validated 2026-08-22).
"""

from __future__ import annotations

import os
from pathlib import Path

from logging_stream import RunLogger, run_streamed
from repos import Repos
from stages import b


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    py = repos.venv_python(repos.exporter_dir)
    mapper_file = repos.mapper_file(game_version)

    cmd = [
        str(py), "src/run.py",
        "--log-level", options.log_level,
        "--should-download-dependencies", b(options.should_download_dependencies),
        "--should-download-steam-game", b(options.should_download_steam_game),
        "--should-get-mapper", b(options.should_get_mapper),
        "--should-batch-export", b(options.should_batch_export),
        "--should-export-textures", b(options.should_export_textures),
        "--force-download-dependencies", b(options.force_download_dependencies),
        "--force-steam-download", b(options.force_steam_download),
        "--force-get-mapper", b(options.force_get_mapper),
        "--force-export", b(options.force_export),
        "--headless", b(options.headless),
        "--manifest-id", options.manifest_id,
        "--steam-game-download-dir", str(repos.steam_download_dir),
        "--output-mapper-file", str(mapper_file),
        "--output-data-dir", str(repos.exports_dir),
        "--wine-prefix", str(repos.wine_prefix),
        "--proton-path", str(repos.proton_path),
    ]

    # Secrets via env (never argv, so they don't show up in `ps`).
    env: dict[str, str] = {}
    if options.steam_username:
        env["STEAM_USERNAME"] = options.steam_username
    if options.steam_password:
        env["STEAM_PASSWORD"] = options.steam_password

    # Headless mapper needs the GPU runtime dir, gamescope on PATH, and no
    # inherited X DISPLAY (pure headless as dev).
    if options.should_get_mapper and options.headless:
        env["XDG_RUNTIME_DIR"] = "/run/user/1001"
        env["PATH"] = "/usr/games:" + os.environ.get("PATH", "")
        env.pop("DISPLAY", None)

    return run_streamed(
        cmd,
        cwd=repos.exporter_dir,
        stage="export",
        log_path=runlog.stage_log_path("export"),
        env=env,
    )
