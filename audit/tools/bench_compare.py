"""Baseline vs remediated benchmarks, run alternately in one session (Phase 7 re-validation).

    python audit/tools/bench_compare.py <baseline-tree> <baseline-python> <scratch-dir> [recipients=1000]

Runs `scripts/benchmark.py --processes N` for N in 1, 2, 4, baseline then remediated (the current
directory, with this interpreter) for each N, each in a fresh work dir under <scratch-dir> (deleted
afterwards), and prints one row per run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HEADER = (
    f"{'variant':<11} {'proc':>4} {'wall s':>8} {'certs/s':>8} {'POST ms':>8} {'render p50':>10}"
    f" {'item p50':>9} {'item max':>9} {'RSS MB':>7}  accept"
)


def run(variant: str, tree: Path, python: str, processes: int, recipients: int, scratch: Path) -> str:
    work = Path(tempfile.mkdtemp(dir=scratch))
    out = work / "result.json"
    try:
        proc = subprocess.run(
            [
                python,
                "scripts/benchmark.py",
                "--recipients",
                str(recipients),
                "--processes",
                str(processes),
                "--output",
                str(out),
                "--work-dir",
                str(work / "data"),
            ],
            cwd=tree,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            raise SystemExit(f"{variant} {processes}p failed (exit {proc.returncode}):\n{proc.stderr}")
        d = json.loads(out.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return (
        f"{variant:<11} {processes:>4} {d['wall_ms'] / 1000:>8.2f} {d['throughput_per_s']:>8.1f}"
        f" {d['post_latency_ms']:>8.0f} {d['render_ms']['p50']:>10.2f} {d['per_item_ms']['p50']:>9.2f}"
        f" {d['per_item_ms']['max']:>9.1f} {d['peak_rss_bytes'] / 2**20:>7.0f}"
        f"  {all(d['acceptance'].values())}"
    )


def main() -> None:
    baseline, baseline_python, scratch = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    recipients = int(sys.argv[4]) if len(sys.argv) > 4 else 1000
    scratch.mkdir(parents=True, exist_ok=True)
    print(HEADER, flush=True)
    for processes in (1, 2, 4):
        print(run("baseline", baseline, baseline_python, processes, recipients, scratch), flush=True)
        print(run("remediated", Path.cwd(), sys.executable, processes, recipients, scratch), flush=True)


if __name__ == "__main__":
    main()
