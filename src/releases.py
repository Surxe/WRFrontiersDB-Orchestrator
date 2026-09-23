#!/usr/bin/env python3
"""Record newly-released robots into the data repo's curated release-date file.

A robot is a `VirtualBot` in the published data iff its modules are used by a
factory preset, and the studio only ships an obtainable robot with one — so a new
id appearing in `current/Objects/VirtualBot.json` **is** the release signal (no
`ProductionStatus` check needed; the Parser gates on the factory preset, which
agrees 1:1 with `Ready` core modules).

The store *is* the dedup source: `curated/robot_release_dates.json` already lists
every robot (with `virtual_bot_ref` = `OBJID_VirtualBot::<slug>`). A roster id
whose ref is already in that file is already recorded, so detection is a pure
file comparison — **no git diff against the previous patch, no separate state
file**. For each roster id not yet recorded we either:

  * backfill the ref onto a pre-recorded entry whose `virtual_bot_ref` is null
    (a robot the news-scraper logged before it hit the datamined roster — e.g.
    Angler), filling only the fields still null so hand-curated data is never
    overwritten; or
  * append a fresh entry (Mechs -> `robots[]`, Titans -> `titans[]`).

An auto-added entry can only carry what the pipeline can source: the in-house
version id (`release_date`), the Steam depot `manifest_id`, and the build's UTC
publish time (`patch_released_at_utc`). Article-derived fields (`release_context`,
`source_article_ids`) are left null/[] for the news-scraper or a human to fill.

Publishing the edit is a commit + push to the data repo (`publish_curated`),
because the Parser's push reclones a fresh checkout each run — an uncommitted
local edit would be wiped before it ever reached the remote. That push is the
only git operation; it is output, not comparison.

Id stability: the id is `slugify(<localized name>)`, so a rename is rare. If a
recorded ref's slug ever leaves the roster the same run a new id appears, it is
reported as `orphaned` (a rename suspect) — we still record the new bot, but a
human should confirm it is a real release. WRF does not retire robots, so an
orphan is itself worth eyeballing. Relic variants are distinct ids and distinct
robots, recorded like any other.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from loguru import logger

# --- Exit codes --------------------------------------------------------------
EXIT_NO_CHANGE = 0
EXIT_NEW_RELEASE = 20
EXIT_ERROR = 1

# Paths within a WRFrontiersDB-Data checkout.
CURATED_REL = Path("curated") / "robot_release_dates.json"
ROSTER_REL = Path("current") / "Objects" / "VirtualBot.json"
VERSION_REL = Path("current") / "version.txt"

DATA_REPO_SLUG = "Surxe/WRFrontiersDB-Data"


class ReleasesError(RuntimeError):
    """The data repo could not be read/written; treat as no-op, nothing committed."""


@dataclass
class RecordResult:
    version: str
    new_bots: list[dict] = field(default_factory=list)      # freshly appended entries
    backfilled: list[dict] = field(default_factory=list)    # pre-recorded entries given a ref
    orphaned_refs: list[str] = field(default_factory=list)  # recorded refs no longer in roster
    changed: bool = False

    @property
    def suspected_rename(self) -> bool:
        # A new id appearing while a recorded ref's slug left the roster is more
        # likely a slug rename than a real simultaneous retire-and-release.
        return bool(self.new_bots and self.orphaned_refs)


# --- helpers -----------------------------------------------------------------
def virtual_bot_ref(bot_id: str) -> str:
    return f"OBJID_VirtualBot::{bot_id}"


def _norm_name(name: str) -> str:
    return (name or "").strip().lower()


def _bot_name(bot: dict) -> str:
    name = bot.get("name")
    if isinstance(name, dict):
        return name.get("en") or name.get("Key") or bot.get("id", "")
    return bot.get("id", "")


def read_roster(data_dir: Path) -> dict[str, dict]:
    """Return {bot_id: {'name', 'character_type'}} from the published roster."""
    path = Path(data_dir) / ROSTER_REL
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleasesError(f"VirtualBot roster not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ReleasesError(f"VirtualBot roster unreadable ({path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ReleasesError(f"VirtualBot roster is not an object: {path}")
    return {
        bot_id: {"name": _bot_name(bot), "character_type": bot.get("character_type")}
        for bot_id, bot in raw.items()
    }


def read_version(data_dir: Path) -> str:
    path = Path(data_dir) / VERSION_REL
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError) as exc:
        raise ReleasesError(f"version.txt unreadable ({path}): {exc}") from exc


def load_curated(data_dir: Path) -> dict:
    path = Path(data_dir) / CURATED_REL
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleasesError(f"curated file not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ReleasesError(f"curated file unreadable ({path}): {exc}") from exc
    if not isinstance(doc, dict) or "robots" not in doc or "titans" not in doc:
        raise ReleasesError(f"curated file missing robots/titans arrays: {path}")
    return doc


def save_curated(data_dir: Path, doc: dict) -> None:
    path = Path(data_dir) / CURATED_REL
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def epoch_to_utc_iso(ts) -> str | None:
    """Steam `timeupdated` (epoch seconds) -> '2026-09-15T07:16:00Z', or None."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return None


def _new_entry(name: str, bot_id: str, release_date, manifest_id, patch_released_at_utc) -> dict:
    """Build a curated entry in the file's field order for an auto-detected release."""
    return {
        "name": name,
        "virtual_bot_ref": virtual_bot_ref(bot_id),
        "release_date": release_date,
        "manifest_id": manifest_id,
        "patch_released_at_utc": patch_released_at_utc,
        "pre_launch": False,
        "release_context": None,      # article-derived; not sourceable here
        "source_article_ids": [],     # ditto
    }


# --- detection + recording ---------------------------------------------------
def record_releases(
    data_dir: Path,
    version: str,
    manifest_id: str | None,
    patch_released_at_utc: str | None,
    *,
    write: bool = True,
) -> RecordResult:
    """Diff the roster against the curated file; append/backfill; optionally save.

    Console-I/O free — the caller presents the result. Raises ReleasesError if the
    data repo can't be read. Only writes the file when something actually changed.
    """
    roster = read_roster(data_dir)
    doc = load_curated(data_dir)
    robots: list[dict] = doc["robots"]
    titans: list[dict] = doc["titans"]
    entries = robots + titans

    by_ref = {e["virtual_bot_ref"]: e for e in entries if e.get("virtual_bot_ref")}
    by_name: dict[str, dict] = {}
    for e in entries:
        by_name.setdefault(_norm_name(e.get("name", "")), e)

    result = RecordResult(version=version)

    for bot_id, meta in roster.items():
        ref = virtual_bot_ref(bot_id)
        if ref in by_ref:
            continue  # already recorded — the dedup that makes this idempotent

        name = meta["name"]
        existing = by_name.get(_norm_name(name))

        if existing is not None and not existing.get("virtual_bot_ref"):
            # Pre-recorded by the news-scraper before it hit the roster (e.g. Angler):
            # attach the ref and fill only still-null sourced fields (never clobber).
            existing["virtual_bot_ref"] = ref
            filled = {"virtual_bot_ref": ref}
            if existing.get("release_date") is None and version:
                existing["release_date"] = version
                filled["release_date"] = version
            if existing.get("manifest_id") is None and manifest_id:
                existing["manifest_id"] = manifest_id
                filled["manifest_id"] = manifest_id
            if existing.get("patch_released_at_utc") is None and patch_released_at_utc:
                existing["patch_released_at_utc"] = patch_released_at_utc
                filled["patch_released_at_utc"] = patch_released_at_utc
            result.backfilled.append({"id": bot_id, "name": name, "filled": filled})
            continue

        if existing is not None and existing.get("virtual_bot_ref"):
            # Name collides with a different, already-recorded robot — never happens
            # in WRF (names are unique); skip rather than duplicate, and flag it.
            logger.warning(
                "roster bot {!r} ({}) name-matches recorded ref {!r}; skipping to avoid "
                "a duplicate — verify these are not two different robots",
                name, ref, existing.get("virtual_bot_ref"),
            )
            continue

        # Genuinely new robot: append to the right array.
        entry = _new_entry(name, bot_id, version, manifest_id, patch_released_at_utc)
        (titans if meta.get("character_type") == "Titan" else robots).append(entry)
        result.new_bots.append({
            "id": bot_id, "name": name,
            "character_type": meta.get("character_type"),
            "release_date": version, "manifest_id": manifest_id,
            "patch_released_at_utc": patch_released_at_utc,
        })

    roster_refs = {virtual_bot_ref(b) for b in roster}
    result.orphaned_refs = sorted(
        e["virtual_bot_ref"] for e in entries
        if e.get("virtual_bot_ref") and e["virtual_bot_ref"] not in roster_refs
    )

    result.changed = bool(result.new_bots or result.backfilled)

    if result.changed:
        meta = doc.get("_meta")
        if isinstance(meta, dict):
            meta["robot_count"] = len(robots)
            meta["titan_count"] = len(titans)
            meta["robots_with_known_date"] = sum(1 for r in robots if r.get("release_date"))
            meta["generated"] = date.today().isoformat()
        if write:
            save_curated(data_dir, doc)

    return result


# --- publishing (the one git operation, for output not comparison) -----------
def _git(args: list[str], cwd: Path, pat: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    )
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        if pat:
            out = out.replace(pat, "********")
        raise ReleasesError(f"git {args[0]} failed: {out.strip()}")
    return proc


def publish_curated(data_dir: Path, *, pat: str, branch: str, message: str) -> None:
    """Commit the curated file and push it to the data repo on `branch`.

    Needed because the Parser's push reclones a fresh checkout each run, so an
    uncommitted local edit would be discarded before it ever reached the remote.
    """
    data_dir = Path(data_dir)
    url = f"https://{pat}@github.com/{DATA_REPO_SLUG}.git"
    _git(["config", "--local", "user.email", "orchestrator@example.com"], data_dir)
    _git(["config", "--local", "user.name", "Orchestrator"], data_dir)
    _git(["remote", "set-url", "origin", url], data_dir, pat=pat)
    _git(["add", str(CURATED_REL)], data_dir)
    status = _git(["status", "--porcelain", "--", str(CURATED_REL)], data_dir)
    if not status.stdout.strip():
        logger.info("curated file unchanged in git; nothing to push")
        return
    _git(["commit", "-m", message], data_dir)
    _git(["push", "origin", branch], data_dir, pat=pat)


# --- CLI ---------------------------------------------------------------------
def _configure_logging() -> None:
    level = os.environ.get("WRF_RELEASES_LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | releases | {message}",
    )


def emit(event: dict) -> None:
    """One machine-readable JSON line to stdout (loguru logs go to stderr)."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record newly-released WRF robots into the data repo's curated file.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("WRF_DATA_DIR", "/srv/dev/repos/WRFrontiersDB-Data")),
        help="WRFrontiersDB-Data checkout (default: /srv/dev/repos/WRFrontiersDB-Data or $WRF_DATA_DIR).",
    )
    parser.add_argument("--version", help="in-house version id (release_date); default: the checkout's version.txt")
    parser.add_argument("--manifest-id", help="Steam depot manifest GID for this build (optional).")
    parser.add_argument("--patch-utc", help="build publish time, e.g. 2026-09-15T07:16:00Z (optional).")
    parser.add_argument("--no-write", action="store_true", help="detect only; do not modify the curated file.")
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        version = args.version or read_version(args.data_dir)
        result = record_releases(
            args.data_dir, version, args.manifest_id, args.patch_utc, write=not args.no_write,
        )
    except ReleasesError as exc:
        logger.error("recording failed, nothing changed: {}", exc)
        emit({"event": "releases-error", "error": str(exc)})
        return EXIT_ERROR

    if result.orphaned_refs:
        logger.warning("recorded refs no longer in roster: {} (WRF does not retire robots — "
                       "likely a slug rename; verify)", result.orphaned_refs)
    for b in result.backfilled:
        logger.info("backfilled {} ({}): {}", b["name"], b["id"], b["filled"])

    if not result.new_bots:
        logger.info("no new robots (curated file already lists the roster; version {})", result.version)
        emit({"event": "no-change", "version": result.version,
              "backfilled": result.backfilled, "orphaned_refs": result.orphaned_refs})
        return EXIT_NO_CHANGE

    names = ", ".join(f"{b['name']} ({b['id']})" for b in result.new_bots)
    logger.warning("ROBOT RELEASED (version {}): {}{}", result.version, names,
                   " [SUSPECTED RENAME — verify]" if result.suspected_rename else "")
    emit({"event": "robots-released", "version": result.version,
          "suspected_rename": result.suspected_rename,
          "new_bots": result.new_bots, "backfilled": result.backfilled,
          "orphaned_refs": result.orphaned_refs})
    return EXIT_NEW_RELEASE


if __name__ == "__main__":
    raise SystemExit(main())
