"""Run report — the per-run summary the orchestrator emails.

The report is pure reporting layered on top of the run: it never changes what the
pipeline does. It answers two things for a finished (or aborted) run:

* how many warnings/errors each step that was reached produced, and
* where every log file is (as a clickable ``file://`` URI).

Counts come from the per-step log files :class:`~logging_stream.RunLogger` handed
out (``runlog.stage_logs``), so only steps that actually ran appear — exactly
"the counts at each step, if it got to that step".

Two count strategies, because not every step logs the same way:

* the orchestrator's own steps and the Python sub-repos (preflight, export,
  parse, releases) log through loguru, whose lines start with the level token
  (``WARNING | ...``) — counted exactly;
* the SITE / SITE-DEPLOY steps shell out to npm/astro/gh, which have no loguru
  levels, so their counts are a best-effort text scan, flagged ``~`` (approx).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Steps whose logs are loguru-formatted (level token leads the line).
_LOGURU_STAGES = {"preflight", "export", "parse", "releases"}

# Loguru file lines look like: "WARNING | module:function:line - message".
_LOGURU_WARN = re.compile(r"^WARNING\b")
_LOGURU_ERROR = re.compile(r"^(ERROR|CRITICAL)\b")

# Heuristic scan for the npm/astro/gh stages (no structured levels).
_TEXT_WARN = re.compile(r"\bwarn(?:ing)?\b", re.IGNORECASE)
_TEXT_ERROR = re.compile(r"\b(error|failed)\b|npm ERR!", re.IGNORECASE)


@dataclass(frozen=True)
class StepCount:
    stage: str
    warnings: int
    errors: int
    approx: bool  # counts are a text heuristic (npm/gh), not loguru levels


def _count_file(stage: str, path: Path) -> StepCount:
    """Count warnings/errors in one step's log (empty if unreadable)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return StepCount(stage, 0, 0, approx=False)

    if stage in _LOGURU_STAGES:
        warns = sum(1 for ln in text.splitlines() if _LOGURU_WARN.match(ln))
        errs = sum(1 for ln in text.splitlines() if _LOGURU_ERROR.match(ln))
        return StepCount(stage, warns, errs, approx=False)

    warns = sum(1 for ln in text.splitlines() if _TEXT_WARN.search(ln))
    errs = sum(1 for ln in text.splitlines() if _TEXT_ERROR.search(ln))
    return StepCount(stage, warns, errs, approx=True)


class RunReport:
    """Collects a run's outcome and renders the report body + subject."""

    def __init__(self, runlog, *, game_version: str | None = None) -> None:
        self._runlog = runlog
        self.game_version = game_version
        self.result = "INCOMPLETE"  # set via finalize()

    def finalize(self, result: str, *, game_version: str | None = None) -> None:
        """Record the run's outcome (e.g. 'COMPLETE', 'FAILED at PARSE')."""
        self.result = result
        if game_version is not None:
            self.game_version = game_version

    def counts(self) -> list[StepCount]:
        return [_count_file(stage, path) for stage, path in self._runlog.stage_logs]

    def totals(self) -> tuple[int, int]:
        counts = self.counts()
        return sum(c.warnings for c in counts), sum(c.errors for c in counts)

    def subject(self) -> str:
        version = self.game_version or "unknown"
        warns, errs = self.totals()
        return (f"WRFrontiersDB {version} - {self.result}: "
                f"{warns} warnings, {errs} errors")

    def body(self) -> str:
        version = self.game_version or "unknown"
        counts = self.counts()
        warns, errs = self.totals()

        lines = [
            "WRFrontiersDB-Orchestrator run report",
            f"Patch:  {version}",
            f"Result: {self.result}",
            f"Totals: {warns} warnings, {errs} errors",
            "",
            "Steps reached (warnings / errors):",
        ]
        if counts:
            width = max(len(c.stage) for c in counts)
            for c in counts:
                flag = " ~approx" if c.approx else ""
                lines.append(f"  {c.stage:<{width}}  {c.warnings}W / {c.errors}E{flag}")
        else:
            lines.append("  (no steps ran)")

        lines += ["", "Logs:", f"  {_uri(self._runlog.run_dir)}"]
        for _stage, path in self._runlog.stage_logs:
            lines.append(f"  {_uri(path)}")
        lines.append(f"  {_uri(self._runlog.run_log)}")

        if any(c.approx for c in counts):
            lines += ["",
                      "(~approx: SITE/SITE-DEPLOY counts are a text scan of "
                      "npm/astro/gh output, not loguru levels.)"]
        return "\n".join(lines) + "\n"


def _uri(path: Path) -> str:
    """Absolute file:// URI for a log path (resolve first — as_uri needs abs)."""
    return Path(path).resolve().as_uri()
