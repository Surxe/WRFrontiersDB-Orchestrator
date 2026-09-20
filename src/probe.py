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

SCOPE: probe only. On a detected patch it logs and advances its state — it does
NOT trigger the pipeline. Wiring the orchestrator trigger is a separate later PR.
When that lands, advancing the state should be gated on a successful trigger
(mirroring the News-Scraper's "only mark the week parsed once it's handed off"),
so a failed trigger is retried rather than silently swallowed.

Exit codes (the machine-readable signal alongside the log):
    0   clean check, no new patch. Also the first-run case: the current GID is
        recorded as the baseline and reported, so we never fire on first run.
    10  new patch detected (public manifest GID changed). State is advanced.
    1   probe error (transient CM/login failure, unexpected PICS shape, ...).
        State is left UNTOUCHED and no update is signalled, so the next run
        re-checks. "Probe failed" is deliberately never "patch detected".

The systemd unit sets SuccessExitStatus=10 so a detected patch is not a "failed"
unit; only exit 1 (a real probe error) marks the unit failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from loguru import logger

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

    # New public manifest GID -> a patch shipped.
    save_state(args.state, gid, current["buildid"], current["timeupdated"])
    logger.warning("PATCH DETECTED: gid {} -> {} (buildid {})",
                   prev_gid, gid, current["buildid"])
    emit({"event": "patch-detected", "old_gid": prev_gid, "new_gid": gid,
          "buildid": current["buildid"], "timeupdated": current["timeupdated"]})
    return EXIT_UPDATE


if __name__ == "__main__":
    raise SystemExit(main())
