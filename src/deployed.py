#!/usr/bin/env python3
"""What data is live? Each frontend's deployed WRFrontiersDB-Data commit vs Data main.

For the Site and the Discount Visualizer: the deploy record the live site serves
(`/deploy.json`), how far its Data commit is behind Data `main`, and whether the
pipeline's state file (what the Discord bot reads) matches it. Deploys made
outside the pipeline update only the live record, so a mismatch there is normal
until the next pipeline deploy; it matters only for the Site, since the bot reads
the Site's state.

Exit code: 0 when both frontends serve Data `main`, 1 otherwise (behind, or a
record could not be read).

Examples:
    bin/wrf-deployed
    bin/wrf-deployed --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

import deploy_record  # noqa: E402
import gh_runs  # noqa: E402
from deploy_record import DATA_REPO, DeployRecordError, Frontend  # noqa: E402


def data_main(gh=gh_runs.gh_json) -> dict:
    commit = gh(["api", f"repos/{DATA_REPO}/commits/main"])
    return {"sha": commit["sha"], "date_utc": commit["commit"]["committer"]["date"]}


def commits_behind(data_commit: str, main_sha: str, gh=gh_runs.gh_json) -> int:
    """How many commits Data main has that `data_commit` doesn't."""
    if data_commit == main_sha:
        return 0
    return int(gh(["api", f"repos/{DATA_REPO}/compare/{data_commit}...{main_sha}"])["ahead_by"])


def check(frontend: Frontend, main_sha: str, *, gh=gh_runs.gh_json,
          fetch_live=deploy_record.fetch_live, read_state=deploy_record.read_state) -> dict:
    """One frontend's live record, pipeline state and distance from Data main."""
    out: dict = {"url": frontend.record_url, "live": None, "behind_main": None,
                 "state": None, "state_matches_live": None, "error": None}
    try:
        out["state"] = read_state(frontend)
    except DeployRecordError as exc:
        out["error"] = str(exc)
    try:
        live = fetch_live(frontend)
    except DeployRecordError as exc:
        out["error"] = str(exc)
        return out
    out["live"] = live
    if out["state"] is not None:
        out["state_matches_live"] = out["state"].get("run_id") == live.get("run_id")
    try:
        out["behind_main"] = commits_behind(live["data_commit"], main_sha, gh=gh)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        out["error"] = f"could not compare {live['data_commit'][:7]} with Data main: {exc}"
    return out


def report(main: dict, results: dict[str, dict], now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    lines = [f"Data main   {main['sha'][:7]}  committed {main['date_utc']}"]
    for name, r in results.items():
        live = r["live"]
        if live is None:
            lines.append(f"{name:<11} ERROR: {r['error']}")
            continue
        behind = r["behind_main"]
        where = ("on main" if behind == 0 else
                 f"{behind} commit(s) behind main" if behind is not None else "vs main unknown")
        lines.append(f"{name:<11} data {live['data_commit'][:7]}  version {live['data_version']}  "
                     f"{where}")
        lines.append(f"{'':<11} built {live['built_at_utc']} ({_ago(live['built_at_utc'], now)}) "
                     f"by {live.get('trigger') or 'hand'}: {live.get('run_url') or '-'}")
        if r["state"] is None:
            lines.append(f"{'':<11} pipeline state: none recorded")
        elif not r["state_matches_live"]:
            st = r["state"]
            lines.append(f"{'':<11} pipeline state: run {st.get('run_id')} (data "
                         f"{st.get('data_commit', '?')[:7]}), not the live run; deployed outside "
                         "the pipeline since")
        if r["error"]:
            lines.append(f"{'':<11} ERROR: {r['error']}")
    commits = {r["live"]["data_commit"] for r in results.values() if r["live"]}
    if len(commits) > 1:
        lines.append("Note: the frontends serve different Data commits.")
    return lines


def all_current(results: dict[str, dict]) -> bool:
    return all(r["live"] is not None and r["behind_main"] == 0 for r in results.values())


def _ago(iso: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    hours = (now - then).total_seconds() / 3600
    return f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="Print one JSON object instead.")
    args = parser.parse_args(argv)
    try:
        main_commit = data_main()
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        print(f"error: could not read Data main: {exc}", file=sys.stderr)
        return 1
    results = {f.name: check(f, main_commit["sha"]) for f in deploy_record.FRONTENDS}
    if args.json:
        print(json.dumps({"data_main": main_commit, "frontends": results}, indent=2))
    else:
        print("\n".join(report(main_commit, results)))
    return 0 if all_current(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
