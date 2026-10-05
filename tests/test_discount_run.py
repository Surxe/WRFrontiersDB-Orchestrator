"""Tests for the discount run: outcome mapping, its report, and when it emails.

No network and no scraper: the two stages are stubbed to write a canned stage
log, and alerts.send_report is stubbed to record whether the run emailed.
"""
from __future__ import annotations

import argparse
import contextlib
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
import gh_runs  # noqa: E402
from discount_report import DiscountReport  # noqa: E402
from optionsconfig import ArgumentWriter  # noqa: E402
from stages import discount as discount_stage  # noqa: E402

@contextlib.contextmanager
def _null_sink(path):
    yield path


ANNOUNCED = {
    "event": "discount-announced", "id": 999, "title": "Intel & Salvage: TEST",
    "url": "https://example.invalid/news/999-test", "week_id": "2026-10-06",
    "week": "October 6 - 13", "date_range": "10-06 10-13",
    "items": ["Angler", "Some <Item>"], "first_run": False,
}


def _args(log_dir: Path, repos_dir: Path) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--latest", type=int, default=3)
    p.add_argument("--visualizer-timeout", type=float, default=1800)
    ArgumentWriter().add_arguments(p)
    return p.parse_args(["--log-dir", str(log_dir), "--repos-dir", str(repos_dir)])


def _vis(conclusion: str):
    return discount_stage.VisualizerRun(url="https://example.invalid/run/1",
                                        status="completed", conclusion=conclusion)


class FollowVisualizerTests(unittest.TestCase):
    """follow_visualizer against a scripted fake `gh` and a fake clock."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.runlog = SimpleNamespace(run_dir=tmp, stage_logs=[], log_level="DEBUG")
        self.runlog.stage_sink = lambda stage: _null_sink(tmp / f"{stage}.log")
        self.now = 1_000_000.0

    def _clock(self):
        return self.now

    def _sleep(self, secs):
        self.now += secs

    def _gh(self, lists, views):
        def gh(args):
            if args[:2] == ["run", "list"]:
                return lists.pop(0) if len(lists) > 1 else lists[0]
            return views.pop(0) if len(views) > 1 else views[0]
        return gh

    def _iso(self, ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _follow(self, lists, views, timeout=1800):
        return discount_stage.follow_visualizer(
            self.now, self.runlog, gh=self._gh(lists, views),
            sleep=self._sleep, clock=self._clock, timeout=timeout)

    def test_waits_for_new_run_then_completion(self):
        old = {"databaseId": 1, "createdAt": self._iso(self.now - 86400), "url": "u1"}
        new = {"databaseId": 2, "createdAt": self._iso(self.now + 5), "url": "u2"}
        res = self._follow([[old], [new, old]],
                           [{"status": "queued", "conclusion": ""},
                            {"status": "in_progress", "conclusion": ""},
                            {"status": "completed", "conclusion": "success", "jobs": []}])
        self.assertTrue(res.ok)
        self.assertEqual(res.url, "u2")       # the fresh run, not yesterday's

    def test_failure_reports_failed_jobs(self):
        new = {"databaseId": 2, "createdAt": self._iso(self.now), "url": "u2"}
        jobs = [{"name": "map", "conclusion": "failure",
                 "steps": [{"name": "Run Orchestrator Pipeline", "conclusion": "failure"}]}]
        with mock.patch.object(gh_runs.subprocess, "run",
                               side_effect=OSError("no gh")):
            res = self._follow([[new]], [{"status": "completed",
                                          "conclusion": "failure", "jobs": jobs}])
        self.assertFalse(res.ok)
        self.assertEqual(res.describe(), "failure - u2")

    def test_no_run_found(self):
        res = self._follow([[]], [])
        self.assertEqual(res.status, "not found")
        self.assertFalse(res.ok)

    def test_times_out(self):
        new = {"databaseId": 2, "createdAt": self._iso(self.now), "url": "u2"}
        res = self._follow([[new]], [{"status": "in_progress", "conclusion": ""}],
                           timeout=60)
        self.assertEqual(res.status, "timed out")
        self.assertFalse(res.ok)


class OutcomeTests(unittest.TestCase):
    def test_last_event_decides(self):
        self.assertEqual(discount.outcome([ANNOUNCED, {"event": "dispatched"}]),
                         ("DISPATCHED", True))
        self.assertEqual(discount.outcome([{"event": "no-change"}]), ("NO CHANGE", True))
        self.assertEqual(
            discount.outcome([ANNOUNCED, {"event": "already-published"}]),
            ("ALREADY PUBLISHED (deployed by hand)", True))
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
    def _run(self, events: list[dict], watch_rc: int = 0, vis=None):
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
             mock.patch.object(discount_stage, "follow_visualizer",
                               return_value=vis or _vis("success")) as follow, \
             mock.patch.object(discount.alerts, "send_report") as send:
            rc = discount.main(_args(tmp / "logs", tmp / "repos"))
        self.followed = follow.called
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
        self.assertTrue(self.followed)
        self.assertEqual(report.result, "DISPATCHED")
        self.assertIn(("Visualizer", "success - https://example.invalid/run/1"),
                      report.facts())
        self.assertEqual(report.announced["week_id"], "2026-10-06")
        self.assertEqual(len(run_dirs), 1)

    def test_failed_visualizer_run_fails_the_run(self):
        rc, send, _ = self._run([ANNOUNCED, {"event": "dispatched"}],
                                vis=_vis("failure"))
        self.assertEqual(rc, 1)
        self.assertEqual(send.call_args.args[1].result, "VISUALIZER FAILURE")

    def test_dry_run_does_not_follow(self):
        rc, send, _ = self._run([ANNOUNCED, {"event": "dispatch-dry-run"}])
        self.assertEqual(rc, 0)
        self.assertFalse(self.followed)
        send.assert_called_once()

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
