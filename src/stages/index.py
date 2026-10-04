"""Index stage: rebuild the data repo's `index/` from the freshly pushed `current/`.

Runs after PARSE. The data repo owns the logic (`tools/wrfdb_data`, imported from
the checkout); this stage feeds it what only the box knows, reports the results,
and commits + pushes `index/`.

* Slug map (`index/slug_map.json`): always rebuilt. Unreadable data or a slug
  collision fails the stage, since SITE / SITE-DEPLOY would otherwise publish
  against a stale or broken map. An object that should have a page but got no
  slug is a warning.
* Robot release dates + patch manifests: advisory. A failure is logged as an
  error but doesn't fail the pipeline.

  Sourceable fields:
    * release_date / version  <- the in-house version id (game_version)
    * manifest_id             <- data/steam-download/manifest.txt (the built GID)
    * buildid,
      patch_released_at_utc   <- the probe's state file (`buildid`, `timeupdated`),
                                 only for its `last_gid`; null otherwise (no live
                                 Steam lookup; offline only).
  The probe's GID->version registry is merged in too, so every patch the box has
  versioned is published, not just the one built now. Article-derived fields
  (release_context, source_article_ids) are left for the news-scraper / a human.
* Publish: one commit for `index/`. A failed push fails the stage: the Site's CI
  reads `index/slug_map.json` from the data repo's main branch.
"""

from __future__ import annotations

from loguru import logger

import data_repo
import probe
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
    included under `game_version` (a registry version wins if they disagree: it is
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
        latest["patch_released_at_utc"] = probe.epoch_to_utc_iso(state.get("timeupdated"))
    return builds


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    with runlog.stage_sink("index"):
        return _run(options, repos, game_version)


def _run(options, repos: Repos, game_version: str) -> int:
    try:
        tools = data_repo.import_tools(repos.data_dir)
    except ImportError as exc:
        logger.error(f"cannot import wrfdb_data from {repos.data_dir / data_repo.TOOLS_REL}: {exc}")
        return 1

    changes = _build_slug_map(repos, tools)
    if changes is None:
        return 1
    changes += _record_releases(repos, game_version, tools)

    if not changes:
        logger.info("index/ unchanged; nothing to publish.")
        return 0
    if not (options.should_push_data and options.gh_data_repo_pat):
        logger.info("index/ updated locally; not pushed (push_data off or no PAT). "
                    "It will be discarded on the next reclone.")
        return 0

    message = (f"Update index for {game_version}: " + ", ".join(changes)
               + "\n\nindex/ rebuilt by the orchestrator INDEX stage (tools/wrfdb_data).")
    try:
        data_repo.publish(repos.data_dir, [tools.paths.INDEX_REL], pat=options.gh_data_repo_pat,
                          branch=options.target_branch, message=message)
    except data_repo.DataRepoGitError as exc:
        logger.error(f"push of index/ failed: {exc}")
        return 1
    logger.info(f"committed + pushed index/ to {options.target_branch}.")
    return 0


def _build_slug_map(repos: Repos, tools) -> list[str] | None:
    """Rebuild index/slug_map.json. Returns the commit-message bits, or None on failure."""
    try:
        result = tools.slug_map.write_slug_map(repos.data_dir)
    except tools.paths.DataRepoError as exc:
        logger.error(f"slug map failed: {exc}")
        return None

    for object_id, reason in result.skipped:
        logger.warning(f"no slug for {object_id} ({reason}); it gets no page link")
    for object_type, slug, ids in result.collisions:
        logger.error(f"slug collision: {object_type} '{slug}' is used by {', '.join(ids)}")
    if result.collisions:
        return None

    logger.info(f"slug map: {len(result.slug_map)} slugs, "
                + ("changed" if result.changed else "unchanged"))
    return ["update slug map"] if result.changed else []


def _record_releases(repos: Repos, game_version: str, tools) -> list[str]:
    """Record new robots + known builds. Advisory: errors are logged, never raised."""
    manifest_id = _read_manifest_id(repos)
    builds = _collect_builds(manifest_id, game_version)
    logger.info(f"version={game_version} manifest_id={manifest_id} known_builds={len(builds)}")

    try:
        result = tools.releases.record_releases(repos.data_dir, game_version, manifest_id)
        patches = tools.releases.record_patch_manifests(repos.data_dir, builds)
    except tools.paths.DataRepoError as exc:
        logger.error(f"recording releases failed (non-fatal): {exc}")
        return []

    if result.orphaned_refs:
        logger.warning(
            f"recorded refs no longer in roster: {result.orphaned_refs}. "
            "WRF does not retire robots; likely a rename. Verify."
        )
    for b in result.backfilled:
        logger.info(f"settled pending {b['name']} ({b['id']}): {b['filled']}")
    for gid, key, recorded, offered in patches.conflicts:
        logger.warning(f"patch {gid} {key}: recorded {recorded!r}, build says {offered!r}; "
                       "keeping recorded")
    if patches.changed:
        logger.info(f"patch manifests: added {patches.added}, filled {patches.filled}")

    if result.new_bots:
        logger.info(f"NEW ROBOT(S) RELEASED in {game_version}"
                    + (" [SUSPECTED RENAME - verify]" if result.suspected_rename else ""))
        for b in result.new_bots:
            logger.info(f"  - {b['name']} ({b['id']}, {b['character_type']})")
    elif not result.changed:
        logger.info("no new robots; the release dates already list the roster.")

    bits = []
    if result.new_bots:
        bits.append(f"add {len(result.new_bots)} newly-released robot(s)")
    if result.backfilled:
        bits.append(f"settle {len(result.backfilled)} pending robot(s)")
    if patches.added:
        bits.append(f"add {len(patches.added)} patch manifest(s)")
    if patches.filled:
        bits.append(f"fill {len(patches.filled)} patch manifest(s)")
    return bits
