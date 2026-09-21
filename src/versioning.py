"""Derive the pipeline version id (yyyy-mm-dd[-N]) from a manifest timestamp + GID.

The version id names the .usmap, the exports/parsed/textures dirs, and the Parser
push, so it must be stable per patch. It is the UTC calendar day of the manifest's
unix timestamp (the probe's `timeupdated`). A given manifest GID always maps to the
same version, so re-runs are idempotent; a *different* GID on the same UTC day gets
the next free `-N` suffix (`-1`, `-2`, ...), because two patches can ship on one day.

The GID->version assignments are kept in a small registry file (JSON) so the mapping
survives across runs and the suffix logic can see what days are already taken.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_DATE_FMT = "%Y-%m-%d"


def utc_date(timestamp: int | str) -> str:
    """The UTC calendar day (yyyy-mm-dd) of a unix timestamp."""
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime(_DATE_FMT)


def _load_registry(path: Path) -> dict[str, str]:
    """Read the GID->version registry; {} if absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_registry(path: Path, registry: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic swap, so a crash never leaves a half-written registry


def derive_version(timestamp: int | str, gid: str, registry_path: Path) -> str:
    """Map (manifest unix timestamp, GID) -> a stable yyyy-mm-dd[-N] version id.

    Idempotent per GID: an already-seen GID returns its recorded version. A new GID
    lands on the base UTC date, or the next free `-N` suffix if that date (or -1,
    -2, ...) is already taken by another GID on the same day.
    """
    gid = str(gid)
    registry = _load_registry(registry_path)
    if gid in registry:
        return registry[gid]

    base = utc_date(timestamp)
    taken = set(registry.values())
    version = base
    n = 0
    while version in taken:
        n += 1
        version = f"{base}-{n}"

    registry[gid] = version
    _save_registry(registry_path, registry)
    return version
