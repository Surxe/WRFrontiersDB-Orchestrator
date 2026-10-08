"""Index stage: rebuild the data repo's `index/` from the freshly pushed `current/`.

Runs after PARSE. The data repo owns the logic (`tools/wrfdb_data`, imported from
the checkout); this stage feeds it what only the box knows, reports the results,
and commits + pushes `index/`.

* Slug map (`index/slug_map.json`): always rebuilt. Unreadable data or a slug
  collision fails the stage, since SITE / SITE-DEPLOY would otherwise publish
  against a stale or broken map. An object that should have a page but got no
  slug is a warning.
* Build codes (`index/build_codes.json` + `build_code_vectors.json`, the registry
  behind the Site's short `/models?a=<code>` links): always rebuilt, and fatal like
  the slug map. The registry is append-only; a data change that would change what
  a published code means (a module losing a socket or a fit, gaining a required
  socket, disappearing) is an error, nothing is written, and the stage fails so
  SITE / SITE-DEPLOY never ship against it. Fix it in the data repo's
  tools/wrfdb_data/build_codes.py (rules in its docs/build-codes.md).
* Nicknames (`index/nicknames.json`, pilot first names and chassis `<robot> Legs`
  for the Discord bot's lookups): always rebuilt. Two premium pilots sharing a
  first name is logged as an error (neither gets it) but doesn't fail the stage:
  nicknames are a lookup convenience, not worth holding back the Site.
* Aliases (`index/aliases.json`, robot part names like `Wyrm Chassis`) and
  abbreviations (`index/abbreviations.json`, `r` -> `relic`): always rebuilt, and
  advisory like nicknames. An abbreviation whose full form no name has any more
  is a warning (update the data repo's tools/wrfdb_data/abbreviations.py).
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
    build_code_changes = _build_build_codes(repos, tools)
    if build_code_changes is None:
        return 1
    changes += build_code_changes
    changes += _build_nicknames(repos, tools)
    changes += _build_aliases(repos, tools)
    changes += _build_abbreviations(repos, tools)
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


def _build_build_codes(repos: Repos, tools) -> list[str] | None:
    """Rebuild index/build_codes.json + vectors. Returns the commit-message bits, or None on failure."""
    try:
        result = tools.build_codes.write_build_codes(repos.data_dir)
    except tools.paths.DataRepoError as exc:
        logger.error(f"build codes failed: {exc}")
        return None

    for error in result.errors:
        logger.error(f"build codes: {error}")
    if result.errors:
        logger.error("build codes: not written; this data change would change what published "
                     "codes mean. See the data repo's docs/build-codes.md.")
        return None
    for addition in result.appended:
        logger.info(f"build codes: appended {addition}")
    logger.info(f"build codes: {len(result.registry['modules'])} modules, "
                + ("changed" if result.changed else "unchanged"))
    return ["update build codes"] if result.changed else []


def _build_nicknames(repos: Repos, tools) -> list[str]:
    """Rebuild index/nicknames.json. Errors are logged, never raised."""
    try:
        result = tools.nicknames.write_nicknames(repos.data_dir)
    except tools.paths.DataRepoError as exc:
        logger.error(f"nicknames failed (non-fatal): {exc}")
        return []

    for nickname, ids in result.conflicts:
        logger.error(f"nickname conflict: premium pilots share '{nickname}' ({', '.join(ids)}); "
                     "none of them gets it. Add a rule in the data repo's tools/wrfdb_data/nicknames.py")
    for nickname, ids in result.ambiguous:
        logger.info(f"nickname '{nickname}' is shared by common pilots only ({', '.join(ids)}); skipped")
    logger.info(f"nicknames: {len(result.nicknames)}, " + ("changed" if result.changed else "unchanged"))
    return ["update nicknames"] if result.changed else []


def _build_aliases(repos: Repos, tools) -> list[str]:
    """Rebuild index/aliases.json. Errors are logged, never raised."""
    try:
        result = tools.robot_parts.write_aliases(repos.data_dir)
    except tools.paths.DataRepoError as exc:
        logger.error(f"aliases failed (non-fatal): {exc}")
        return []
    logger.info(f"aliases: {len(result.aliases)}, " + ("changed" if result.changed else "unchanged"))
    return ["update aliases"] if result.changed else []


def _build_abbreviations(repos: Repos, tools) -> list[str]:
    """Rebuild index/abbreviations.json. Errors are logged, never raised."""
    try:
        result = tools.abbreviations.write_abbreviations(repos.data_dir)
    except tools.paths.DataRepoError as exc:
        logger.error(f"abbreviations failed (non-fatal): {exc}")
        return []
    for short, full in result.unused:
        logger.warning(f"abbreviation '{short}' -> '{full}': no published name has it any more; "
                       "update the data repo's tools/wrfdb_data/abbreviations.py")
    logger.info(f"abbreviations: {len(result.abbreviations)}, "
                + ("changed" if result.changed else "unchanged"))
    return ["update abbreviations"] if result.changed else []


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
