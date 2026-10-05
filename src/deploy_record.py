"""Deploy records: which WRFrontiersDB-Data commit each frontend is serving.

Both frontends (the Site and the Discount Visualizer) write a deploy record when
they build (Data's `record-deploy` action): the Data commit, date and version they
were built from, plus their own commit and CI run. It is served live as
`<url>/deploy.json` and uploaded as the run's `deploy-record` artifact. Fields are
documented in WRFrontiersDB-Data's `tools/wrfdb_data/deploy_record.py`.

After SITE-DEPLOY follows the Site's run to success, :func:`record` downloads that
run's artifact (exact, no CDN lag) and writes it, plus `deployed_at_utc`, to
`data/site_deploy_state.json`. The Discord bot watches that file: it re-fetches the
Site's `/meta_descriptions.json` when `run_id` changes, and reads `data_commit` from
it. Only the Site has a state file, because the bot is its only consumer; anything
else asking what's deployed reads the live `/deploy.json`, which every deploy
updates, pipeline or not. `bin/wrf-deployed` (src/deployed.py) compares the live
records with Data `main` and the Site's state file.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

import gh_runs

DATA_REPO = "Surxe/WRFrontiersDB-Data"
ARTIFACT = "deploy-record"
STATE_DIR = Path(__file__).resolve().parents[1] / "data"
FETCH_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Frontend:
    name: str
    repo: str
    url: str
    """The live site's root, with a trailing slash."""
    state_file: Path | None = None
    """Where the pipeline records its deploys of this frontend, if it does."""

    @property
    def record_url(self) -> str:
        return self.url + "deploy.json"


SITE = Frontend("Site", "Surxe/WRFrontiersDB-Site", "https://wrf-db.info/",
                STATE_DIR / "site_deploy_state.json")
VISUALIZER = Frontend("Visualizer", "Surxe/WRFrontiers-Discount-Visualizer",
                      "https://surxe.github.io/WRFrontiers-Discount-Visualizer/")
FRONTENDS = (SITE, VISUALIZER)


class DeployRecordError(RuntimeError):
    pass


def download(repo: str, run_id: str) -> dict:
    """The deploy record a run uploaded as its `deploy-record` artifact."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(["gh", "run", "download", run_id, "-R", repo, "-n", ARTIFACT, "-D", tmp],
                           check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or exc
            raise DeployRecordError(f"could not download the {ARTIFACT} artifact of run {run_id} "
                                    f"on {repo}: {str(detail).strip()[:300]}") from exc
        return _parse(Path(tmp) / "deploy.json", f"run {run_id}'s {ARTIFACT} artifact")


def fetch_live(frontend: Frontend) -> dict:
    """The record the live site serves (cache-busted, so a fresh CDN copy)."""
    url = f"{frontend.record_url}?t={int(time.time())}"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8")
    except OSError as exc:
        raise DeployRecordError(f"could not fetch {frontend.record_url}: {exc}") from exc
    return _loads(text, frontend.record_url)


def read_state(frontend: Frontend) -> dict | None:
    """The last deploy the pipeline recorded, or None if it never recorded one."""
    if frontend.state_file is None or not frontend.state_file.exists():
        return None
    return _parse(frontend.state_file, f"{frontend.name} deploy state")


def record(frontend: Frontend, run: gh_runs.WorkflowRun) -> int:
    """Record a successful deploy run in the frontend's state file; 1 if that fails."""
    try:
        state = download(frontend.repo, run.run_id)
        if state.get("run_id") != run.run_id:
            raise DeployRecordError(f"the artifact is from run {state.get('run_id')}, "
                                    f"not run {run.run_id}")
        state["deployed_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_atomic(frontend.state_file, state)
    except (DeployRecordError, OSError) as exc:
        logger.error(f"{frontend.name} deployed, but its deploy was not recorded in "
                     f"{frontend.state_file}: {exc}; consumers will not pick it up")
        return 1
    logger.info(f"Recorded {frontend.name} run {run.run_id} (data {state['data_commit'][:7]}, "
                f"version {state['data_version']}) in {frontend.state_file}")
    return 0


def _write_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # consumers never see a half-written file


def _parse(path: Path, what: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeployRecordError(f"{what} unreadable: {exc}") from exc
    return _loads(text, what)


def _loads(text: str, what: str) -> dict:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise DeployRecordError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not doc.get("data_commit"):
        raise DeployRecordError(f"{what} has no data_commit")
    return doc
