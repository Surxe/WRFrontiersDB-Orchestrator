"""src/measure_memory.py's pure parts: stage stamps from the orchestrator's output,
the per-stage summary, and the cgroup file parsers.

No cgroups or systemd: output lines are fed in, cgroup files are temp files.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from measure_memory import StageTracker, read_kv, read_psi, summarize  # noqa: E402


class StageTrackerTest(unittest.TestCase):
    def banner(self, tracker, text):
        for line in ("INFO | logging_stream:banner:74 - ", "=" * 70, text, "=" * 70):
            tracker.feed(line + "\n")

    def test_stages_and_markers(self):
        t = StageTracker()
        self.assertEqual(t.current, "preflight")
        self.banner(t, "WRFrontiersDB-Orchestrator \u2014 patch 2026-10-20")
        self.assertEqual(t.current, "preflight")
        self.banner(t, "EXPORT")
        self.assertEqual(t.current, "export")
        # The Exporter's own rules carry a log prefix, so they are not banners.
        t.feed("INFO | run:run_mapper_creation:149 - " + "=" * 60 + "\n")
        t.feed("INFO | run:run_mapper_creation:150 - STEP 3: DLL INJECTION FOR MAPPER FILE\n")
        self.assertEqual(t.current, "export:mapper")
        t.feed("[process: WRFrontiersDB-CUE4Parse] Phase maps: 12 released map(s): A\n")
        # The end-of-run summary must not count as a phase start.
        t.feed("[process: WRFrontiersDB-CUE4Parse] Phase assets: 16172/16172 in 01:20\n")
        self.assertEqual(t.current, "export:cue4parse-maps")
        self.banner(t, "PARSE")
        t.feed("INFO | enrichment:enrich:67 - Starting enrichment phase...\n")
        self.assertEqual(t.current, "parse:enrich")
        self.banner(t, "SITE-DEPLOY")
        self.assertEqual(t.current, "site-deploy")
        self.banner(t, "Pipeline complete")
        self.assertEqual(t.current, "finish")


class SummaryTest(unittest.TestCase):
    def row(self, t, stage, anon, file, swap=0):
        return {"elapsed_s": t, "stage": stage, "current": anon + file, "anon": anon,
                "file": file, "shmem": 0, "kernel": 0, "swap": swap,
                "anon_plus_file": anon + file, "psi_some_avg10": 0.0,
                "host_MemAvailable": 100, "host_SwapFree": 50, "vm_rss": 7, "vm_swap": 0}

    def test_peaks_per_stage(self):
        rows = [self.row(0, "a", 10, 5), self.row(1, "a", 20, 1),
                self.row(2, "b", 5, 30, swap=3)]
        a, b = summarize(rows)
        self.assertEqual((a["stage"], a["duration_s"], a["peak_anon"], a["peak_anon_plus_file"]),
                         ("a", 2.0, 20, 21))
        self.assertEqual((b["peak_anon"], b["peak_current"], b["peak_swap"]), (5, 35, 3))


class ParsersTest(unittest.TestCase):
    def test_kv_and_psi(self):
        with tempfile.TemporaryDirectory() as tmp:
            stat = Path(tmp, "memory.stat")
            stat.write_text("anon 4096\nfile 8192\n")
            self.assertEqual(read_kv(stat), {"anon": 4096, "file": 8192})
            psi = Path(tmp, "memory.pressure")
            psi.write_text("some avg10=1.50 avg60=0.00 avg300=0.00 total=1\n"
                           "full avg10=0.25 avg60=0.00 avg300=0.00 total=1\n")
            self.assertEqual(read_psi(psi), (1.5, 0.25))
            self.assertEqual(read_kv(Path(tmp, "missing")), {})


if __name__ == "__main__":
    unittest.main()
