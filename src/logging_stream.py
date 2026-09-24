"""Streamed, teed subprocess runner.

Fixes the long-standing "it pauses, only the .log has the output" problem: a
child Python process block-buffers stdout when it writes to a pipe instead of a
TTY, so nothing surfaces until the buffer flushes (often only at exit). We run
every child with PYTHONUNBUFFERED=1, merge stderr into stdout, and read the
stream line by line, writing each line to BOTH the console (live) and a
per-stage log file.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

# Match optionsconfig's format so the orchestrator's OWN lines parse the same way
# as the export/parse subprocess logs (level token leads each line). File sinks
# strip the color markup automatically.
LOG_FORMAT = (
    "<level>{level}</level> | <cyan>{module}</cyan>:<cyan>{function}</cyan>:"
    "<cyan>{line}</cyan> - <level>{message}</level>"
)


class RunLogger:
    """Owns one run's timestamped log directory and the orchestrator's loguru sinks.

    The orchestrator's OWN output — banners, the preflight gate, the in-process
    RELEASES stage — goes through loguru, so every line carries a level token a
    warning/error counter can match. Two kinds of sink:

    * an aggregate ``run.log`` for the whole run, set up here;
    * a scoped per-step file (:meth:`stage_sink`) for each in-process step, so
      every step still has its own URI-referenceable ``NN-<step>.log``.

    Subprocess stages (export/parse/site) keep :func:`run_streamed`, which tees
    the child's own (already loguru-formatted) output to a per-stage file.
    """

    def __init__(self, log_dir: Path, log_level: str = "DEBUG") -> None:
        self.run_dir = Path(log_dir) / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_log = self.run_dir / "run.log"
        self.log_level = log_level
        self._n = 0
        # (stage, path) for every per-step log handed out, in creation order.
        # The run report scans these to count warnings/errors per step reached.
        self.stage_logs: list[tuple[str, Path]] = []
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Point loguru at this run: console + the aggregate run.log."""
        logger.remove()
        logger.add(sys.stdout, level=self.log_level, format=LOG_FORMAT)
        logger.add(
            str(self.run_log), level=self.log_level, format=LOG_FORMAT,
            enqueue=False, mode="w",
        )

    def _next_path(self, stage: str) -> Path:
        self._n += 1
        path = self.run_dir / f"{self._n:02d}-{stage}.log"
        self.stage_logs.append((stage, path))
        return path

    def banner(self, text: str) -> None:
        logger.info(f"\n{'=' * 70}\n{text}\n{'=' * 70}")

    def stage_log_path(self, stage: str) -> Path:
        """Allocate a per-stage log file for a subprocess stage (run_streamed)."""
        return self._next_path(stage)

    @contextlib.contextmanager
    def stage_sink(self, stage: str):
        """Scoped per-step loguru file sink, for an in-process step.

        Everything the step logs while the context is open lands in both its own
        ``NN-<stage>.log`` and the aggregate run.log; the sink is torn down (and
        flushed — the sink is synchronous) when the step ends. Yields the path.
        """
        path = self._next_path(stage)
        sink_id = logger.add(
            str(path), level=self.log_level, format=LOG_FORMAT,
            enqueue=False, mode="w",
        )
        try:
            yield path
        finally:
            logger.remove(sink_id)


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
