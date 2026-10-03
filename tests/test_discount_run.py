"""Tests for the discount run: outcome mapping, its report, and when it emails.

No network and no scraper: the two stages are stubbed to write a canned stage
log, and alerts.send_report is stubbed to record whether the run emailed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import discount  # noqa: E402
from discount_report import DiscountReport  # noqa: E402
from optionsconfig import ArgumentWriter  # noqa: E402
from stages import discount as discount_stage  # noqa: E402

ANNOUNCED = {
    "event": "discount-announced", "id": 999, "title": "Intel & Salvage: TEST",
    "url": "https://example.invalid/news/999-test", "week_id": "2026-10-06",
    "week": "October 6 - 13", "date_range": "10-06 10-13",
    "items": ["Angler", "Some <Item>"], "first_run": False,
}


def _args(log_dir: Path, repos_dir: Path) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--latest", type=int, default=3)
    ArgumentWriter().add_arguments(p)
    return p.parse_args(["--log-dir", str(log_dir), "--repos-dir", str(repos_dir)])


class OutcomeTests(unittest.TestCase):
    def test_last_event_decides(self):
        self.assertEqual(discount.outcome([ANNOUNCED, {"event": "dispatched"}]),
                         ("DISPATCHED", True))
        self.assertEqual(discount.outcome([{"event": "no-change"}]), ("NO CHANGE", True))
        self.assertFalse(discount.outcome([ANNOUNCED, {"event": "dispatch-error"}])[1])
        self.assertFalse(discount.outcome([ANNOUNCED, {"event": "dispatch-skipped"}])[1])

    def test_no_or_unknown_events_fail(self):
        self.assertEqual(discount.outcome([]), ("UNKNOWN OUTCOME (no events)", False))
        self.assertFalse(discount.outcome([{"event": "new-thing"}])[1])

    def test_read_events_skips_non_json(self):
        log = Path(tempfile.mkdtemp()) / "03-watch.log"
        log.write_text("$ python watch_discount.py\n"
                       + json.dumps(ANNOUNCED) + "\n{not json\n"
                       + json.dumps({"event": "dispatched"}) + "\n[watch] exit=0\n")
        self.assertEqual([e["event"] for e in discount_stage.read_events(log)],
                         ["discount-announced", "dispatched"])


class DiscountReportTests(unittest.TestCase):
    def _report(self) -> DiscountReport:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "02-scrape.log").write_text("ERROR fetching article 1: timeout\n")
        runlog = SimpleNamespace(run_dir=tmp, run_log=tmp / "run.log",
                                 stage_logs=[("scrape", tmp / "02-scrape.log")])
        rep = DiscountReport(runlog)
        rep.announced = ANNOUNCED
        rep.finalize("DISPATCHED")
        return rep

    def test_subject_and_body(self):
        rep = self._report()
        self.assertEqual(rep.subject(),
                         "WRF discount 2026-10-06 - DISPATCHED: 0 warnings, 1 errors")
        body = rep.body()
        self.assertIn("discount run report", body)
        self.assertIn("Range:  10-06 10-13", body)
        self.assertIn("  Angler", body)
        self.assertNotIn("unknown", body)          # no parser, no unknown column
        self.assertIn("timeout", body)             # error line is inline

    def test_html_escapes_items(self):
        html = self._report().body_html()
        self.assertIn("Some &lt;Item&gt;", html)
        self.assertNotIn("unknown properties", html)


class EmailDecisionTests(unittest.TestCase):
    def _run(self, events: list[dict], watch_rc: int = 0):
        tmp = Path(tempfile.mkdtemp())
        scripts = tmp / "repos" / "WRFrontiers-News-Scraper" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("archive.py", "watch_discount.py"):
            (scripts / name).write_text("")

        def fake_stage(stage, rc, text):
            def run(repos, runlog, latest):
                runlog.stage_log_path(stage).write_text(text)
                return rc
            return run

        watch_text = "".join(json.dumps(e) + "\n" for e in events)
        with mock.patch.object(discount_stage, "scrape", fake_stage("scrape", 0, "Done.\n")), \
             mock.patch.object(discount_stage, "watch", fake_stage("watch", watch_rc, watch_text)), \
             mock.patch.object(discount.alerts, "send_report") as send:
            rc = discount.main(_args(tmp / "logs", tmp / "repos"))
        return rc, send, list((tmp / "logs").iterdir())

    def test_quiet_poll_sends_nothing_and_cleans_up(self):
        rc, send, run_dirs = self._run([{"event": "no-change"}])
        self.assertEqual(rc, 0)
        send.assert_not_called()
        self.assertEqual(run_dirs, [])

    def test_new_week_emails(self):
        rc, send, run_dirs = self._run([ANNOUNCED, {"event": "dispatched"}])
        self.assertEqual(rc, 0)
        send.assert_called_once()
        report = send.call_args.args[1]
        self.assertEqual(report.result, "DISPATCHED")
        self.assertEqual(report.announced["week_id"], "2026-10-06")
        self.assertEqual(len(run_dirs), 1)

    def test_failed_dispatch_emails_and_fails(self):
        rc, send, _ = self._run([ANNOUNCED, {"event": "dispatch-error", "error": "x"}])
        self.assertEqual(rc, 1)
        send.assert_called_once()

    def test_failed_watch_emails(self):
        rc, send, _ = self._run([], watch_rc=1)
        self.assertEqual(rc, 1)
        self.assertEqual(send.call_args.args[1].result, "FAILED at WATCH")


if __name__ == "__main__":
    unittest.main()
