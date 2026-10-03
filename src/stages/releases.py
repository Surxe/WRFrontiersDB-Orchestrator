"""Releases stage — record newly-released robots and this patch's manifest.

Runs after PARSE (so `current/` holds this patch's roster and the data repo is a
fresh, PAT-configured checkout). Diffs the roster against
`curated/robot_release_dates.json` (keyed by `virtual_bot_ref`), adds/settles any
newly-released robot, merges every known build into `curated/patch_manifests.json`
(keyed by manifest GID), and commits + pushes those two files.

Sourceable fields:
  * release_date / version  <- the in-house version id (game_version / version.txt)
  * manifest_id             <- data/steam-download/manifest.txt (the built GID)
  * buildid,
    patch_released_at_utc   <- the probe's state file (`buildid`, `timeupdated`),
                               only for its `last_gid`; null otherwise (no live
                               Steam lookup — offline only).
The probe's GID->version registry is merged in too, so every patch the box has
versioned is published, not just the one built now. Article-derived fields
(release_context, source_article_ids) are left for the news-scraper / a human.

Advisory: a failure here never fails the pipeline (the data is already published
and SITE should still build), but it is logged loudly.
"""

from __future__ import annotations

from loguru import logger

import probe
import releases
import versioning
from logging_stream import RunLogger
from repos import Repos


def _read_manifest_id(repos: Repos) -> str | None:
    path = repos.steam_download_dir / "manifest.txt"
    try:
        gid = path.read_text(encoding="utf-8").strip()
        return gid or None
    except OSError:
        return None


def _collect_builds(manifest_id: str | None, game_version: str) -> dict[str, dict]:
    """Every build known offline: {gid: {version, buildid, patch_released_at_utc}}.

    The probe's registry gives GID->version for each patch it has versioned; its
    state file adds buildid + publish time for the latest GID. The built manifest is
    included under `game_version` (a registry version wins if they disagree — it is
    what the probe handed the pipeline).
    """
    registry = versioning.load_registry(probe.registry_path(probe.DEFAULT_STATE))
    builds = {gid: {"version": version} for gid, version in registry.items()}

    if manifest_id:
        build = builds.setdefault(manifest_id, {"version": game_version})
        if build["version"] != game_version:
            logger.warning(f"manifest {manifest_id} is {build['version']} in the probe registry "
                           f"but this run is {game_version}; recording the registry version")

    state = probe.load_state(probe.DEFAULT_STATE)
    latest = builds.get(state.get("last_gid"))
    if latest is not None:
        latest["buildid"] = state.get("buildid")
        latest["patch_released_at_utc"] = releases.epoch_to_utc_iso(state.get("timeupdated"))
    return builds


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    with runlog.stage_sink("releases"):
        return _run(options, repos, game_version)


def _run(options, repos: Repos, game_version: str) -> int:
    manifest_id = _read_manifest_id(repos)
    builds = _collect_builds(manifest_id, game_version)
    logger.info(f"version={game_version} manifest_id={manifest_id} known_builds={len(builds)}")

    try:
        result = releases.record_releases(
            data_dir=repos.data_dir, version=game_version, manifest_id=manifest_id, write=True,
        )
        patches = releases.record_patch_manifests(repos.data_dir, builds, write=True)
    except releases.ReleasesError as exc:
        logger.error(f"recording failed (non-fatal): {exc}")
        return 0

    if result.orphaned_refs:
        logger.warning(
            f"recorded refs no longer in roster: {result.orphaned_refs} — "
            "WRF does not retire robots; likely a slug rename. Verify."
        )
    for b in result.backfilled:
        logger.info(f"settled pending {b['name']} ({b['id']}): {b['filled']}")
    if patches.changed:
        logger.info(f"patch manifests: added {patches.added}, filled {patches.filled}")

    if result.new_bots:
        logger.info(f"NEW ROBOT(S) RELEASED in {game_version}"
                    + (" [SUSPECTED RENAME — verify]" if result.suspected_rename else ""))
        for b in result.new_bots:
            logger.info(f"  - {b['name']} ({b['id']}, {b['character_type']})")
    elif not result.changed:
        logger.info("no new robots; curated file already lists the roster.")

    if not (result.changed or patches.changed):
        return 0

    # Publish the files. The Parser's push reclones each run, so an uncommitted edit
    # would be discarded — this commit + push is the only git operation.
    if not (options.should_push_data and options.gh_data_repo_pat):
        logger.info("curated files updated locally; not pushed "
                    "(push_data off or no PAT). They will be discarded on the next reclone.")
        return 0

    bits = []
    if result.new_bots:
        bits.append(f"add {len(result.new_bots)} newly-released robot(s)")
    if result.backfilled:
        bits.append(f"settle {len(result.backfilled)} pending robot(s)")
    if patches.added:
        bits.append(f"add {len(patches.added)} patch manifest(s)")
    if patches.filled:
        bits.append(f"fill {len(patches.filled)} patch manifest(s)")
    message = (f"Record releases for {game_version}: " + ", ".join(bits)
               + "\n\ncurated/robot_release_dates.json + curated/patch_manifests.json "
                 "auto-updated by the orchestrator RELEASES stage.")
    try:
        releases.publish_curated(
            repos.data_dir, pat=options.gh_data_repo_pat,
            branch=options.target_branch, message=message,
        )
        logger.info(f"committed + pushed curated files to {options.target_branch}.")
    except releases.ReleasesError as exc:
        logger.error(f"push of curated files failed (non-fatal): {exc}")

    return 0
