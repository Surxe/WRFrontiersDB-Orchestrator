"""Streamed, teed subprocess runner.

Fixes the long-standing "it pauses, only the .log has the output" problem: a
child Python process block-buffers stdout when it writes to a pipe instead of a
TTY, so nothing surfaces until the buffer flushes (often only at exit). We run
every child with PYTHONUNBUFFERED=1, merge stderr into stdout, and read the
stream line by line, writing each line to BOTH the console (live) and a
per-stage log file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


class RunLogger:
    """Owns one run's timestamped log directory."""

    def __init__(self, log_dir: Path) -> None:
        self.run_dir = Path(log_dir) / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_log = self.run_dir / "run.log"
        self._n = 0

    def banner(self, text: str) -> None:
        line = f"{'=' * 70}\n{text}\n{'=' * 70}"
        print(line, flush=True)
        with self.run_log.open("a") as fh:
            fh.write(line + "\n")

    def stage_log_path(self, stage: str) -> Path:
        self._n += 1
        return self.run_dir / f"{self._n:02d}-{stage}.log"


def run_streamed(
    cmd: list[str],
    *,
    cwd: Path,
    stage: str,
    log_path: Path,
    env: dict | None = None,
) -> int:
    """Run cmd, streaming merged stdout/stderr to console and to log_path.

    Returns the child's exit code. Never raises on a non-zero exit — the caller
    decides what a non-zero code means (the mapper, for instance, tolerates a
    gamescope teardown segfault because success is the .usmap existing).
    """
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    child_env["PYTHONUNBUFFERED"] = "1"

    start = time.time()
    header = f"$ (cwd={cwd})\n$ {' '.join(cmd)}"
    print(header, flush=True)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        log.write(header + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
        proc.wait()
        elapsed = time.time() - start
        footer = f"[{stage}] exit={proc.returncode} in {elapsed:.1f}s -> {log_path}"
        print(footer, flush=True)
        log.write(footer + "\n")

    return proc.returncode
