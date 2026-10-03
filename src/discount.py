#!/usr/bin/env python3
"""WRFrontiersDB-Orchestrator — discount run driver.

preflight -> scrape -> watch, each streamed to a per-stage log, wrapping
WRFrontiers-News-Scraper's pipeline (scrape the latest news posts; on a new
weekly discount, dispatch the Discount-Visualizer). Run on a timer several times
a day (home-server's hs-wrf-discount-watch.service).

Reporting works like the patch-day run (run.py): the run report, with the logs
attached, is emailed via alerts.send_report using the same SMTP_* options. The
difference is volume: most polls find nothing new, so a run is emailed only when
it found a new discount week or something went wrong. A quiet poll (no new week,
no warnings/errors) sends nothing and deletes its own log dir, so the timer
doesn't pile up empty runs; its output is still in the journal.

Examples:
    python src/discount.py --log-dir /srv/dev/wrf/discount-logs
    WRF_DISPATCH=1 python src/discount.py --log-dir /srv/dev/wrf/discount-logs
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

from loguru import logger  # noqa: E402
from optionsconfig import init_options, ArgumentWriter  # noqa: E402

import alerts  # noqa: E402
import config  # noqa: E402
from discount_report import DiscountReport  # noqa: E402
from logging_stream import LOG_FORMAT, RunLogger  # noqa: E402
from repos import Repos  # noqa: E402
from stages import discount as discount_stage  # noqa: E402

# Results of a poll that found nothing to act on.
_QUIET_RESULTS = {"NO CHANGE", "NO DISCOUNT POST"}


def outcome(events: list[dict]) -> tuple[str, bool]:
    """(result, ok) for the watch step, from the events it printed.

    The last event decides it: watch_discount.py ends a run with exactly one of
    these (after `discount-announced`, if a new week was found).
    """
    last = events[-1]["event"] if events else None
    return {
        "dispatched": ("DISPATCHED", True),
        "dispatch-dry-run": ("DRY RUN (dispatch off)", True),
        "dispatch-error": ("DISPATCH FAILED", False),
        "dispatch-skipped": ("NOT DISPATCHED (missing date range or items)", False),
        "no-change": ("NO CHANGE", True),
        "no-discount-post-found": ("NO DISCOUNT POST", True),
        "primed": ("PRIMED", True),
    }.get(last, (f"UNKNOWN OUTCOME ({last or 'no events'})", False))


def main(args: argparse.Namespace) -> int:
    config.load_secrets()
    options = init_options(args=args, log_file=None)
    repos = Repos(wrf_root=options.wrf_root, repos_dir=options.repos_dir)

    runlog = RunLogger(options.log_dir, options.log_level)
    report = DiscountReport(runlog)

    rc = 1
    try:
        rc = _run_discount(repos, runlog, report, args.latest)
        return rc
    finally:
        warns, errs, _unknown = report.totals()
        if (report.result in _QUIET_RESULTS and rc == 0
                and warns == 0 and errs == 0):
            _discard_quiet_run(runlog, report.result)
        else:
            alerts.send_report(options, report)


def _run_discount(repos: Repos, runlog: RunLogger, report: DiscountReport,
                  latest: int) -> int:
    with runlog.stage_sink("preflight"):
        scripts = repos.news_scraper_dir / "scripts"
        missing = [p for p in (scripts / "archive.py", scripts / "watch_discount.py")
                   if not p.is_file()]
        if missing:
            logger.error("News-Scraper scripts not found: "
                         + ", ".join(str(p) for p in missing))
            report.finalize("PREFLIGHT FAILED")
            return 2

    runlog.banner("WRFrontiersDB-Orchestrator — discount run")

    runlog.banner("SCRAPE")
    if (rc := discount_stage.scrape(repos, runlog, latest)) != 0:
        logger.error(f"SCRAPE FAILED (exit {rc}) — see its stage log")
        report.finalize("FAILED at SCRAPE")
        return 1

    runlog.banner("WATCH")
    rc = discount_stage.watch(repos, runlog, latest)
    events = discount_stage.read_events(runlog.stage_logs[-1][1])
    report.announced = next(
        (e for e in events if e["event"] == "discount-announced"), None)
    if rc != 0:
        logger.error(f"WATCH FAILED (exit {rc}) — see its stage log")
        report.finalize("FAILED at WATCH")
        return 1

    result, ok = outcome(events)
    report.finalize(result)
    if not ok:
        logger.error(f"Discount run: {result}")
        return 1
    runlog.banner(f"Discount run complete: {result}")
    return 0


def _discard_quiet_run(runlog: RunLogger, result: str) -> None:
    """Drop a quiet poll's log dir; its output already went to the console."""
    logger.remove()  # close the run.log / stage sinks before deleting their dir
    logger.add(sys.stdout, level=runlog.log_level, format=LOG_FORMAT)
    shutil.rmtree(runlog.run_dir, ignore_errors=True)
    logger.info(f"{result}: nothing to report; no email, run dir removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WRFrontiersDB-Orchestrator — discount run driver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--latest", type=int, default=3,
        help="How many of the newest news posts to scrape and check (default: 3).",
    )
    ArgumentWriter().add_arguments(parser)
    sys.exit(main(parser.parse_args()))
