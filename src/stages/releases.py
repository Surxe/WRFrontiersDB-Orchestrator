"""Releases stage — record newly-released robots into the data repo's curated file.

Runs after PARSE (so `current/` holds this patch's roster and the data repo is a
fresh, PAT-configured checkout). Diffs the roster against
`curated/robot_release_dates.json`, appends/backfills any newly-released robot
with the fields the pipeline can source, and commits + pushes that one file.

Sourceable fields:
  * release_date            <- the in-house version id (game_version / version.txt)
  * manifest_id             <- data/steam-download/manifest.txt (the built GID)
  * patch_released_at_utc   <- Steam PICS `timeupdated`, but only when the public
                               manifest GID matches the built one (probe state
                               first, else a live anonymous query); else null.
Article-derived fields (release_context, source_article_ids) are left for the
news-scraper / a human.

Advisory: a failure here never fails the pipeline (the data is already published
and SITE should still build), but it is logged loudly.
"""

from __future__ import annotations

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


def _resolve_patch_utc(manifest_id: str | None, tee) -> str | None:
    """UTC publish time for `manifest_id`, only if a source's GID matches it.

    Tries the probe's state file first (offline; written when the probe triggered
    this run), then a live anonymous PICS query. Returns None (never a guess) if
    neither confirms the same GID as the built manifest.
    """
    if not manifest_id:
        return None

    state = probe.load_state(probe.DEFAULT_STATE)
    if state.get("last_gid") == manifest_id and state.get("timeupdated"):
        return releases.epoch_to_utc_iso(state["timeupdated"])

    try:
        pics = probe.query_pics(retries=2, delay=3)
    except probe.ProbeError as exc:
        tee(f"[releases] PICS lookup for patch time failed (non-fatal): {exc}")
        return None
    if pics.get("gid") == manifest_id:
        return releases.epoch_to_utc_iso(pics.get("timeupdated"))
    tee(f"[releases] public manifest {pics.get('gid')} != built {manifest_id}; "
        "leaving patch_released_at_utc null (processing a non-current build?)")
    return None


def run(options, repos: Repos, game_version: str, runlog: RunLogger) -> int:
    log_path = runlog.stage_log_path("releases")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    def tee(line: str) -> None:
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    manifest_id = _read_manifest_id(repos)
    patch_utc = _resolve_patch_utc(manifest_id, tee)
    tee(f"[releases] version={game_version} manifest_id={manifest_id} patch_utc={patch_utc}")

    try:
        result = releases.record_releases(
            data_dir=repos.data_dir,
            version=game_version,
            manifest_id=manifest_id,
            patch_released_at_utc=patch_utc,
            write=True,
        )
    except releases.ReleasesError as exc:
        tee(f"[releases] recording failed (non-fatal): {exc}")
        return 0

    if result.orphaned_refs:
        tee(f"[releases] WARNING recorded refs no longer in roster: {result.orphaned_refs} — "
            "WRF does not retire robots; likely a slug rename. Verify.")
    for b in result.backfilled:
        tee(f"[releases] backfilled {b['name']} ({b['id']}): {b['filled']}")

    if not result.new_bots:
        if not result.changed:
            tee("[releases] no new robots; curated file already lists the roster.")
            return 0
    else:
        tee(f"[releases] NEW ROBOT(S) RELEASED in {game_version}"
            + (" [SUSPECTED RENAME — verify]" if result.suspected_rename else ""))
        for b in result.new_bots:
            tee(f"[releases]   - {b['name']} ({b['id']}, {b['character_type']})")

    if not result.changed:
        return 0

    # Publish the one file. The Parser's push reclones each run, so an uncommitted
    # edit would be discarded — this commit + push is the only git operation.
    if not (options.should_push_data and options.gh_data_repo_pat):
        tee("[releases] curated file updated locally; not pushed "
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
        tee(f"[releases] committed + pushed curated/robot_release_dates.json to "
            f"{options.target_branch}.")
    except releases.ReleasesError as exc:
        tee(f"[releases] push of curated file failed (non-fatal): {exc}")

    return 0
