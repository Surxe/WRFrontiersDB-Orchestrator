"""Discount run report — the patch-day run report, re-titled for a discount run.

Same step counts, inline warning/error lines, log links and log attachments as
the patch-day email (report.RunReport, sent by alerts.send_report); this adds
what the discount run found: the announced week, its post, and the items handed
to the Discount-Visualizer.
"""

from __future__ import annotations

from report import RunReport


class DiscountReport(RunReport):
    title = "WRFrontiersDB-Orchestrator discount run report"
    track_unknown = False
    approx_note = ("scrape/watch counts are a text scan of the News-Scraper "
                   "scripts' output, not loguru levels.")

    def __init__(self, runlog) -> None:
        super().__init__(runlog)
        # The watch step's `discount-announced` event, once it has run.
        self.announced: dict | None = None

    def subject(self) -> str:
        week = (self.announced or {}).get("week_id") or "no new week"
        return f"WRF discount {week} - {self.result}: {self._totals_text()}"

    def facts(self) -> list[tuple[str, str]]:
        a = self.announced
        if not a:
            return [("Week", "no new week")]
        return [
            ("Week", f"{a.get('week') or '?'} (id {a.get('week_id') or '?'})"),
            ("Range", a.get("date_range") or "?"),
            ("Post", f"{a.get('title') or '?'} - {a.get('url') or '?'}"),
        ]

    def details(self) -> list[tuple[str, list[str]]]:
        items = (self.announced or {}).get("items") or []
        return [("Items sent to the visualizer", list(items))] if items else []
