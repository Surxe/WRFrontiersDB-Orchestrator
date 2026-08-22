#!/usr/bin/env python3
"""WRFrontiersDB-Orchestrator — patch-day pipeline driver.

preflight -> export -> parse -> (push) -> site, each gated by its SHOULD_ flag
and streamed to a per-stage log. Paths derive from WRF_ROOT; secrets overlay from
~dev/.config/wrf-orchestrator/secrets.env and reach sub-repos via the env.

Examples:
    python src/run.py --game-version 2026-08-22          # full patch day
    python src/run.py --should-parse true --should-build-site true \
                      --game-version 2026-08-22          # re-parse + rebuild only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make repo root importable (options_schema) and src/ importable (stages).
SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

from optionsconfig import init_options, ArgumentWriter  # noqa: E402

import config  # noqa: E402
import preflight  # noqa: E402
from logging_stream import RunLogger  # noqa: E402
from repos import Repos  # noqa: E402
from stages import export as export_stage  # noqa: E402
from stages import parse as parse_stage  # noqa: E402
from stages import site as site_stage  # noqa: E402


def main(args: argparse.Namespace) -> int:
    config.load_secrets()
    options = init_options(args=args, log_file=None)

    repos = Repos(wrf_root=options.wrf_root, repos_dir=options.repos_dir)

    interactive = sys.stdin.isatty()
    try:
        game_version = preflight.confirm_game_version(options, interactive=interactive)
        preflight.validate(options, repos, game_version=game_version)
    except preflight.PreflightError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    runlog = RunLogger(options.log_dir)
    runlog.banner(f"WRFrontiersDB-Orchestrator — patch {game_version}")

    stages = [
        ("EXPORT", options.should_export, export_stage.run),
        ("PARSE", options.should_parse or options.should_push_data, parse_stage.run),
        ("SITE", options.should_build_site, site_stage.run),
    ]

    for name, enabled, fn in stages:
        if not enabled:
            print(f"[{name}] skipped", flush=True)
            continue
        runlog.banner(name)
        rc = fn(options, repos, game_version, runlog)
        if rc != 0:
            runlog.banner(f"{name} FAILED (exit {rc}) — see its stage log")
            return 1

    runlog.banner("Pipeline complete")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WRFrontiersDB-Orchestrator — patch-day pipeline driver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ArgumentWriter().add_arguments(parser)
    sys.exit(main(parser.parse_args()))
