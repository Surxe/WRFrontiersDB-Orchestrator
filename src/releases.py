#!/usr/bin/env python3
"""Record newly-released robots into the data repo's curated release-date file.

A robot is a `VirtualBot` in the published data iff its modules are used by a
factory preset, and the studio only ships an obtainable robot with one — so a new
id appearing in `current/Objects/VirtualBot.json` **is** the release signal (no
`ProductionStatus` check needed; the Parser gates on the factory preset, which
agrees 1:1 with `Ready` core modules).

The store *is* the dedup source: `curated/robot_release_dates.json` keys every
robot by its `virtual_bot_ref` (`OBJID_VirtualBot::<slug>`), in `robots{}` and
`titans{}`. A roster id whose ref is already a key is already recorded, so
detection is a pure file comparison — **no git diff against the previous patch,
no separate state file**. For each roster id we either:

  * append a fresh entry if its ref is not a key yet (Mechs -> `robots{}`,
    Titans -> `titans{}`); or
  * settle a `pending_roster` entry — one hand-recorded from the news before the
    robot reached the datamined roster (e.g. Angler) — by filling only its
    still-null sourced fields and clearing the flag, so hand-curated data is
    never overwritten.

An auto-added entry can only carry what the pipeline can source: the in-house
version id (`release_date`) and the Steam depot `manifest_id`. Article-derived
fields (`release_context`, `source_article_ids`) are left null/[] for the
news-scraper or a human to fill.

Patch metadata lives once per build in `curated/patch_manifests.json`, keyed by
manifest GID (`version`, `buildid`, `patch_released_at_utc`); a robot's
`manifest_id` points into it. `record_patch_manifests` merges in every build the
pipeline knows about (the probe's GID->version registry plus the build just
parsed), fill-only, so the box-local registry is published rather than stranded.

Publishing the edits is a commit + push to the data repo (`publish_curated`),
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
import base64
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
PATCHES_REL = Path("curated") / "patch_manifests.json"
ROSTER_REL = Path("current") / "Objects" / "VirtualBot.json"
VERSION_REL = Path("current") / "version.txt"

# Per-build fields in curated/patch_manifests.json, in file order.
PATCH_FIELDS = ("version", "buildid", "patch_released_at_utc")

DATA_REPO_SLUG = "Surxe/WRFrontiersDB-Data"


class ReleasesError(RuntimeError):
    """The data repo could not be read/written; treat as no-op, nothing committed."""


@dataclass
class RecordResult:
    version: str
    new_bots: list[dict] = field(default_factory=list)      # freshly appended entries
    backfilled: list[dict] = field(default_factory=list)    # pending_roster entries settled
    orphaned_refs: list[str] = field(default_factory=list)  # recorded refs no longer in roster
    changed: bool = False

    @property
    def suspected_rename(self) -> bool:
        # A new id appearing while a recorded ref's slug left the roster is more
        # likely a slug rename than a real simultaneous retire-and-release.
        return bool(self.new_bots and self.orphaned_refs)


@dataclass
class PatchResult:
    added: list[str] = field(default_factory=list)   # GIDs newly recorded
    filled: list[str] = field(default_factory=list)  # recorded GIDs given a still-null field

    @property
    def changed(self) -> bool:
        return bool(self.added or self.filled)


# --- helpers -----------------------------------------------------------------
def virtual_bot_ref(bot_id: str) -> str:
    return f"OBJID_VirtualBot::{bot_id}"


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


def _load_json(path: Path, what: str) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleasesError(f"{what} not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ReleasesError(f"{what} unreadable ({path}): {exc}") from exc
    if not isinstance(doc, dict):
        raise ReleasesError(f"{what} is not an object: {path}")
    return doc


def _save_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_curated(data_dir: Path) -> dict:
    path = Path(data_dir) / CURATED_REL
    doc = _load_json(path, "curated file")
    if not all(isinstance(doc.get(k), dict) for k in ("robots", "titans")):
        raise ReleasesError(f"curated file missing robots/titans objects keyed by ref: {path}")
    return doc


def save_curated(data_dir: Path, doc: dict) -> None:
    _save_json(Path(data_dir) / CURATED_REL, doc)


def load_patch_manifests(data_dir: Path) -> dict:
    path = Path(data_dir) / PATCHES_REL
    doc = _load_json(path, "patch manifests file")
    if not isinstance(doc.get("patches"), dict):
        raise ReleasesError(f"patch manifests file missing patches object keyed by GID: {path}")
    return doc


def save_patch_manifests(data_dir: Path, doc: dict) -> None:
    _save_json(Path(data_dir) / PATCHES_REL, doc)


def epoch_to_utc_iso(ts) -> str | None:
    """Steam `timeupdated` (epoch seconds) -> '2026-09-15T07:16:00Z', or None."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return None


def _new_entry(name: str, release_date, manifest_id) -> dict:
    """Build a curated entry in the file's field order for an auto-detected release."""
    return {
        "name": name,
        "release_date": release_date,
        "manifest_id": manifest_id,
        "pre_launch": False,
        "release_context": None,      # article-derived; not sourceable here
        "source_article_ids": [],     # ditto
    }


# --- detection + recording ---------------------------------------------------
def record_releases(
    data_dir: Path,
    version: str,
    manifest_id: str | None,
    *,
    write: bool = True,
) -> RecordResult:
    """Diff the roster against the curated file; append/settle; optionally save.

    Console-I/O free — the caller presents the result. Raises ReleasesError if the
    data repo can't be read. Only writes the file when something actually changed.
    """
    roster = read_roster(data_dir)
    doc = load_curated(data_dir)
    robots: dict[str, dict] = doc["robots"]
    titans: dict[str, dict] = doc["titans"]
    recorded = {**robots, **titans}

    result = RecordResult(version=version)

    for bot_id, meta in roster.items():
        ref = virtual_bot_ref(bot_id)
        entry = recorded.get(ref)

        if entry is None:
            # Genuinely new robot: add it under its ref.
            new = _new_entry(meta["name"], version, manifest_id)
            (titans if meta.get("character_type") == "Titan" else robots)[ref] = new
            result.new_bots.append({
                "id": bot_id, "name": meta["name"],
                "character_type": meta.get("character_type"),
                "release_date": version, "manifest_id": manifest_id,
            })
        elif entry.get("pending_roster"):
            # Hand-recorded from the news before it hit the roster (e.g. Angler): it
            # has now shipped, so fill only still-null sourced fields (never clobber).
            filled = {}
            for key, value in (("release_date", version), ("manifest_id", manifest_id)):
                if entry.get(key) is None and value:
                    entry[key] = value
                    filled[key] = value
            del entry["pending_roster"]
            result.backfilled.append({"id": bot_id, "name": meta["name"], "filled": filled})
        # else: already recorded — the dedup that makes this idempotent

    roster_refs = {virtual_bot_ref(b) for b in roster}
    result.orphaned_refs = sorted(
        ref for ref, e in recorded.items()
        if ref not in roster_refs and not e.get("pending_roster")
    )

    result.changed = bool(result.new_bots or result.backfilled)

    if result.changed:
        meta = doc.get("_meta")
        if isinstance(meta, dict):
            meta["robot_count"] = len(robots)
            meta["titan_count"] = len(titans)
            meta["robots_with_known_date"] = sum(1 for r in robots.values() if r.get("release_date"))
            meta["generated"] = date.today().isoformat()
        if write:
            save_curated(data_dir, doc)

    return result


def record_patch_manifests(
    data_dir: Path,
    builds: dict[str, dict],
    *,
    write: bool = True,
) -> PatchResult:
    """Merge `builds` ({gid: {version, buildid, patch_released_at_utc}}) into the file.

    Fill-only: a new GID is added; a recorded GID only gains fields still null there.
    A conflicting non-null value keeps the recorded one and is logged. Only writes
    the file when something actually changed.
    """
    doc = load_patch_manifests(data_dir)
    patches: dict[str, dict] = doc["patches"]
    result = PatchResult()

    for gid, fields in builds.items():
        gid = str(gid)
        entry = patches.get(gid)
        if entry is None:
            patches[gid] = {k: fields.get(k) for k in PATCH_FIELDS}
            result.added.append(gid)
            continue
        for key in PATCH_FIELDS:
            value = fields.get(key)
            if value is None or entry.get(key) == value:
                continue
            if entry.get(key) is None:
                entry[key] = value
                if gid not in result.filled:
                    result.filled.append(gid)
            else:
                logger.warning("patch {} {}: recorded {!r}, build says {!r}; keeping recorded",
                               gid, key, entry[key], value)

    if result.changed:
        # Chronological by version id (yyyy-mm-dd[-N] sorts lexically).
        doc["patches"] = dict(sorted(patches.items(), key=lambda kv: kv[1].get("version") or ""))
        meta = doc.get("_meta")
        if isinstance(meta, dict):
            meta["patch_count"] = len(patches)
            meta["generated"] = date.today().isoformat()
        if write:
            save_patch_manifests(data_dir, doc)

    return result


# --- publishing (the one git operation, for output not comparison) -----------
def _basic_auth_value(pat: str) -> str:
    return base64.b64encode(f"x-access-token:{pat}".encode()).decode()


def _git_auth_env(pat: str) -> dict[str, str]:
    """Authenticate one git process to GitHub with `pat`, via environment config.

    GIT_CONFIG_COUNT/KEY/VALUE (git 2.31+) sets an http.extraheader for this
    process only, the way actions/checkout does. A PAT embedded in the remote URL
    would be written to the data clone's .git/config and stay on disk.
    """
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {_basic_auth_value(pat)}",
    }


def _git(args: list[str], cwd: Path, pat: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    if pat:
        env.update(_git_auth_env(pat))
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    )
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        if pat:
            out = out.replace(pat, "********").replace(_basic_auth_value(pat), "********")
        raise ReleasesError(f"git {args[0]} failed: {out.strip()}")
    return proc


def publish_curated(data_dir: Path, *, pat: str, branch: str, message: str) -> None:
    """Commit the curated files and push them to the data repo on `branch`.

    Needed because the Parser's push reclones a fresh checkout each run, so an
    uncommitted local edit would be discarded before it ever reached the remote.
    """
    data_dir = Path(data_dir)
    _git(["config", "--local", "user.email", "orchestrator@example.com"], data_dir)
    _git(["config", "--local", "user.name", "Orchestrator"], data_dir)
    # Plain URL (the PAT goes in via _git's env): also scrubs a PAT embedded by older versions.
    _git(["remote", "set-url", "origin", f"https://github.com/{DATA_REPO_SLUG}.git"], data_dir)
    paths = [str(rel) for rel in (CURATED_REL, PATCHES_REL) if (data_dir / rel).exists()]
    _git(["add", *paths], data_dir)
    status = _git(["status", "--porcelain", "--", *paths], data_dir)
    if not status.stdout.strip():
        logger.info("curated files unchanged in git; nothing to push")
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
        description="Record newly-released WRF robots (and this build's patch manifest) "
                    "into the data repo's curated files.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("WRF_DATA_DIR", "/srv/dev/repos/WRFrontiersDB-Data")),
        help="WRFrontiersDB-Data checkout (default: /srv/dev/repos/WRFrontiersDB-Data or $WRF_DATA_DIR).",
    )
    parser.add_argument("--version", help="in-house version id (release_date); default: the checkout's version.txt")
    parser.add_argument("--manifest-id", help="Steam depot manifest GID for this build (optional; "
                        "also records it in patch_manifests.json).")
    parser.add_argument("--buildid", help="Steam buildid for this build (optional).")
    parser.add_argument("--patch-utc", help="build publish time, e.g. 2026-09-15T07:16:00Z (optional).")
    parser.add_argument("--no-write", action="store_true", help="detect only; do not modify the curated files.")
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        version = args.version or read_version(args.data_dir)
        result = record_releases(args.data_dir, version, args.manifest_id, write=not args.no_write)
        patches = PatchResult()
        if args.manifest_id:
            build = {"version": version, "buildid": args.buildid,
                     "patch_released_at_utc": args.patch_utc}
            patches = record_patch_manifests(
                args.data_dir, {args.manifest_id: build}, write=not args.no_write,
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
    if patches.changed:
        logger.info("patch manifests: added {}, filled {}", patches.added, patches.filled)

    if not result.new_bots:
        logger.info("no new robots (curated file already lists the roster; version {})", result.version)
        emit({"event": "no-change", "version": result.version,
              "backfilled": result.backfilled, "orphaned_refs": result.orphaned_refs,
              "patches_added": patches.added})
        return EXIT_NO_CHANGE

    names = ", ".join(f"{b['name']} ({b['id']})" for b in result.new_bots)
    logger.warning("ROBOT RELEASED (version {}): {}{}", result.version, names,
                   " [SUSPECTED RENAME — verify]" if result.suspected_rename else "")
    emit({"event": "robots-released", "version": result.version,
          "suspected_rename": result.suspected_rename,
          "new_bots": result.new_bots, "backfilled": result.backfilled,
          "orphaned_refs": result.orphaned_refs, "patches_added": patches.added})
    return EXIT_NEW_RELEASE


if __name__ == "__main__":
    raise SystemExit(main())
