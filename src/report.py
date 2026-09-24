"""Run report — the per-run summary the orchestrator emails.

The report is pure reporting layered on top of the run: it never changes what the
pipeline does. For a finished (or aborted) run it answers:

* how many warnings/errors each step that was reached produced,
* what those warning/error lines actually said (inline, capped), and
* where every log file is (as a clickable ``file://`` link).

Counts come from the per-step log files :class:`~logging_stream.RunLogger` handed
out (``runlog.stage_logs``), so only steps that actually ran appear — exactly
"the counts at each step, if it got to that step".

The report carries the warning/error lines *inline* so the email is
self-contained: it stays useful even if the log files are later moved or cleaned
(the ``file://`` links are a convenience, not the only copy).

Two count strategies, because not every step logs the same way:

* the orchestrator's own steps and the Python sub-repos (preflight, export,
  parse, releases) log through loguru, whose lines start with the level token
  (``WARNING | ...``) — counted exactly;
* the SITE / SITE-DEPLOY steps shell out to npm/astro/gh, which have no loguru
  levels, so their counts are a best-effort text scan, flagged as approximate.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

# Steps whose logs are loguru-formatted (level token leads the line).
_LOGURU_STAGES = {"preflight", "export", "parse", "releases"}

# Loguru file lines look like: "WARNING | module:function:line - message".
_LOGURU_WARN = re.compile(r"^WARNING\b")
_LOGURU_ERROR = re.compile(r"^(ERROR|CRITICAL)\b")

# Heuristic scan for the npm/astro/gh stages (no structured levels).
_TEXT_WARN = re.compile(r"\bwarn(?:ing)?\b", re.IGNORECASE)
_TEXT_ERROR = re.compile(r"\b(error|failed)\b|npm ERR!", re.IGNORECASE)

# Cap the inline lines per step so a noisy run can't produce a giant email.
_MAX_LINES = 25


@dataclass(frozen=True)
class StepCount:
    stage: str
    warnings: int
    errors: int
    approx: bool          # counts are a text heuristic (npm/gh), not loguru levels
    lines: tuple[str, ...]  # the actual warning/error lines (capped to _MAX_LINES)
    truncated: bool       # there were more matching lines than are shown


def _count_file(stage: str, path: Path) -> StepCount:
    """Count and collect warnings/errors in one step's log (empty if unreadable)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return StepCount(stage, 0, 0, False, (), False)

    loguru = stage in _LOGURU_STAGES
    warn_re, err_re = (
        (_LOGURU_WARN, _LOGURU_ERROR) if loguru else (_TEXT_WARN, _TEXT_ERROR)
    )
    warnings = errors = 0
    collected: list[str] = []
    for ln in text.splitlines():
        is_err = bool(err_re.search(ln))
        # For loguru a line has exactly one level; the heuristic may match both,
        # so count it once (error wins) to avoid double counting.
        is_warn = bool(warn_re.search(ln)) and not (loguru and is_err)
        if is_err:
            errors += 1
        elif is_warn:
            warnings += 1
        if is_err or is_warn:
            if len(collected) < _MAX_LINES:
                collected.append(ln.rstrip())
    truncated = (warnings + errors) > len(collected)
    return StepCount(stage, warnings, errors, not loguru, tuple(collected), truncated)


class RunReport:
    """Collects a run's outcome and renders the report (plain text + HTML)."""

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

    # -- plain text ------------------------------------------------------------
    def body(self) -> str:
        version = self.game_version or "unknown"
        counts = self.counts()
        warns, errs = self.totals()

        out = [
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
                out.append(f"  {c.stage:<{width}}  {c.warnings}W / {c.errors}E{flag}")
        else:
            out.append("  (no steps ran)")

        detailed = [c for c in counts if c.lines]
        if detailed:
            out += ["", "Warnings / errors:"]
            for c in detailed:
                out.append(f"  [{c.stage}]")
                out += [f"    {ln}" for ln in c.lines]
                if c.truncated:
                    out.append(f"    ... (more in the log; showing first {_MAX_LINES})")

        out += ["", "Logs:", f"  {_uri(self._runlog.run_dir)}"]
        for _stage, path in self._runlog.stage_logs:
            out.append(f"  {_uri(path)}")
        out.append(f"  {_uri(self._runlog.run_log)}")

        if any(c.approx for c in counts):
            out += ["",
                    "(~approx: SITE/SITE-DEPLOY counts are a text scan of "
                    "npm/astro/gh output, not loguru levels.)"]
        return "\n".join(out) + "\n"

    # -- HTML (hyperlinked) ----------------------------------------------------
    def body_html(self) -> str:
        version = html.escape(self.game_version or "unknown")
        counts = self.counts()
        warns, errs = self.totals()

        p = ['<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
             'font-size:14px;line-height:1.5">']
        p.append("<h2 style='margin:0 0 8px'>WRFrontiersDB-Orchestrator run report</h2>")
        p.append(f"<p style='margin:0 0 12px'>Patch: <b>{version}</b><br>"
                 f"Result: <b>{html.escape(self.result)}</b><br>"
                 f"Totals: <b>{warns}</b> warnings, <b>{errs}</b> errors</p>")

        p.append("<h3 style='margin:12px 0 4px'>Steps reached</h3>")
        if counts:
            p.append("<table cellpadding='4' style='border-collapse:collapse'>")
            p.append("<tr><th align='left'>step</th><th align='right'>warnings</th>"
                     "<th align='right'>errors</th></tr>")
            for c in counts:
                stage = html.escape(c.stage) + (" <i>~approx</i>" if c.approx else "")
                p.append(f"<tr><td>{stage}</td><td align='right'>{c.warnings}</td>"
                         f"<td align='right'>{c.errors}</td></tr>")
            p.append("</table>")
        else:
            p.append("<p>(no steps ran)</p>")

        detailed = [c for c in counts if c.lines]
        if detailed:
            p.append("<h3 style='margin:12px 0 4px'>Warnings / errors</h3>")
            for c in detailed:
                p.append(f"<p style='margin:8px 0 2px'><b>{html.escape(c.stage)}</b></p>")
                body = "\n".join(html.escape(ln) for ln in c.lines)
                if c.truncated:
                    body += f"\n... (more in the log; showing first {_MAX_LINES})"
                p.append("<pre style='margin:0;padding:8px;background:#f4f4f4;"
                         "border-radius:4px;overflow-x:auto;white-space:pre-wrap'>"
                         f"{body}</pre>")

        p.append("<h3 style='margin:12px 0 4px'>Logs</h3>")
        p.append("<p style='color:#666;font-size:12px;margin:0 0 4px'>Full paths on "
                 "the pipeline host; webmail may show them as plain text — copy the "
                 "path. Links open only on that host.</p>")
        p.append("<ul style='margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;"
                 "font-size:13px'>")
        p.append(f"<li>{_link(self._runlog.run_dir)}</li>")
        for _stage, path in self._runlog.stage_logs:
            p.append(f"<li>{_link(path)}</li>")
        p.append(f"<li>{_link(self._runlog.run_log)}</li>")
        p.append("</ul>")

        if any(c.approx for c in counts):
            p.append("<p style='color:#666;font-size:12px'>~approx: SITE/SITE-DEPLOY "
                     "counts are a text scan of npm/astro/gh output, not loguru levels.</p>")
        p.append("</div>")
        return "\n".join(p)


def _uri(path: Path) -> str:
    """Absolute file:// URI for a log path (resolve first — as_uri needs abs)."""
    return Path(path).resolve().as_uri()


def _link(path: Path) -> str:
    # Show the FULL file:// path as the visible text (copy-pasteable everywhere);
    # keep the href for desktop clients that honour file:// (webmail strips it).
    uri = _uri(path)
    return f'<a href="{html.escape(uri, quote=True)}">{html.escape(uri)}</a>'
