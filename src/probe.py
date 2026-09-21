#!/usr/bin/env python3
"""Anonymous Steam PICS probe: has a new War Robots: Frontiers patch shipped?

A "new patch" is defined as the public depot's manifest GID changing. We read it
straight from Steam's PICS product-info over an ANONYMOUS Steam Connection Manager
login — no account, no ownership, no download (~2s):

    anonymous_login() -> get_product_info(apps=[APP_ID])
    gid = apps[APP_ID]['depots'][DEPOT_ID]['manifests']['public']['gid']

The GID is the right comparison key: it is byte-identical to what the Exporter
caches in its manifest.txt, whereas buildid is a looser signal. We record buildid
and timeupdated too, but only for the log — the decision is on the GID.

This probe keeps its OWN state file (default: <repo>/data/update_probe_state.json)
and does NOT read the Exporter's workstation manifest.txt (which doesn't exist on
this box). It carries no secrets — the PICS query is anonymous. (The eventual real
download needs owned Steam creds; that's out of scope for the probe.)

On a detected patch the probe derives the version id (see src/versioning.py) and,
if --on-patch-cmd is given, hands off to it with {version} substituted (the home
box wires this to `systemctl start wrf-orchestrator@{version}`). The state advance
is GATED on that hand-off: state is only marked seen once the command exits 0, so a
failed hand-off is retried on the next poll rather than silently swallowed (mirrors
the News-Scraper's "only mark the week parsed once it's handed off"). With no
--on-patch-cmd the probe just logs + advances (the old detector-only behaviour).

Exit codes (the machine-readable signal alongside the log):
    0   clean check, no new patch. Also the first-run case: the current GID is
        recorded as the baseline and reported, so we never fire on first run.
    10  new patch detected (public manifest GID changed) and, if an --on-patch-cmd
        was given, handed off successfully. State is advanced.
    1   probe error (transient CM/login failure, unexpected PICS shape, ...) OR a
        failed hand-off. State is left UNTOUCHED and no update is committed, so the
        next run re-checks. "Probe failed" is deliberately never "patch detected".

The systemd unit sets SuccessExitStatus=10 so a detected patch is not a "failed"
unit; only exit 1 (a real probe error) marks the unit failed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

from versioning import derive_version

# --- War Robots: Frontiers on Steam -----------------------------------------
APP_ID = 1491000
DEPOT_ID = "1491005"  # PICS keys depots by string id

# --- Exit codes --------------------------------------------------------------
EXIT_NO_CHANGE = 0
EXIT_UPDATE = 10
EXIT_ERROR = 1

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STATE = REPO / "data" / "update_probe_state.json"


class ProbeError(RuntimeError):
    """A probe attempt failed in a way that must be treated as 'no change'."""


def _configure_logging() -> None:
    """Single stderr sink, INFO by default. loguru auto-disables color off a TTY,
    so this stays clean in the systemd journal. Override with WRF_PROBE_LOG_LEVEL."""
    level = os.environ.get("WRF_PROBE_LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | probe | {message}",
    )


def query_pics(retries: int, delay: float) -> dict:
    """Return {'gid', 'buildid', 'timeupdated'} from an anonymous PICS query.

    Retries transient CM/login failures with a fixed backoff. Raises ProbeError
    if every attempt fails or the product-info shape is not what we expect.
    """
    # Imported lazily so `import probe` (e.g. from a future trigger stage or a
    # test) doesn't pull in the whole gevent/steam client stack unless we query.
    from steam.client import SteamClient
    from steam.enums import EResult

    last_err: str | None = None
    for attempt in range(1, retries + 1):
        client = SteamClient()
        try:
            result = client.anonymous_login()
            if result != EResult.OK:
                last_err = f"anonymous_login returned {result!r}"
                raise ProbeError(last_err)

            info = client.get_product_info(apps=[APP_ID])
            if not info:
                last_err = "get_product_info returned no data"
                raise ProbeError(last_err)

            try:
                app = info["apps"][APP_ID]
                depot = app["depots"][DEPOT_ID]
                gid = depot["manifests"]["public"]["gid"]
                branch = app["depots"]["branches"]["public"]
            except (KeyError, TypeError) as exc:
                # A shape change is not transient — surface it, but still as an
                # error (never as a patch), so we don't fire on garbage.
                raise ProbeError(f"unexpected PICS product-info shape: {exc!r}") from exc

            return {
                "gid": str(gid),
                "buildid": branch.get("buildid"),
                "timeupdated": branch.get("timeupdated"),
            }
        except ProbeError as exc:
            last_err = str(exc)
            logger.warning("PICS query attempt {}/{} failed: {}", attempt, retries, last_err)
        except Exception as exc:  # noqa: BLE001 - any client/network error is transient-ish
            last_err = repr(exc)
            logger.warning("PICS query attempt {}/{} errored: {}", attempt, retries, last_err)
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - logout best-effort; never mask the real error
                pass

        if attempt < retries:
            time.sleep(delay)

    raise ProbeError(last_err or "PICS query failed")


def load_state(path: Path) -> dict:
    """Read the probe's state file; return {} if it's absent or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt state file is treated as "no baseline" rather than an error:
        # the next run re-establishes it. Log loudly so it's visible.
        logger.warning("state file {} unreadable ({}); treating as no baseline", path, exc)
        return {}


def save_state(path: Path, gid: str, buildid, timeupdated) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_gid": gid,
        "buildid": buildid,
        "timeupdated": timeupdated,
        "checked_at": int(time.time()),
    }
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def emit(event: dict) -> None:
    """One machine-readable JSON line to stdout (loguru logs go to stderr)."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anonymous Steam PICS patch probe for WRF.")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("WRF_PROBE_STATE", DEFAULT_STATE)),
        help="path to the probe state file (default: <repo>/data/update_probe_state.json, "
        "or $WRF_PROBE_STATE)",
    )
    parser.add_argument("--retries", type=int, default=int(os.environ.get("WRF_PROBE_RETRIES", 3)),
                        help="PICS query attempts before giving up (default 3)")
    parser.add_argument("--retry-delay", type=float,
                        default=float(os.environ.get("WRF_PROBE_RETRY_DELAY", 5)),
                        help="seconds between retries (default 5)")
    parser.add_argument("--on-patch-cmd", default=os.environ.get("WRF_PROBE_ON_PATCH_CMD"),
                        help="command run on patch detection, with {version} "
                        "substituted (split shell-style, run WITHOUT a shell). The "
                        "state advance is gated on it: exit 0 -> state advances and "
                        "the probe exits 10; non-zero -> state untouched and the "
                        "probe exits 1 so the next poll retries. Omit to just detect "
                        "+ advance (no hand-off).")
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        current = query_pics(args.retries, args.retry_delay)
    except ProbeError as exc:
        logger.error("probe failed, treating as no change (state untouched): {}", exc)
        emit({"event": "probe-error", "error": str(exc)})
        return EXIT_ERROR

    gid = current["gid"]
    logger.info("PICS ok: gid={} buildid={} timeupdated={}",
                gid, current["buildid"], current["timeupdated"])

    state = load_state(args.state)
    prev_gid = state.get("last_gid")

    if not prev_gid:
        save_state(args.state, gid, current["buildid"], current["timeupdated"])
        logger.info("baseline recorded (gid={}); no update signalled on first run", gid)
        emit({"event": "probe-baseline", "gid": gid, "buildid": current["buildid"]})
        return EXIT_NO_CHANGE

    if gid == prev_gid:
        # Refresh checked_at so the state file doubles as a "last seen" heartbeat.
        save_state(args.state, gid, current["buildid"], current["timeupdated"])
        logger.info("no change (gid unchanged: {})", gid)
        emit({"event": "no-change", "gid": gid})
        return EXIT_NO_CHANGE

    # New public manifest GID -> a patch shipped. Derive the version id future
    # steps use from the manifest's unix timestamp (UTC calendar day), keyed on the
    # GID so a second patch the same day gets a -N suffix. Fall back to now if Steam
    # didn't report timeupdated, so we always produce a version.
    ts = current["timeupdated"]
    if ts is None:
        ts = int(time.time())
        logger.warning("manifest timeupdated missing; using current time for the version date")
    registry_path = args.state.parent / "version_registry.json"
    version = derive_version(ts, gid, registry_path)
    logger.warning("PATCH DETECTED: gid {} -> {} (buildid {}) -> version {}",
                   prev_gid, gid, current["buildid"], version)

    # Hand off to the pipeline, if wired. The state advance is GATED on this: we only
    # mark the GID seen once the hand-off succeeds, so a failed trigger is retried on
    # the next poll instead of being lost. The version assignment (registry, above) is
    # idempotent, so a retry reuses the same version.
    if args.on_patch_cmd:
        trig = [tok.replace("{version}", version) for tok in shlex.split(args.on_patch_cmd)]
        logger.info("handing off: {}", " ".join(trig))
        try:
            rc = subprocess.run(trig, check=False).returncode
        except OSError as exc:
            logger.error("hand-off failed to launch ({}); state untouched, will retry", exc)
            emit({"event": "trigger-error", "new_gid": gid, "version": version, "error": str(exc)})
            return EXIT_ERROR
        if rc != 0:
            logger.error("hand-off exited {}; state untouched, will retry", rc)
            emit({"event": "trigger-error", "new_gid": gid, "version": version, "returncode": rc})
            return EXIT_ERROR
        logger.info("hand-off launched ok")

    save_state(args.state, gid, current["buildid"], current["timeupdated"])
    emit({"event": "patch-detected", "old_gid": prev_gid, "new_gid": gid,
          "buildid": current["buildid"], "timeupdated": current["timeupdated"],
          "version": version})
    return EXIT_UPDATE


if __name__ == "__main__":
    raise SystemExit(main())
