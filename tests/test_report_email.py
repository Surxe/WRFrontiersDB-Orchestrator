"""Tests for the run-report email: counting, the enable gate, delivery, wiring.

No network: SMTP is a fake context manager, and the run.main wiring test stubs
alerts.send_report to record that it fired on every exit path.
"""
from __future__ import annotations

import argparse
import smtplib
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

import alerts  # noqa: E402
import run  # noqa: E402
from optionsconfig import ArgumentWriter  # noqa: E402
from report import RunReport  # noqa: E402


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--patch-day", action="store_true")
    p.add_argument("--force-patch-day", action="store_true")
    ArgumentWriter().add_arguments(p)
    return p.parse_args(argv)


def _fake_runlog(run_dir: Path, stage_logs):
    return SimpleNamespace(
        run_dir=run_dir,
        run_log=run_dir / "run.log",
        stage_logs=stage_logs,
    )


class ReportCountTests(unittest.TestCase):
    def _report(self, files: dict[str, str]) -> RunReport:
        tmp = Path(tempfile.mkdtemp())
        stage_logs = []
        for name, content in files.items():
            stage = name.split("-", 1)[1].rsplit(".", 1)[0]  # NN-<stage>.log
            path = tmp / name
            path.write_text(content, encoding="utf-8")
            stage_logs.append((stage, path))
        (tmp / "run.log").write_text("", encoding="utf-8")
        return RunReport(_fake_runlog(tmp, stage_logs), game_version="2026-08-22")

    def test_loguru_counts_are_exact(self):
        rep = self._report({
            "01-preflight.log": "INFO | preflight:validate:1 - OK\n",
            "02-parse.log": (
                "INFO | parse:main:1 - start\n"
                "WARNING | parse:x:2 - a\n"
                "WARNING | parse:x:3 - b\n"
                "ERROR | parse:y:4 - boom\n"
                "CRITICAL | parse:z:5 - dead\n"
            ),
        })
        counts = {c.stage: c for c in rep.counts()}
        self.assertEqual((counts["preflight"].warnings, counts["preflight"].errors), (0, 0))
        self.assertEqual((counts["parse"].warnings, counts["parse"].errors), (2, 2))
        self.assertFalse(counts["parse"].approx)
        self.assertEqual(rep.totals(), (2, 2))
        # The actual warning/error lines are captured inline (self-contained email).
        self.assertEqual(len(counts["parse"].lines), 4)
        self.assertTrue(any("boom" in ln for ln in counts["parse"].lines))

    def test_site_counts_are_heuristic(self):
        rep = self._report({
            "05-site.log": (
                "$ npm run build\n"
                "warning: something deprecated\n"
                "npm ERR! build failed\n"
                "Error: type mismatch\n"
            ),
        })
        c = rep.counts()[0]
        self.assertEqual(c.stage, "site")
        self.assertTrue(c.approx)
        self.assertGreaterEqual(c.warnings, 1)
        self.assertGreaterEqual(c.errors, 1)

    def test_body_has_uris_result_and_inline_lines(self):
        rep = self._report({"02-parse.log": "ERROR | parse:y:1 - boom\n"})
        rep.finalize("FAILED at PARSE")
        body = rep.body()
        self.assertIn("FAILED at PARSE", body)
        self.assertIn("file://", body)
        self.assertIn("02-parse.log", body)
        self.assertIn("boom", body)  # the actual error line is inline
        self.assertIn("FAILED at PARSE", rep.subject())
        self.assertIn("1 errors", rep.subject())

    def test_html_hyperlinks_and_escapes(self):
        rep = self._report({"02-parse.log": "ERROR | parse:y:1 - bad <x> & y\n"})
        rep.finalize("COMPLETE")
        html = rep.body_html()
        self.assertIn('<a href="file://', html)          # links are hyperlinked
        self.assertIn("02-parse.log</a>", html)
        self.assertIn("bad &lt;x&gt; &amp; y", html)      # message text is escaped

    def test_lines_are_capped(self):
        many = "".join(f"ERROR | parse:y:{i} - e{i}\n" for i in range(40))
        rep = self._report({"02-parse.log": many})
        c = rep.counts()[0]
        self.assertEqual(c.errors, 40)          # count is exact
        self.assertEqual(len(c.lines), 25)      # inline lines are capped
        self.assertTrue(c.truncated)
        self.assertIn("showing first 25", rep.body())


class EmailConfigTests(unittest.TestCase):
    def _opts(self, **over):
        base = dict(smtp_host="smtp.gmail.com", smtp_port=587,
                    smtp_user=None, smtp_password=None, email_to=None)
        base.update(over)
        return SimpleNamespace(**base)

    def test_disabled_when_any_missing(self):
        self.assertIsNone(alerts.EmailConfig.from_options(self._opts()))
        self.assertIsNone(alerts.EmailConfig.from_options(
            self._opts(smtp_user="a@b.com", smtp_password="pw")))  # no recipient

    def test_enabled_when_all_present(self):
        cfg = alerts.EmailConfig.from_options(
            self._opts(smtp_user="a@b.com", smtp_password="pw", email_to="c@d.com"))
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.host, "smtp.gmail.com")
        self.assertEqual(cfg.port, 587)
        self.assertEqual(cfg.to_addr, "c@d.com")


class FakeSMTP:
    """Records the STARTTLS/login/send sequence; no network."""
    last: "FakeSMTP | None" = None

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.tls = False
        self.login_args = None
        self.sent = None
        FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent = msg


class EmailAlerterTests(unittest.TestCase):
    def _cfg(self):
        return alerts.EmailConfig(host="h", port=587, user="from@x.com",
                                  password="pw", to_addr="to@y.com")

    def _report(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "run.log").write_text("", encoding="utf-8")
        rep = RunReport(_fake_runlog(tmp, []), game_version="2026-08-22")
        rep.finalize("COMPLETE")
        return rep

    def test_send_delivers_and_composes(self):
        alerter = alerts.EmailAlerter(self._cfg(), smtp_factory=FakeSMTP)
        ok = alerter.send(self._report())
        self.assertTrue(ok)
        sent = FakeSMTP.last
        self.assertTrue(sent.tls)
        self.assertEqual(sent.login_args, ("from@x.com", "pw"))
        self.assertEqual(sent.sent["From"], "from@x.com")
        self.assertEqual(sent.sent["To"], "to@y.com")
        self.assertIn("COMPLETE", sent.sent["Subject"])
        # multipart/alternative: a plain text part and a hyperlinked HTML part.
        msg = sent.sent
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn('<a href="file://', html_part)

    def test_smtp_error_is_caught(self):
        def boom(host, port):
            raise smtplib.SMTPException("nope")
        alerter = alerts.EmailAlerter(self._cfg(), smtp_factory=boom)
        self.assertFalse(alerter.send(self._report()))  # no raise


class RunWiringTests(unittest.TestCase):
    """run.main sends the report on both the success and failure exit paths."""

    def _drive(self, extra_argv, *, rc_by_stage=None, expect_rc=0):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "--should-export", "false", "--should-parse", "false",
                "--should-push-data", "false", "--should-detect-releases", "false",
            ] + extra_argv + [
                "--game-version", "2026-08-22", "--assume-manifest-confirmed", "true",
                "--log-dir", tmp,
            ]
            args = _parse(argv)

            def rec_run_streamed(cmd, *, cwd, stage, log_path, env=None):
                Path(log_path).write_text("", encoding="utf-8")
                return (rc_by_stage or {}).get(stage, 0)

            with mock.patch.object(run.preflight, "validate", lambda *a, **k: None), \
                 mock.patch.object(run.Repos, "prune_old_versions", lambda *a, **k: None), \
                 mock.patch.object(run.alerts, "send_report",
                                   lambda opts, report: sent.append(report.result)), \
                 mock.patch("stages.site.run_streamed", rec_run_streamed), \
                 mock.patch("stages.site_deploy.run_streamed", rec_run_streamed):
                rc = run.main(args)
        self.assertEqual(rc, expect_rc)
        return sent

    def test_emails_on_success(self):
        sent = self._drive(["--should-build-site", "true"])
        self.assertEqual(sent, ["COMPLETE"])

    def test_emails_on_stage_failure(self):
        sent = self._drive(["--should-build-site", "true"],
                           rc_by_stage={"site": 1}, expect_rc=1)
        self.assertEqual(sent, ["FAILED at SITE"])


if __name__ == "__main__":
    unittest.main()
