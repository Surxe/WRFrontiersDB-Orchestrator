"""bin/wrf-deployed (src/deployed.py) and deploy_record's state/record parsing.

No network: the live fetch, the pipeline state and `gh` are all faked.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

import deploy_record  # noqa: E402
import deployed  # noqa: E402
from deploy_record import DeployRecordError  # noqa: E402

MAIN = "m" * 40
OLD = "o" * 40
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)


def rec(data_commit: str, run_id: str = "10") -> dict:
    return {"app": "x", "run_id": run_id, "run_url": f"https://example.invalid/{run_id}",
            "trigger": "push", "built_at_utc": "2026-10-05T12:00:00Z",
            "data_commit": data_commit, "data_version": "2026-09-29"}


def fake_gh(behind: int):
    def gh(args):
        assert args[0] == "api" and "/compare/" in args[1], args
        return {"ahead_by": behind}
    return gh


def check(live, state=None, behind=0, frontend=deploy_record.SITE):
    def fetch_live(frontend):
        if isinstance(live, Exception):
            raise live
        return live
    return deployed.check(frontend, MAIN, gh=fake_gh(behind),
                          fetch_live=fetch_live, read_state=lambda f: state)


class CheckTests(unittest.TestCase):
    def test_on_main(self):
        r = check(rec(MAIN), state=rec(MAIN))
        self.assertEqual(r["behind_main"], 0)
        self.assertTrue(r["state_matches_live"])
        self.assertIsNone(r["error"])

    def test_behind_main(self):
        r = check(rec(OLD), behind=3)
        self.assertEqual(r["behind_main"], 3)
        self.assertIsNone(r["state_matches_live"])  # no state recorded

    def test_deployed_outside_the_pipeline(self):
        r = check(rec(MAIN, run_id="11"), state=rec(OLD, run_id="10"))
        self.assertFalse(r["state_matches_live"])

    def test_unreachable(self):
        r = check(DeployRecordError("404"))
        self.assertIsNone(r["live"])
        self.assertEqual(r["error"], "404")


class ReportTests(unittest.TestCase):
    MAIN_COMMIT = {"sha": MAIN, "date_utc": "2026-10-04T20:10:39Z"}

    def test_all_current(self):
        results = {"Site": check(rec(MAIN), state=rec(MAIN)), "Visualizer": check(rec(MAIN))}
        self.assertTrue(deployed.all_current(results))
        text = "\n".join(deployed.report(self.MAIN_COMMIT, results, now=NOW))
        self.assertIn("Site        data mmmmmmm  version 2026-09-29  on main", text)
        self.assertIn("(6.0h ago) by push", text)
        self.assertIn("pipeline state: none recorded", text)
        self.assertNotIn("different Data commits", text)

    def test_behind_and_mismatched(self):
        results = {"Site": check(rec(OLD), behind=2), "Visualizer": check(rec(MAIN))}
        self.assertFalse(deployed.all_current(results))
        text = "\n".join(deployed.report(self.MAIN_COMMIT, results, now=NOW))
        self.assertIn("2 commit(s) behind main", text)
        self.assertIn("the frontends serve different Data commits", text)

    def test_visualizer_has_no_pipeline_state(self):
        self.assertIsNone(deploy_record.VISUALIZER.state_file)
        self.assertIsNone(deploy_record.read_state(deploy_record.VISUALIZER))
        results = {"Visualizer": check(rec(MAIN), frontend=deploy_record.VISUALIZER)}
        text = "\n".join(deployed.report(self.MAIN_COMMIT, results, now=NOW))
        self.assertNotIn("pipeline state", text)

    def test_error_is_not_current(self):
        results = {"Site": check(DeployRecordError("404"))}
        self.assertFalse(deployed.all_current(results))
        self.assertIn("ERROR: 404", deployed.report(self.MAIN_COMMIT, results, now=NOW)[1])


class StateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.frontend = dataclasses.replace(deploy_record.SITE,
                                            state_file=Path(tmp.name) / "state.json")

    def test_no_state_yet(self):
        self.assertIsNone(deploy_record.read_state(self.frontend))

    def test_state_without_data_commit_is_an_error(self):
        self.frontend.state_file.write_text('{"run_id": "1"}')
        with self.assertRaises(DeployRecordError):
            deploy_record.read_state(self.frontend)

    def test_record_url(self):
        self.assertEqual(deploy_record.SITE.record_url, "https://wrf-db.info/deploy.json")


if __name__ == "__main__":
    unittest.main()
