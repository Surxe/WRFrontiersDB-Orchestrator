"""Run report — the per-run summary the orchestrator emails.

The report is pure reporting layered on top of the run: it never changes what the
pipeline does. For a finished (or aborted) run it answers:

* how many warnings/errors/unknown properties each step that was reached produced,
* what those warning/error lines actually said (inline, capped) — except for the
  parse step, whose lines are summarised as groups instead (see below), and
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
  (``WARNING | ...``) — counted exactly. The parser logs every unknown property
  (new game data it neither parses nor skips) at its custom ``UNKNOWN_PROPERTY``
  level; those are counted separately and not shown inline, since one patch can
  produce thousands of them;
* the SITE / SITE-DEPLOY steps shell out to npm/astro/gh, which have no loguru
  levels, so their counts are a best-effort text scan, flagged as approximate.

The parse step's raw lines aren't shown: a patch's parse repeats one message per
module, so the first lines are usually copies of a single issue. Instead the
Parser's tools/warning_report.py groups the whole log (parse_warnings.py) and the
report lists each group once — kind, count, title, NEW vs the last completed
parse — with the full grouped report attached as parse-warnings.md.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

# Steps whose logs are loguru-formatted (level token leads the line).
_LOGURU_STAGES = {"preflight", "export", "parse", "releases", "visualizer"}

# Loguru file lines look like: "WARNING | module:function:line - message".
_LOGURU_WARN = re.compile(r"^WARNING\b")
_LOGURU_ERROR = re.compile(r"^(ERROR|CRITICAL)\b")
# Parser's custom level for unknown properties (WRFrontiersDB-Parser src/utils.py).
_LOGURU_UNKNOWN = re.compile(r"^UNKNOWN_PROPERTY\b")

# Heuristic scan for the npm/astro/gh stages (no structured levels).
_TEXT_WARN = re.compile(r"\bwarn(?:ing)?\b", re.IGNORECASE)
_TEXT_ERROR = re.compile(r"\b(error|failed)\b|npm ERR!", re.IGNORECASE)

# Cap the inline lines per step so a noisy run can't produce a giant email.
_MAX_LINES = 25
# Steps whose raw lines are not shown inline (summarised as groups instead).
_NO_INLINE_STAGES = {"parse"}
# Cap the parse warning groups listed in the email (the attachment has them all).
_MAX_GROUPS = 40
_GROUP_TITLE_WIDTH = 140


@dataclass(frozen=True)
class StepCount:
    stage: str
    warnings: int
    errors: int
    approx: bool          # counts are a text heuristic (npm/gh), not loguru levels
    lines: tuple[str, ...]  # the actual warning/error lines (capped to _MAX_LINES)
    truncated: bool       # there were more matching lines than are shown
    unknown: int = 0      # UNKNOWN_PROPERTY lines (loguru stages only; not inline)


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
    warnings = errors = unknown = 0
    collected: list[str] = []
    inline = stage not in _NO_INLINE_STAGES
    for ln in text.splitlines():
        if loguru and _LOGURU_UNKNOWN.match(ln):
            unknown += 1
            continue
        is_err = bool(err_re.search(ln))
        # For loguru a line has exactly one level; the heuristic may match both,
        # so count it once (error wins) to avoid double counting.
        is_warn = bool(warn_re.search(ln)) and not (loguru and is_err)
        if is_err:
            errors += 1
        elif is_warn:
            warnings += 1
        if inline and (is_err or is_warn):
            if len(collected) < _MAX_LINES:
                collected.append(ln.rstrip())
    truncated = inline and (warnings + errors) > len(collected)
    return StepCount(stage, warnings, errors, not loguru, tuple(collected), truncated, unknown)


class RunReport:
    """Collects a run's outcome and renders the report (plain text + HTML).

    This is the patch-day report. Other run kinds (the discount run, see
    discount_report.py) subclass it and override the class attributes plus the
    ``facts`` / ``details`` / ``subject`` hooks; the step counts, log links,
    and the email path (alerts.py) are shared.
    """

    title = "WRFrontiersDB-Orchestrator run report"
    # Whether the run has UNKNOWN_PROPERTY lines worth a column (parser runs).
    track_unknown = True
    approx_note = ("SITE/SITE-DEPLOY counts are a text scan of npm/astro/gh "
                   "output, not loguru levels.")

    def __init__(self, runlog, *, game_version: str | None = None) -> None:
        self._runlog = runlog
        self.game_version = game_version
        self.result = "INCOMPLETE"  # set via finalize()
        # parse_warnings.ParseWarnings once the parse step's log was grouped;
        # None if parse never ran or the grouping failed.
        self.parse_warnings = None

    def finalize(self, result: str, *, game_version: str | None = None) -> None:
        """Record the run's outcome (e.g. 'COMPLETE', 'FAILED at PARSE')."""
        self.result = result
        if game_version is not None:
            self.game_version = game_version

    def counts(self) -> list[StepCount]:
        return [_count_file(stage, path) for stage, path in self._runlog.stage_logs]

    def totals(self) -> tuple[int, int, int]:
        """(warnings, errors, unknown properties) across every step reached."""
        counts = self.counts()
        return (sum(c.warnings for c in counts), sum(c.errors for c in counts),
                sum(c.unknown for c in counts))

    def log_files(self) -> list[Path]:
        """Every log file this run wrote (stage logs in order, then run.log)."""
        paths = [path for _stage, path in self._runlog.stage_logs]
        paths.append(self._runlog.run_log)
        if self.parse_warnings is not None and self.parse_warnings.report_path:
            paths.append(self.parse_warnings.report_path)
        return [Path(p) for p in paths if Path(p).is_file()]

    def run_dir_name(self) -> str:
        """The run dir's name (its timestamp), used to name the attachments."""
        return Path(self._runlog.run_dir).name

    def hs_command(self) -> str:
        """A copy-paste command to reach this run's logs on the home-server.

        `hs` (dev's shell function) SSHes to the box and runs the command, then
        drops into an interactive shell in the log dir. Each run gets its own dir,
        which holds ONLY this run's files, so a plain `ls -lh` there lists exactly
        the relevant logs — and keeps the command short enough not to line-wrap.
        """
        return f"hs 'cd {self._runlog.run_dir} && ls -lh'"

    def subject(self) -> str:
        version = self.game_version or "unknown"
        text = f"WRFrontiersDB {version} - {self.result}: {self._totals_text()}"
        pw = self.parse_warnings
        if pw is not None:
            text += f" ({len(pw.groups)} groups" + (
                f", {pw.new_count} NEW)" if pw.baseline else ")")
        return text

    def facts(self) -> list[tuple[str, str]]:
        """Label/value lines heading the report, above Result and Totals."""
        return [("Patch", self.game_version or "unknown")]

    def details(self) -> list[tuple[str, list[str]]]:
        """Extra (heading, lines) sections shown after the step table."""
        return self._parse_warning_details()

    def _parse_warning_details(self) -> list[tuple[str, list[str]]]:
        parsed = any(stage == "parse" for stage, _path in self._runlog.stage_logs)
        if not parsed:
            return []
        pw = self.parse_warnings
        if pw is None:
            return [("Parse warning groups",
                     ["unavailable: the grouping failed (see run.log)"])]
        heading = f"Parse warning groups ({len(pw.groups)}"
        heading += (f"; {pw.new_count} NEW vs {pw.baseline})" if pw.baseline
                    else "; no baseline for NEW)")
        if not pw.groups:
            return [(heading, ["none - clean parse"])]
        kind_w = max(len(g.kind) for g in pw.groups)
        count_w = max(len(str(g.count)) for g in pw.groups) + 1
        lines = [
            f"{g.kind:<{kind_w}}  {str(g.count) + 'x':>{count_w}}  "
            f"{_clip(g.title, _GROUP_TITLE_WIDTH)}" + ("  NEW" if g.new else "")
            for g in pw.groups[:_MAX_GROUPS]
        ]
        more = len(pw.groups) - _MAX_GROUPS
        where = pw.report_path.name if pw.report_path else "the parse log"
        if more > 0:
            lines.append(f"... {more} more in {where}")
        elif pw.report_path:
            lines.append(f"(examples and export files: {where}, attached)")
        return [(heading, lines)]

    def _totals_text(self) -> str:
        warns, errs, unknown = self.totals()
        text = f"{warns} warnings, {errs} errors"
        if self.track_unknown:
            text += f", {unknown} unknown properties"
        return text

    # -- plain text ------------------------------------------------------------
    def body(self) -> str:
        counts = self.counts()
        facts = self.facts() + [("Result", self.result), ("Totals", self._totals_text())]
        label_w = max(len(label) for label, _value in facts) + 1

        out = [self.title]
        out += [f"{label + ':':<{label_w}} {value}" for label, value in facts]
        out += ["", "Steps reached (warnings / errors"
                + (" / unknown properties):" if self.track_unknown else "):")]
        if counts:
            width = max(len(c.stage) for c in counts)
            for c in counts:
                flag = " ~approx" if c.approx else ""
                unknown = f" / {c.unknown}U" if self.track_unknown else ""
                out.append(f"  {c.stage:<{width}}  {c.warnings}W / {c.errors}E{unknown}{flag}")
        else:
            out.append("  (no steps ran)")

        for heading, lines in self.details():
            out += ["", f"{heading}:"] + [f"  {ln}" for ln in lines]

        detailed = [c for c in counts if c.lines]
        if detailed:
            out += ["", "Warnings / errors:"]
            for c in detailed:
                out.append(f"  [{c.stage}]")
                out += [f"    {ln}" for ln in c.lines]
                if c.truncated:
                    out.append(f"    ... (more in the log; showing first {_MAX_LINES})")

        out += ["", "Open the logs on the home-server:", f"  {self.hs_command()}"]

        out += ["", "Logs:", f"  {_uri(self._runlog.run_dir)}"]
        for _stage, path in self._runlog.stage_logs:
            out.append(f"  {_uri(path)}")
        out.append(f"  {_uri(self._runlog.run_log)}")

        if any(c.approx for c in counts):
            out += ["", f"(~approx: {self.approx_note})"]
        return "\n".join(out) + "\n"

    # -- HTML (hyperlinked) ----------------------------------------------------
    def body_html(self) -> str:
        counts = self.counts()
        warns, errs, unknown = self.totals()

        p = ['<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
             'font-size:14px;line-height:1.5">']
        p.append(f"<h2 style='margin:0 0 8px'>{html.escape(self.title)}</h2>")
        totals = f"<b>{warns}</b> warnings, <b>{errs}</b> errors"
        if self.track_unknown:
            totals += f", <b>{unknown}</b> unknown properties"
        facts = [f"{html.escape(label)}: <b>{html.escape(value)}</b>"
                 for label, value in self.facts() + [("Result", self.result)]]
        facts.append(f"Totals: {totals}")
        p.append(f"<p style='margin:0 0 12px'>{'<br>'.join(facts)}</p>")

        p.append("<h3 style='margin:12px 0 4px'>Steps reached</h3>")
        if counts:
            p.append("<table cellpadding='4' style='border-collapse:collapse'>")
            p.append("<tr><th align='left'>step</th><th align='right'>warnings</th>"
                     "<th align='right'>errors</th>"
                     + ("<th align='right'>unknown properties</th>"
                        if self.track_unknown else "") + "</tr>")
            for c in counts:
                stage = html.escape(c.stage) + (" <i>~approx</i>" if c.approx else "")
                unknown = (f"<td align='right'>{c.unknown}</td>"
                           if self.track_unknown else "")
                p.append(f"<tr><td>{stage}</td><td align='right'>{c.warnings}</td>"
                         f"<td align='right'>{c.errors}</td>{unknown}</tr>")
            p.append("</table>")
        else:
            p.append("<p>(no steps ran)</p>")

        for heading, lines in self.details():
            p.append(f"<h3 style='margin:12px 0 4px'>{html.escape(heading)}</h3>")
            # Monospace + preserved spaces: detail lines may be column-aligned.
            p.append("<ul style='margin:0;font-family:ui-monospace,Menlo,Consolas,"
                     "monospace;font-size:13px;white-space:pre-wrap'>"
                     + "".join(f"<li>{html.escape(ln)}</li>" for ln in lines)
                     + "</ul>")

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

        p.append("<h3 style='margin:12px 0 4px'>Open the logs on the home-server</h3>")
        # white-space:pre (not pre-wrap): keep the command on one line so a copy
        # never picks up a soft-wrap newline; scroll horizontally if it's long.
        p.append("<pre style='margin:0 0 8px;padding:8px;background:#f4f4f4;"
                 "border-radius:4px;overflow-x:auto;white-space:pre'>"
                 f"{html.escape(self.hs_command())}</pre>")

        p.append("<h3 style='margin:12px 0 4px'>Logs</h3>")
        p.append("<ul style='margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;"
                 "font-size:13px'>")
        p.append(f"<li>{_link(self._runlog.run_dir)}</li>")
        for _stage, path in self._runlog.stage_logs:
            p.append(f"<li>{_link(path)}</li>")
        p.append(f"<li>{_link(self._runlog.run_log)}</li>")
        p.append("</ul>")

        if any(c.approx for c in counts):
            p.append("<p style='color:#666;font-size:12px'>~approx: "
                     f"{html.escape(self.approx_note)}</p>")
        p.append("</div>")
        return "\n".join(p)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _uri(path: Path) -> str:
    """Absolute file:// URI for a log path (resolve first — as_uri needs abs)."""
    return Path(path).resolve().as_uri()


def _link(path: Path) -> str:
    # Show the FULL file:// path as the visible text (copy-pasteable everywhere);
    # keep the href for desktop clients that honour file:// (webmail strips it).
    uri = _uri(path)
    return f'<a href="{html.escape(uri, quote=True)}">{html.escape(uri)}</a>'
