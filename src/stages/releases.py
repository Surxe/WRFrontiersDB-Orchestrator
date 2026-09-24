"""Releases stage — record newly-released robots into the data repo's curated file.

Runs after PARSE (so `current/` holds this patch's roster and the data repo is a
fresh, PAT-configured checkout). Diffs the roster against
`curated/robot_release_dates.json`, appends/backfills any newly-released robot
with the fields the pipeline can source, and commits + pushes that one file.

Sourceable fields:
  * release_date            <- the in-house version id (game_version / version.txt)
  * manifest_id             <- data/steam-download/manifest.txt (the built GID)
  * patch_released_at_utc   <- the probe's state file `timeupdated`, but only when
                               its `last_gid` matches the built manifest; else null
                               (no live Steam lookup — offline only).
Article-derived fields (release_context, source_article_ids) are left for the
news-scraper / a human.

Advisory: a failure here never fails the pipeline (the data is already published
and SITE should still build), but it is logged loudly.
"""

from __future__ import annotations

from loguru import logger

import probe
import releases
from logging_stream import RunLogger
from repos import Repos


def _read_manifest_id(repos: Repos) -> str | None:
    path = repos.steam_download_dir / "manifest.txt"
    try:
        gid = path.read_text(encoding="utf-8").strip()
        return gid or None
    except OSError:
        return None


def _resolve_patch_utc(manifest_id: str | None) -> str | None:
    """UTC publish time for `manifest_id` from the probe's state file (offline).

    The probe records `last_gid` + `timeupdated` for the public build it detected.
    We use its time only when that GID matches the manifest we actually built; no
    live Steam lookup, so an unmatched or absent state simply yields None (never a
    guess). Wiring the probe as the pipeline trigger is what keeps this populated.
    """
    if not manifest_id:
        return None
    state = probe.load_state(probe.DEFAULT_STATE)
    if state.get("last_gid") == manifest_id and state.get("timeupdated"):
        return releases.epoch_to_utc_iso(state["timeupdated"])
    return None


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    with runlog.stage_sink("releases"):
        return _run(options, repos, game_version)


def _run(options, repos: Repos, game_version: str) -> int:
    manifest_id = _read_manifest_id(repos)
    patch_utc = _resolve_patch_utc(manifest_id)
    logger.info(f"version={game_version} manifest_id={manifest_id} patch_utc={patch_utc}")

    try:
        result = releases.record_releases(
            data_dir=repos.data_dir,
            version=game_version,
            manifest_id=manifest_id,
            patch_released_at_utc=patch_utc,
            write=True,
        )
    except releases.ReleasesError as exc:
        logger.error(f"recording failed (non-fatal): {exc}")
        return 0

    if result.orphaned_refs:
        logger.warning(
            f"recorded refs no longer in roster: {result.orphaned_refs} — "
            "WRF does not retire robots; likely a slug rename. Verify."
        )
    for b in result.backfilled:
        logger.info(f"backfilled {b['name']} ({b['id']}): {b['filled']}")

    if not result.new_bots:
        if not result.changed:
            logger.info("no new robots; curated file already lists the roster.")
            return 0
    else:
        logger.info(f"NEW ROBOT(S) RELEASED in {game_version}"
                    + (" [SUSPECTED RENAME — verify]" if result.suspected_rename else ""))
        for b in result.new_bots:
            logger.info(f"  - {b['name']} ({b['id']}, {b['character_type']})")

    if not result.changed:
        return 0

    # Publish the one file. The Parser's push reclones each run, so an uncommitted
    # edit would be discarded — this commit + push is the only git operation.
    if not (options.should_push_data and options.gh_data_repo_pat):
        logger.info("curated file updated locally; not pushed "
                    "(push_data off or no PAT). It will be discarded on the next reclone.")
        return 0

    n_new, n_fill = len(result.new_bots), len(result.backfilled)
    bits = []
    if n_new:
        bits.append(f"add {n_new} newly-released robot(s)")
    if n_fill:
        bits.append(f"backfill {n_fill} ref(s)")
    message = (f"Record robot releases for {game_version}: " + ", ".join(bits)
               + "\n\ncuration/robot_release_dates.json auto-updated by the orchestrator "
                 "RELEASES stage (roster diff).")
    try:
        releases.publish_curated(
            repos.data_dir, pat=options.gh_data_repo_pat,
            branch=options.target_branch, message=message,
        )
        logger.info(f"committed + pushed curated/robot_release_dates.json to "
                    f"{options.target_branch}.")
    except releases.ReleasesError as exc:
        logger.error(f"push of curated file failed (non-fatal): {exc}")

    return 0
