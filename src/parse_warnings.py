"""Parse warning groups — the Parser's tools/warning_report.py, run for the report.

A patch-day parse can log thousands of WARNING / UNKNOWN_PROPERTY lines that are
really a dozen distinct issues. Once the PARSE step has run (finished or not),
this runs the Parser's own grouping tool on the run's parse log and keeps:

* ``<run_dir>/parse-warnings.md`` — the full grouped report (examples, export
  links), attached to the run-report email with the logs, and
* the groups themselves (kind / title / count / NEW vs the last completed parse),
  which the email lists in place of raw warning lines.

Pure reporting, like the email: it never changes the run's outcome. It runs with
``--no-decisions`` because the decisions files are the human patch-warnings
workflow's local state (in the Parser dev worktree), not the pipeline's — and the
pipeline's Parser checkout must stay clean.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from repos import Repos

REPORT_NAME = "parse-warnings.md"
_TIMEOUT_S = 300


@dataclass(frozen=True)
class WarningGroup:
    kind: str     # error | warning | unknown-property
    title: str
    count: int
    new: bool     # absent from the baseline (last completed) parse


@dataclass(frozen=True)
class ParseWarnings:
    groups: tuple[WarningGroup, ...]
    total_lines: int
    baseline: str | None   # baseline run dir name, None when there was none
    report_path: Path | None  # the markdown report, None if it couldn't be written

    @property
    def new_count(self) -> int:
        return sum(1 for g in self.groups if g.new)


def collect(repos: Repos, run_dir: Path, wrf_root: Path) -> ParseWarnings | None:
    """Group the run's parse log; None (logged) if the tool fails or is missing."""
    tool = repos.parser_dir / "tools" / "warning_report.py"
    if not tool.is_file():
        logger.warning(f"Parse warning report: {tool} not found; skipped")
        return None
    base = [str(repos.venv_python(repos.parser_dir)), str(tool), str(run_dir), "--no-decisions"]
    # The tool finds the baseline parse and the export tree under WRF_ROOT.
    env = {**os.environ, "WRF_ROOT": str(wrf_root)}

    data = _run(base + ["--json"], repos.parser_dir, env)
    if data is None:
        return None
    try:
        parsed = json.loads(data)
        groups = tuple(
            WarningGroup(g["kind"], g["title"], int(g["count"]), bool(g.get("new")))
            for g in parsed["groups"]
        )
        baseline = parsed.get("baseline")
        total = int(parsed.get("total_lines", 0))
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(f"Parse warning report: unreadable JSON ({exc}); skipped")
        return None

    report_path: Path | None = run_dir / REPORT_NAME
    if _run(base + ["--out", str(report_path)], repos.parser_dir, env) is None:
        report_path = None
    elif not report_path.is_file():
        report_path = None

    result = ParseWarnings(groups, total,
                           Path(baseline).parent.name if baseline else None, report_path)
    logger.info(f"Parse warning report: {total} lines -> {len(groups)} groups"
                + (f", {result.new_count} NEW" if baseline else "")
                + (f" -> {report_path}" if report_path else ""))
    return result


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> str | None:
    """stdout of the tool; exit 0 (no groups) and 1 (groups) are both success."""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"Parse warning report failed: {exc}")
        return None
    if proc.returncode not in (0, 1):
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["(no output)"]
        logger.warning(f"Parse warning report failed (exit {proc.returncode}): {tail[0]}")
        return None
    return proc.stdout
