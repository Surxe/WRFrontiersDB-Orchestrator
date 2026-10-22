#!/usr/bin/env python3
"""Run a command in its own memory cgroup and record what it really uses.

The command runs in a transient systemd user slice. Once a second the sampler reads
the slice's cgroup files (memory.current, memory.stat, memory.swap.current,
memory.events, memory.pressure) plus the host side (/proc/meminfo and, with --vm, a
Proxmox VM's RSS and swap), and writes one CSV row per sample. At the end it reads
memory.peak, memory.swap.peak and memory.events and writes a summary (Markdown and
JSON) with one line per stage.

Why a slice and a PATH shim: the Exporter starts its export with its own
`systemd-run --user --scope`, and a new scope lands under app.slice, outside any
cgroup this wrapper made. The shim (on PATH for the command only) adds
`--slice=<this slice>` to such calls, so nested scopes stay inside the measured
cgroup and inside its limits, as they would inside one container.

memory.current counts file cache, which the kernel can reclaim; `anon` (plus
`shmem`, which is not reclaimable without swap) is the part a limit really has to
hold. Both are recorded.

Stages: with --stages each sample is stamped with the orchestrator stage running.
The command's output passes through this wrapper (to its stdout, and to --log if
given); the last banner (EXPORT, PARSE, ...) names the stage, and markers in the
export and parse output (STAGE_MARKERS) name the sub-stage. The console stream is
used, not the per-stage log files, because those are block-buffered and lag by
minutes during quiet steps such as the mapper. Without --stages the stage column is
empty and the output is not touched.

Examples:
    bin/measure-memory --out /tmp/m -- sleep 5
    bin/measure-memory --out runs/uncapped --vm 100 --stages \\
        -- bin/run-pipeline --game-version 2026-10-20 ...
    bin/measure-memory -p MemoryHigh=2.5G -p MemoryMax=2.75G --out runs/capped -- ...

Exit code: the command's.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
STAT_KEYS = ("anon", "file", "shmem", "kernel", "file_dirty", "file_writeback",
             "file_mapped", "slab_reclaimable")
EVENT_KEYS = ("high", "max", "oom", "oom_kill")
MEMINFO_KEYS = ("MemAvailable", "MemFree", "SwapFree", "SwapTotal")

COLUMNS = (
    ["t", "elapsed_s", "stage", "current"]
    + list(STAT_KEYS)
    + ["swap", "anon_plus_file", "procs"]
    + [f"ev_{k}" for k in EVENT_KEYS]
    + ["psi_some_avg10", "psi_full_avg10"]
    + [f"host_{k}" for k in MEMINFO_KEYS]
    + ["vm_rss", "vm_swap"]
)

# Sub-stage markers: (stage log name, regex, label). The last marker matched in a
# stage's log names the sub-stage; a group in the regex is appended to the label.
STAGE_MARKERS = (
    ("export", re.compile(r"STEP \d+: DEPENDENCY MANAGER"), "deps"),
    ("export", re.compile(r"STEP \d+: STEAM DOWNLOAD"), "steam"),
    ("export", re.compile(r"STEP \d+: DLL INJECTION FOR MAPPER"), "mapper"),
    ("export", re.compile(r"STEP \d+: EXPORT"), "cue4parse"),
    ("export", re.compile(r"Phase (\w+): \d+ (?:file|released)"), "cue4parse-{}"),
    ("parse", re.compile(r"WRFrontiersDB-Parser@run\.py started"), "objects"),
    ("parse", re.compile(r"map:parse_maps:\d+ - Maps: [A-Z]"), "maps"),
    ("parse", re.compile(r"Starting enrichment phase"), "enrich"),
    ("parse", re.compile(r"Starting to process parsed images"), "images"),
)
BANNER_RULE = re.compile(r"^={10,}\s*$")
# Banner texts that are not stages.
BANNER_STAGES = {"Pipeline complete": "finish"}


# ---------------------------------------------------------------- cgroup reads

def read_kv(path: Path) -> dict[str, int]:
    """A `key value` file (memory.stat, memory.events) as {key: int}."""
    out: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, value = line.partition(" ")
            if value.strip().lstrip("-").isdigit():
                out[key] = int(value)
    except OSError:
        pass
    return out


def read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def read_psi(path: Path) -> tuple[float | None, float | None]:
    """(some avg10, full avg10) from a cgroup's memory.pressure."""
    some = full = None
    try:
        for line in path.read_text().splitlines():
            kind, *fields = line.split()
            avg10 = dict(f.split("=") for f in fields).get("avg10")
            if kind == "some":
                some = float(avg10)
            elif kind == "full":
                full = float(avg10)
    except (OSError, ValueError):
        pass
    return some, full


def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    """/proc/meminfo in bytes."""
    out: dict[str, int] = {}
    for line in path.read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * (1024 if parts[1:] == ["kB"] else 1)
    return out


def find_vm_pid(vmid: str) -> int | None:
    """The QEMU process of Proxmox VM `vmid` (its cmdline carries `-id <vmid>`)."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if args and args[0].endswith(b"kvm") and b"-id" in args:
            i = args.index(b"-id")
            if i + 1 < len(args) and args[i + 1].decode() == vmid:
                return int(entry.name)
    return None


def read_vm(pid: int | None) -> tuple[int | None, int | None]:
    """(VmRSS, VmSwap) in bytes of a process."""
    if pid is None:
        return None, None
    status = read_meminfo(Path(f"/proc/{pid}/status")) if Path(f"/proc/{pid}").exists() else {}
    return status.get("VmRSS"), status.get("VmSwap")


def count_procs(cg: Path) -> int:
    """Processes in the cgroup and every cgroup below it."""
    total = 0
    for dirpath, _dirs, _files in os.walk(cg):
        try:
            total += len(Path(dirpath, "cgroup.procs").read_text().split())
        except OSError:
            pass
    return total


def sample(cg: Path, vm_pid: int | None) -> dict:
    stat = read_kv(cg / "memory.stat")
    events = read_kv(cg / "memory.events")
    some, full = read_psi(cg / "memory.pressure")
    host = read_meminfo()
    vm_rss, vm_swap = read_vm(vm_pid)
    row = {"current": read_int(cg / "memory.current")}
    row.update({k: stat.get(k) for k in STAT_KEYS})
    row["swap"] = read_int(cg / "memory.swap.current")
    row["anon_plus_file"] = (stat.get("anon", 0) + stat.get("file", 0)) if stat else None
    row["procs"] = count_procs(cg)
    row.update({f"ev_{k}": events.get(k) for k in EVENT_KEYS})
    row["psi_some_avg10"], row["psi_full_avg10"] = some, full
    row.update({f"host_{k}": host.get(k) for k in MEMINFO_KEYS})
    row["vm_rss"], row["vm_swap"] = vm_rss, vm_swap
    return row


# ---------------------------------------------------------------- stage tracking

@dataclass
class StageTracker:
    """Names the running stage from the orchestrator's output, fed line by line.

    A banner is a line between two rules of `=`; its text names the stage
    (lowercased). Before the first stage banner the stage is `preflight`. Within a
    stage, the last STAGE_MARKERS match names the sub-stage.
    """

    current: str = "preflight"
    _stage: str = "preflight"
    _prev_rule: bool = False

    def feed(self, line: str) -> None:
        line = line.rstrip("\n")
        if BANNER_RULE.match(line):
            self._prev_rule = True
            return
        if self._prev_rule and line.strip():
            text = line.strip()
            if text in BANNER_STAGES:
                self._set(BANNER_STAGES[text])
            elif re.fullmatch(r"[A-Z][A-Z-]*", text):
                self._set(text.lower())
        self._prev_rule = False
        for name, rx, label in STAGE_MARKERS:
            if name == self._stage:
                m = rx.search(line)
                if m:
                    self.current = f"{self._stage}:{label.format(*m.groups()).lower()}"

    def _set(self, stage: str) -> None:
        self._stage = stage
        self.current = stage


def pump(stream, tracker: StageTracker, log_fh) -> None:
    """Copy the command's output to our stdout (and log), feeding the tracker."""
    for raw in iter(stream.readline, b""):
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        if log_fh:
            log_fh.write(raw)
            log_fh.flush()
        tracker.feed(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------- summary

def summarize(rows: list[dict], interval: float = 1.0) -> list[dict]:
    """One entry per stage (in order of first appearance): duration and peaks."""
    order: list[str] = []
    by_stage: dict[str, list[dict]] = {}
    for row in rows:
        stage = row["stage"] or "-"
        if stage not in by_stage:
            order.append(stage)
            by_stage[stage] = []
        by_stage[stage].append(row)

    def peak(rs: list[dict], key: str) -> int | None:
        values = [r[key] for r in rs if r.get(key) is not None]
        return max(values) if values else None

    def low(rs: list[dict], key: str) -> int | None:
        values = [r[key] for r in rs if r.get(key) is not None]
        return min(values) if values else None

    out = []
    for stage in order:
        rs = by_stage[stage]
        out.append({
            "stage": stage,
            "samples": len(rs),
            "start_s": rs[0]["elapsed_s"],
            "duration_s": round(rs[-1]["elapsed_s"] - rs[0]["elapsed_s"] + interval, 1),
            "peak_current": peak(rs, "current"),
            "peak_anon": peak(rs, "anon"),
            "peak_anon_plus_shmem": max(((r["anon"] or 0) + (r["shmem"] or 0) for r in rs), default=None),
            "peak_anon_plus_file": peak(rs, "anon_plus_file"),
            "peak_kernel": peak(rs, "kernel"),
            "peak_swap": peak(rs, "swap"),
            "peak_psi_some_avg10": peak(rs, "psi_some_avg10"),
            "min_host_MemAvailable": low(rs, "host_MemAvailable"),
            "min_host_SwapFree": low(rs, "host_SwapFree"),
            "peak_vm_rss": peak(rs, "vm_rss"),
            "peak_vm_swap": peak(rs, "vm_swap"),
        })
    return out


def mib(value) -> str:
    return "-" if value is None else f"{value / 2**20:,.0f}"


def write_summary(out: Path, meta: dict, stages: list[dict], final: dict) -> None:
    (out / "summary.json").write_text(json.dumps(
        {"meta": meta, "final": final, "stages": stages}, indent=2) + "\n")
    lines = [
        f"# Memory: {meta['name']}",
        "",
        f"- Command: `{' '.join(meta['command'])}`",
        f"- Started {meta['started']}, ran {meta['wall_s']:.0f} s, exit {meta['exit_code']}",
        f"- Slice properties: {', '.join(meta['properties']) or 'none (uncapped)'}",
        f"- memory.peak {mib(final.get('memory.peak'))} MiB, "
        f"memory.swap.peak {mib(final.get('memory.swap.peak'))} MiB",
        f"- memory.events: {final.get('memory.events')}",
        "",
        "All figures MiB. anon+file is the peak of their sum at one sample.",
        "",
        "| Stage | Duration s | Peak current | Peak anon | Peak anon+shmem | Peak anon+file "
        "| Peak swap | Min host MemAvailable | Peak VM RSS |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in stages:
        lines.append(
            f"| {s['stage']} | {s['duration_s']:.0f} | {mib(s['peak_current'])} | {mib(s['peak_anon'])} "
            f"| {mib(s['peak_anon_plus_shmem'])} | {mib(s['peak_anon_plus_file'])} | {mib(s['peak_swap'])} "
            f"| {mib(s['min_host_MemAvailable'])} | {mib(s['peak_vm_rss'])} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- running

SHIM = """#!/bin/sh
# measure-memory shim: keep nested `systemd-run --user --scope` inside the measured slice.
user=0 scope=0 slice=0
for arg in "$@"; do
  case "$arg" in
    --) break ;;
    --user) user=1 ;;
    --scope) scope=1 ;;
    --slice|--slice=*) slice=1 ;;
  esac
done
if [ "$user$scope$slice" = 110 ]; then
  exec "$MEASURE_REAL_SYSTEMD_RUN" --slice="$MEASURE_SLICE" "$@"
fi
exec "$MEASURE_REAL_SYSTEMD_RUN" "$@"
"""


def systemctl(*args: str, check: bool = True) -> str:
    return subprocess.run(["systemctl", "--user", *args], check=check,
                          capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--out", type=Path, required=True,
                    help="Output dir for samples.csv, summary.md and summary.json.")
    ap.add_argument("--name", help="Run name (default: the --out dir's name).")
    ap.add_argument("--interval", type=float, default=1.0, help="Seconds between samples.")
    ap.add_argument("-p", "--property", action="append", default=[], metavar="KEY=VALUE",
                    help="systemd resource property for the slice, e.g. MemoryHigh=2.5G. Repeatable.")
    ap.add_argument("--no-nest-scopes", action="store_true",
                    help="Don't shim systemd-run; nested user scopes then escape the slice.")
    ap.add_argument("--stages", action="store_true",
                    help="Stamp each sample with the orchestrator stage, read from the command's output.")
    ap.add_argument("--log", type=Path, help="With --stages, also copy the command's output here.")
    ap.add_argument("--vm", metavar="VMID", help="Also sample this Proxmox VM's RSS and swap.")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no command given (put it after --)")

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = args.name or out.name
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    tag = re.sub(r"[^A-Za-z0-9_]", "_", f"{name}_{stamp}")
    slice_unit = f"measure-{tag}.slice"

    if args.property:
        systemctl("set-property", "--runtime", slice_unit, *args.property)

    env = dict(os.environ)
    shim_dir = None
    if not args.no_nest_scopes:
        real = shutil.which("systemd-run")
        if real is None:
            ap.error("systemd-run not found")
        shim_dir = Path(tempfile.mkdtemp(prefix="measure-memory-"))
        (shim_dir / "systemd-run").write_text(SHIM)
        (shim_dir / "systemd-run").chmod(0o755)
        env.update(MEASURE_REAL_SYSTEMD_RUN=real, MEASURE_SLICE=slice_unit,
                   PATH=f"{shim_dir}{os.pathsep}{env.get('PATH', '')}")

    started = time.time()
    meta = {"name": name, "command": command, "properties": args.property,
            "slice": slice_unit, "started": datetime.now().isoformat(timespec="seconds"),
            "interval_s": args.interval, "nest_scopes": not args.no_nest_scopes}
    proc = subprocess.Popen(
        [shutil.which("systemd-run") or "systemd-run", "--user", "--scope", "--quiet",
         f"--slice={slice_unit}", f"--unit=measure-{tag}", "--", *command], env=env,
        **({"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT} if args.stages else {}))
    tracker = StageTracker() if args.stages else None
    log_fh = args.log.open("wb") if args.stages and args.log else None
    reader = None
    if tracker:
        reader = threading.Thread(target=pump, args=(proc.stdout, tracker, log_fh), daemon=True)
        reader.start()

    cg = None
    for _ in range(50):
        path = systemctl("show", "-p", "ControlGroup", "--value", slice_unit, check=False)
        if path and (CGROUP_ROOT / path.lstrip("/")).is_dir():
            cg = CGROUP_ROOT / path.lstrip("/")
            break
        time.sleep(0.1)
    if cg is None:
        print(f"measure-memory: no cgroup for {slice_unit}", file=sys.stderr)
        proc.wait()
        return proc.returncode
    meta["cgroup"] = str(cg)
    print(f"measure-memory: sampling {cg} every {args.interval}s -> {out}", file=sys.stderr)

    vm_pid = find_vm_pid(args.vm) if args.vm else None
    rows: list[dict] = []

    # Let Ctrl-C reach the command (same process group) while we keep sampling.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with (out / "samples.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        next_t = time.monotonic()
        while True:
            done = proc.poll() is not None
            if done and reader:
                reader.join(timeout=5)
            row = {"t": datetime.now().isoformat(timespec="seconds"),
                   "elapsed_s": round(time.time() - started, 1),
                   "stage": tracker.current if tracker else ""}
            row.update(sample(cg, vm_pid))
            rows.append(row)
            writer.writerow(row)
            fh.flush()
            if done:
                break
            next_t += args.interval
            time.sleep(max(0.0, next_t - time.monotonic()))

    final = {
        "memory.peak": read_int(cg / "memory.peak"),
        "memory.swap.peak": read_int(cg / "memory.swap.peak"),
        "memory.events": read_kv(cg / "memory.events"),
        "memory.swap.events": read_kv(cg / "memory.swap.events"),
    }
    meta["exit_code"] = proc.returncode
    meta["wall_s"] = time.time() - started
    write_summary(out, meta, summarize(rows, args.interval), final)

    systemctl("stop", slice_unit, check=False)
    if args.property:
        systemctl("revert", slice_unit, check=False)
    if log_fh:
        log_fh.close()
    if shim_dir:
        shutil.rmtree(shim_dir, ignore_errors=True)
    print(f"measure-memory: exit {proc.returncode}; summary in {out / 'summary.md'}", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
