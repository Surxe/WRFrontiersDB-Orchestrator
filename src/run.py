#!/usr/bin/env python3
"""WRFrontiersDB-Orchestrator — patch-day pipeline driver.

preflight -> export -> parse -> (push) -> site, each gated by its SHOULD_ flag
and streamed to a per-stage log. Paths derive from WRF_ROOT; secrets overlay from
~dev/.config/wrf-orchestrator/secrets.env and reach sub-repos via the env.

Examples:
    python src/run.py --patch-day --game-version 2026-08-22        # full patch day
    python src/run.py --force-patch-day --game-version 2026-08-22  # + force re-do
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
from stages import releases as releases_stage  # noqa: E402
from stages import site as site_stage  # noqa: E402
from stages import site_deploy as site_deploy_stage  # noqa: E402


# --patch-day: the whole pipeline — every stage gate and every export sub-step.
# Equivalent to running with no SHOULD_ flags (optionsconfig's "all-false ->
# all-true" rule), but named and explicit. The sub-steps must be set too: turning
# on only the stage gates would leave the sub-steps at their False default, so
# they wouldn't hit the all-false rule and the EXPORT stage would run empty.
_PATCH_DAY_FLAGS = (
    "should_export",
    "should_parse",
    "should_push_data",
    "should_detect_releases",
    "should_build_site",
    "should_deploy_site",
    "should_download_dependencies",
    "should_download_steam_game",
    "should_get_mapper",
    "should_batch_export",
    "should_export_textures",
)

# --force-patch-day: a patch day that re-does work whose output already exists.
# Layered on top of the patch-day preset.
_FORCE_FLAGS = (
    "force_download_dependencies",
    "force_steam_download",
    "force_get_mapper",
    "force_export",
)


def _apply_preset(args: argparse.Namespace, flags: tuple[str, ...]) -> None:
    """Turn on each flag the user left unset, so an explicit CLI value wins."""
    for attr in flags:
        if getattr(args, attr, None) is None:
            setattr(args, attr, True)


def main(args: argparse.Namespace) -> int:
    config.load_secrets()

    # Preset shortcuts, each only filling flags left unset (explicit --should-*/
    # --force-* on the CLI still wins). --force-patch-day implies --patch-day.
    if getattr(args, "patch_day", False) or getattr(args, "force_patch_day", False):
        _apply_preset(args, _PATCH_DAY_FLAGS)
    if getattr(args, "force_patch_day", False):
        _apply_preset(args, _FORCE_FLAGS)

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
        ("RELEASES", options.should_detect_releases, releases_stage.run),
        ("SITE", options.should_build_site, site_stage.run),
        ("SITE-DEPLOY", options.should_deploy_site, site_deploy_stage.run),
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
    # Prune old patch versions (keep 2 most recent), keeping steam-download static.
    repos.prune_old_versions(keep=2)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WRFrontiersDB-Orchestrator — patch-day pipeline driver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--patch-day",
        action="store_true",
        help="Shorthand for a full patch day: enables every stage and every "
             "export sub-step (export, parse, push, build site, plus all the "
             "Exporter sub-steps). Same as passing no --should-* flags, but "
             "explicit. Explicit --should-* flags still override the preset.",
    )
    parser.add_argument(
        "--force-patch-day",
        action="store_true",
        help="Like --patch-day, but also forces every stage to re-do work whose "
             "output already exists (--force-download-dependencies, "
             "--force-steam-download, --force-get-mapper, --force-export). "
             "Explicit --should-*/--force-* flags still override the preset.",
    )
    ArgumentWriter().add_arguments(parser)
    sys.exit(main(parser.parse_args()))
