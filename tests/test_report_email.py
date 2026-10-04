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
import parse_warnings  # noqa: E402
from parse_warnings import ParseWarnings, WarningGroup  # noqa: E402
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
                "UNKNOWN_PROPERTY | utils:p:6 - Ability A.0 has unknown property: 'K' of value '1'\n"
                "UNKNOWN_PROPERTY | utils:p:7 - Ability B.0 has unknown property: 'K' of value '2'\n"
                "UNKNOWN_PROPERTY | utils:p:8 - Ability C.0 has unknown property: 'K' of value '3'\n"
                "ERROR | parse:y:4 - boom\n"
                "CRITICAL | parse:z:5 - dead\n"
            ),
        })
        counts = {c.stage: c for c in rep.counts()}
        self.assertEqual((counts["preflight"].warnings, counts["preflight"].errors), (0, 0))
        self.assertEqual((counts["parse"].warnings, counts["parse"].errors), (2, 2))
        self.assertFalse(counts["parse"].approx)
        self.assertEqual(counts["parse"].unknown, 3)
        self.assertEqual(rep.totals(), (2, 2, 3))
        # The parse step's raw lines are not inline (they're summarised as warning
        # groups instead); unknown properties are only counted.
        self.assertEqual(counts["parse"].lines, ())
        self.assertFalse(counts["parse"].truncated)
        self.assertIn("3 unknown properties", rep.subject())
        self.assertIn("2W / 2E / 3U", rep.body())
        self.assertIn("<b>3</b> unknown properties", rep.body_html())
        self.assertNotIn("boom", rep.body())

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
        rep = self._report({"02-export.log": "ERROR | export:y:1 - boom\n"})
        rep.finalize("FAILED at EXPORT")
        body = rep.body()
        self.assertIn("FAILED at EXPORT", body)
        self.assertIn("file://", body)
        self.assertIn("02-export.log", body)
        self.assertIn("boom", body)  # the actual error line is inline
        self.assertIn("FAILED at EXPORT", rep.subject())
        self.assertIn("1 errors", rep.subject())

    def test_html_hyperlinks_and_escapes(self):
        rep = self._report({"02-export.log": "ERROR | export:y:1 - bad <x> & y\n"})
        rep.finalize("COMPLETE")
        html = rep.body_html()
        self.assertIn('<a href="file://', html)          # links are hyperlinked
        self.assertIn("02-export.log</a>", html)
        self.assertIn("bad &lt;x&gt; &amp; y", html)      # message text is escaped

    def test_hs_command_lists_this_runs_logs(self):
        rep = self._report({
            "01-preflight.log": "INFO | preflight:validate:1 - OK\n",
            "02-parse.log": "ERROR | parse:y:1 - boom\n",
        })
        cmd = rep.hs_command()
        self.assertTrue(cmd.startswith("hs 'cd "))
        self.assertTrue(cmd.endswith("&& ls -lh'"))  # per-run dir => plain ls
        self.assertNotIn("\n", cmd)                   # single line (no wrap risk)
        self.assertIn(cmd, rep.body())                # in the plain body (verbatim)
        self.assertIn("&amp;&amp; ls -lh", rep.body_html())  # HTML-escaped, present

    def test_lines_are_capped(self):
        many = "".join(f"ERROR | export:y:{i} - e{i}\n" for i in range(40))
        rep = self._report({"02-export.log": many})
        c = rep.counts()[0]
        self.assertEqual(c.errors, 40)          # count is exact
        self.assertEqual(len(c.lines), 25)      # inline lines are capped
        self.assertTrue(c.truncated)
        self.assertIn("showing first 25", rep.body())


class ParseWarningGroupTests(unittest.TestCase):
    """The parse step is summarised as warning groups (parse_warnings.py)."""

    _report = ReportCountTests._report

    def _groups(self, groups, *, baseline="2026-09-23_214314", report_path=None):
        return ParseWarnings(tuple(WarningGroup(*g) for g in groups), 1500,
                             baseline, report_path)

    def test_groups_listed_once_with_new_markers(self):
        rep = self._report({"03-parse.log": "WARNING | parse:x:1 - a\n" * 1488})
        md = rep._runlog.run_dir / "parse-warnings.md"
        md.write_text("# Parse warning report\n", encoding="utf-8")
        rep.parse_warnings = self._groups([
            ("warning", "analysis:get_ability_stat: Module <id>: index <n> out of range", 1488, False),
            ("unknown-property", "Ability: 'DamageResistance'", 1, True),
        ], report_path=md)
        body = rep.body()
        self.assertIn("Parse warning groups (2; 1 NEW vs 2026-09-23_214314):", body)
        self.assertIn("1488x  analysis:get_ability_stat", body)
        self.assertIn("Ability: 'DamageResistance'  NEW", body)
        self.assertNotIn("parse:x:1 - a", body)          # no raw lines
        self.assertIn("(2 groups, 1 NEW)", rep.subject())
        self.assertIn("DamageResistance&#x27;  NEW", rep.body_html())
        self.assertIn(md, rep.log_files())               # attached with the logs

    def test_clean_and_no_baseline(self):
        rep = self._report({"03-parse.log": "INFO | parse:x:1 - fine\n"})
        rep.parse_warnings = self._groups([], baseline=None)
        self.assertIn("Parse warning groups (0; no baseline for NEW):", rep.body())
        self.assertIn("none - clean parse", rep.body())
        self.assertTrue(rep.subject().endswith("(0 groups)"))

    def test_grouping_failed_is_said(self):
        rep = self._report({"03-parse.log": "WARNING | parse:x:1 - a\n"})
        self.assertIn("unavailable: the grouping failed", rep.body())
        self.assertNotIn("groups", rep.subject())

    def test_no_section_without_parse(self):
        rep = self._report({"02-export.log": "WARNING | export:x:1 - a\n"})
        self.assertNotIn("Parse warning groups", rep.body())

    def test_group_list_is_capped(self):
        rep = self._report({"03-parse.log": ""})
        rep.parse_warnings = self._groups(
            [("unknown-property", f"Ability: 'K{i}'", 1, False) for i in range(45)])
        body = rep.body()
        self.assertIn("Ability: 'K39'", body)
        self.assertNotIn("Ability: 'K40'", body)
        self.assertIn("... 5 more in the parse log", body)


class CollectTests(unittest.TestCase):
    """parse_warnings.collect runs the Parser's tool (faked here) read-only."""

    FAKE_TOOL = """import json, sys
args = sys.argv[1:]
open(sys.argv[0] + ".argv", "a").write(" ".join(args) + "\\n")
if "--json" in args:
    print(json.dumps({"log": "x", "baseline": "/w/logs/2026-09-23_214314/03-parse.log",
                      "export_dir": None, "total_lines": 3, "counts": {},
                      "groups": [{"kind": "warning", "title": "T", "count": 3, "new": True}]}))
elif "--out" in args:
    open(args[args.index("--out") + 1], "w").write("# report\\n")
sys.exit(int(__import__("os").environ.get("FAKE_RC", "1")))
"""

    def _setup(self, tmp: Path):
        parser_dir = tmp / "WRFrontiersDB-Parser"
        (parser_dir / "tools").mkdir(parents=True)
        tool = parser_dir / "tools" / "warning_report.py"
        tool.write_text(self.FAKE_TOOL, encoding="utf-8")
        run_dir = tmp / "run"
        run_dir.mkdir()
        repos = SimpleNamespace(parser_dir=parser_dir, venv_python=lambda _d: Path(sys.executable))
        return repos, run_dir, tool

    def test_collects_groups_and_writes_report(self):
        with tempfile.TemporaryDirectory() as t:
            repos, run_dir, tool = self._setup(Path(t))
            pw = parse_warnings.collect(repos, run_dir, Path(t))
            self.assertEqual(pw.groups, (WarningGroup("warning", "T", 3, True),))
            self.assertEqual(pw.baseline, "2026-09-23_214314")
            self.assertEqual(pw.report_path, run_dir / "parse-warnings.md")
            self.assertTrue(pw.report_path.is_file())
            calls = Path(str(tool) + ".argv").read_text().splitlines()
            self.assertTrue(all("--no-decisions" in c for c in calls))  # read-only

    def test_bad_exit_returns_none(self):
        with tempfile.TemporaryDirectory() as t:
            repos, run_dir, _tool = self._setup(Path(t))
            with mock.patch.dict("os.environ", {"FAKE_RC": "2"}):
                self.assertIsNone(parse_warnings.collect(repos, run_dir, Path(t)))

    def test_missing_tool_returns_none(self):
        with tempfile.TemporaryDirectory() as t:
            repos = SimpleNamespace(parser_dir=Path(t), venv_python=lambda _d: Path(sys.executable))
            self.assertIsNone(parse_warnings.collect(repos, Path(t), Path(t)))


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
        # multipart/mixed: the plain/HTML alternative body plus the log attachments.
        msg = sent.sent
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn('<a href="file://', html_part)
        plain_part = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("COMPLETE", plain_part)

    def _report_with_logs(self, files: dict[str, str]):
        tmp = Path(tempfile.mkdtemp()) / "2026-08-22_120000"
        tmp.mkdir()
        stage_logs = []
        for name, content in files.items():
            (tmp / name).write_text(content, encoding="utf-8")
            stage_logs.append((name.split("-", 1)[1].rsplit(".", 1)[0], tmp / name))
        (tmp / "run.log").write_text("INFO | run:main:1 - start\n", encoding="utf-8")
        rep = RunReport(_fake_runlog(tmp, stage_logs), game_version="2026-08-22")
        rep.finalize("COMPLETE")
        return rep

    def test_logs_attached_as_text(self):
        rep = self._report_with_logs({"01-preflight.log": "INFO | p:v:1 - OK\n",
                                      "02-parse.log": "ERROR | parse:y:1 - boom\n"})
        alerts.EmailAlerter(self._cfg(), smtp_factory=FakeSMTP).send(rep)
        atts = {a.get_filename(): a for a in FakeSMTP.last.sent.iter_attachments()}
        self.assertEqual(list(atts), ["01-preflight.log", "02-parse.log", "run.log"])
        self.assertEqual(atts["02-parse.log"].get_content_type(), "text/plain")
        self.assertIn("boom", atts["02-parse.log"].get_content())

    def test_big_logs_attached_as_one_zip(self):
        import io
        import zipfile
        rep = self._report_with_logs({"02-parse.log": "UNKNOWN_PROPERTY | x\n" * 50})
        with mock.patch.object(alerts, "_MAX_PLAIN_BYTES", 100):
            alerts.EmailAlerter(self._cfg(), smtp_factory=FakeSMTP).send(rep)
        atts = list(FakeSMTP.last.sent.iter_attachments())
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0].get_filename(), "2026-08-22_120000-logs.zip")
        with zipfile.ZipFile(io.BytesIO(atts[0].get_content())) as zf:
            self.assertEqual(sorted(zf.namelist()),
                             ["2026-08-22_120000/02-parse.log",
                              "2026-08-22_120000/run.log"])

    def test_huge_logs_sent_without_attachments(self):
        rep = self._report_with_logs({"02-parse.log": "x\n" * 50})
        with mock.patch.object(alerts, "_MAX_PLAIN_BYTES", 10), \
             mock.patch.object(alerts, "_MAX_ZIP_BYTES", 10):
            ok = alerts.EmailAlerter(self._cfg(), smtp_factory=FakeSMTP).send(rep)
        self.assertTrue(ok)  # the report still goes out
        self.assertEqual(list(FakeSMTP.last.sent.iter_attachments()), [])

    def test_smtp_error_is_caught(self):
        def boom(host, port):
            raise smtplib.SMTPException("nope")
        alerter = alerts.EmailAlerter(self._cfg(), smtp_factory=boom)
        self.assertFalse(alerter.send(self._report()))  # no raise


class SendTestEmailTests(unittest.TestCase):
    """src/send_test_email.py rebuilds a TEST report from an existing run dir."""

    def test_latest_run_and_stage_logs(self):
        import send_test_email
        log_dir = Path(tempfile.mkdtemp())
        (log_dir / "2026-09-30_220706").mkdir()
        run_dir = log_dir / "2026-10-01_234331"
        run_dir.mkdir()
        for name in ("01-preflight.log", "02-site-deploy.log", "run.log", "notes.txt"):
            (run_dir / name).write_text("INFO | x\n", encoding="utf-8")
        self.assertEqual(send_test_email.latest_run_dir(log_dir), run_dir)
        rep = send_test_email.report_from_run_dir(run_dir)
        self.assertEqual([c.stage for c in rep.counts()], ["preflight", "site-deploy"])
        self.assertEqual([p.name for p in rep.log_files()],
                         ["01-preflight.log", "02-site-deploy.log", "run.log"])
        self.assertIn("WRFrontiersDB TEST - TEST (from 2026-10-01_234331)", rep.subject())


class RunWiringTests(unittest.TestCase):
    """run.main sends the report on both the success and failure exit paths."""

    def _drive(self, extra_argv, *, rc_by_stage=None, expect_rc=0):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "--should-export", "false", "--should-parse", "false",
                "--should-push-data", "false", "--should-build-index", "false",
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

    def test_parse_grouped_before_email_even_on_failure(self):
        order = []
        with mock.patch.object(run.parse_warnings, "collect",
                               lambda *a, **k: order.append("grouped")), \
             mock.patch("stages.parse.run_streamed",
                        lambda cmd, *, cwd, stage, log_path, env=None:
                        (Path(log_path).write_text("", encoding="utf-8"), 1)[1]):
            sent = self._drive(["--should-parse", "true"], expect_rc=1)
        self.assertEqual(order, ["grouped"])
        self.assertEqual(sent, ["FAILED at PARSE"])

    def test_no_grouping_without_parse(self):
        with mock.patch.object(run.parse_warnings, "collect",
                               side_effect=AssertionError("should not run")):
            self._drive(["--should-build-site", "true"])

    def test_emails_on_stage_failure(self):
        sent = self._drive(["--should-build-site", "true"],
                           rc_by_stage={"site": 1}, expect_rc=1)
        self.assertEqual(sent, ["FAILED at SITE"])


if __name__ == "__main__":
    unittest.main()
