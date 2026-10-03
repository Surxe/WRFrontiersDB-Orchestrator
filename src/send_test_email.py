#!/usr/bin/env python3
"""Send a test run-report email built from an existing run's logs.

Exercises the real email path (alerts.send_report: compose, attachments, SMTP)
without running the pipeline. The report is rebuilt from a finished run's log
dir, with the patch shown as TEST so it can't be mistaken for a real run.

Options resolve exactly as in run.py (argument > .env > default; secrets from
the environment), so run it with the pipeline's environment to send for real —
the `send-test-email` skill does that via the wrf-orchestrator@ unit's
EnvironmentFile. Without the SMTP_* secrets it only logs the report.

Examples:
    python src/send_test_email.py --log-dir /srv/dev/wrf/logs        # latest run
    python src/send_test_email.py /srv/dev/wrf/logs/2026-10-01_234331
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

from loguru import logger  # noqa: E402
from optionsconfig import init_options, ArgumentWriter  # noqa: E402

import alerts  # noqa: E402
import config  # noqa: E402
from logging_stream import LOG_FORMAT  # noqa: E402
from report import RunReport  # noqa: E402

# Per-stage logs are NN-<stage>.log (logging_stream.RunLogger._next_path).
_STAGE_LOG = re.compile(r"^\d{2}-(?P<stage>.+)\.log$")


def latest_run_dir(log_dir: Path) -> Path | None:
    """The newest run dir under log_dir (names are sortable timestamps)."""
    runs = sorted(p for p in Path(log_dir).iterdir() if p.is_dir()) \
        if Path(log_dir).is_dir() else []
    return runs[-1] if runs else None


def report_from_run_dir(run_dir: Path) -> RunReport:
    """Rebuild a RunReport over an existing run dir's logs, marked as a test."""
    stage_logs = []
    for path in sorted(run_dir.iterdir()):
        m = _STAGE_LOG.match(path.name)
        if m:
            stage_logs.append((m["stage"], path))
    runlog = SimpleNamespace(run_dir=run_dir, run_log=run_dir / "run.log",
                             stage_logs=stage_logs)
    report = RunReport(runlog, game_version="TEST")
    report.finalize(f"TEST (from {run_dir.name})")
    return report


def main(args: argparse.Namespace) -> int:
    config.load_secrets()
    options = init_options(args=args, log_file=None)
    logger.remove()
    logger.add(sys.stdout, level="INFO", format=LOG_FORMAT)

    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir(options.log_dir)
    if run_dir is None or not run_dir.is_dir():
        logger.error(f"No run dir found (looked in {args.run_dir or options.log_dir}).")
        return 2

    logger.info(f"Sending a test run-report email from {run_dir}")
    if alerts.send_report(options, report_from_run_dir(run_dir)):
        return 0
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send a test run-report email built from an existing run's logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "run_dir", nargs="?",
        help="Run dir to build the report from. Default: the newest run under "
             "--log-dir.",
    )
    ArgumentWriter().add_arguments(parser)
    sys.exit(main(parser.parse_args()))
