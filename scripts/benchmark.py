"""Benchmark certificate generation and write the evidence JSON the README figures come from.

Phase A times each item through one in-process worker; phase B drains the same corpus with N worker
processes. Exits non-zero if any acceptance check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # run from a checkout without installing the package

from fastapi.testclient import TestClient  # noqa: E402

from certgen.clock import system_clock  # noqa: E402
from certgen.config import Settings  # noqa: E402
from certgen.main import create_app  # noqa: E402
from certgen.rendering import CertificateData, render_certificate  # noqa: E402
from certgen.storage import Storage  # noqa: E402
from certgen.worker import MAX_PDF_BYTES, Worker  # noqa: E402

NAMES = ("Zoë Martin", "Νίκος Παπαδόπουλος", "Екатерина Иванова")
CERTIFICATE = {"title": "Python for Data Engineering", "issuer": "Acme Academy", "issued_on": "2026-10-01"}
WARMUP_RENDERS = 10
PACKAGES = ("fastapi", "starlette", "uvicorn", "pydantic", "email-validator", "sqlalchemy", "reportlab")


def corpus(n: int) -> dict[str, Any]:
    return {
        "certificate": CERTIFICATE,
        "recipients": [{"name": NAMES[i % 3], "email": f"recipient{i:04d}@example.org"} for i in range(n)],
    }


def stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"p50": round(statistics.median(ordered), 3), "p95": round(p95, 3), "max": round(ordered[-1], 3)}


def submit(data_dir: Path, recipients: int) -> float:
    """POST the corpus once and return the request latency in ms."""
    token = secrets.token_urlsafe(32)
    settings = Settings(
        data_dir=data_dir, embedded_worker=False, max_recipients=max(recipients, 1), api_tokens=(token,)
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(settings), base_url="http://localhost", headers=headers) as client:
        started = time.perf_counter()
        resp = client.post("/v1/certificate-jobs", json=corpus(recipients))
        latency = (time.perf_counter() - started) * 1000
    if resp.status_code != 202:
        raise SystemExit(f"submission failed: {resp.status_code} {resp.text[:300]}")
    return latency


class TimedStorage(Storage):
    def __init__(self, root: Path, sink: list[float]) -> None:
        super().__init__(root)
        self.sink = sink

    def save_atomic(self, job_id: str, certificate_id: str, data: bytes) -> None:
        started = time.perf_counter()
        super().save_atomic(job_id, certificate_id, data)
        self.sink.append((time.perf_counter() - started) * 1000)


def phase_a(work: Path, recipients: int) -> dict[str, dict[str, float]]:
    data_dir = work / "phase-a"
    submit(data_dir, recipients)
    settings = Settings(data_dir=data_dir, embedded_worker=False)
    render_ms: list[float] = []
    storage_ms: list[float] = []
    item_ms: list[float] = []

    def timed_render(data: CertificateData) -> bytes:
        started = time.perf_counter()
        pdf = render_certificate(data)
        render_ms.append((time.perf_counter() - started) * 1000)
        return pdf

    with TestClient(create_app(settings)) as client:
        worker = Worker(
            client.app.state.engines,  # type: ignore[attr-defined]
            TimedStorage(settings.certificates_dir, storage_ms),
            settings,
            system_clock,
            timed_render,
        )
        while True:
            started = time.perf_counter()
            if worker.run_once() is None:
                break
            item_ms.append((time.perf_counter() - started) * 1000)
    return {"per_item_ms": stats(item_ms), "render_ms": stats(render_ms), "storage_ms": stats(storage_ms)}


def query(db: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def phase_b(work: Path, recipients: int, processes: int) -> dict[str, Any]:
    data_dir = work / "phase-b"
    post_latency_ms = submit(data_dir, recipients)
    db = data_dir / "certgen.sqlite3"
    env = {
        **os.environ,
        "CERTGEN_DATA_DIR": str(data_dir),
        "CERTGEN_POLL_INTERVAL_SECONDS": "0.05",
        "PYTHONPATH": str(ROOT),
    }
    started = time.perf_counter()
    workers = [
        subprocess.Popen(
            [sys.executable, "-m", "certgen.worker"], cwd=ROOT, env=env, stderr=subprocess.DEVNULL
        )
        for _ in range(processes)
    ]
    try:
        while query(db, "SELECT COUNT(*) AS n FROM items WHERE status IN ('pending', 'processing')")[0]["n"]:
            if any(w.poll() is not None for w in workers):
                raise SystemExit("a worker process exited early")
            time.sleep(0.05)
        wall_ms = (time.perf_counter() - started) * 1000
    finally:
        for w in workers:
            w.terminate()
        for w in workers:
            w.wait(timeout=30)
    rows = query(db, "SELECT status, attempts FROM items")
    sizes = [p.stat().st_size for p in (data_dir / "certificates").rglob("*.pdf")]
    return {
        "post_latency_ms": round(post_latency_ms, 3),
        "wall_ms": round(wall_ms, 3),
        "throughput_per_s": round(recipients / (wall_ms / 1000), 3),
        "attempts_histogram": {str(k): v for k, v in sorted(Counter(r["attempts"] for r in rows).items())},
        "counts": dict(sorted(Counter(r["status"] for r in rows).items())),
        "pdf_bytes": {"max": max(sizes), "count": len(sizes)},
    }


def peak_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:  # Windows
        return None
    # ru_maxrss: KiB on Linux, bytes on macOS.
    scale = 1 if sys.platform == "darwin" else 1024
    usage = max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )
    return int(usage * scale)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--recipients", type=int, default=1000)
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    # Default under ./data (gitignored) so fsync hits the real disk; /tmp is often tmpfs.
    parser.add_argument("--work-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    sample = CertificateData("0" * 32, "1" * 32, NAMES[0], "warmup@example.org", **CERTIFICATE)
    for _ in range(WARMUP_RENDERS):  # import, font and code-path warm-up, excluded from timings
        render_certificate(sample)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="certgen-bench-", dir=args.work_dir) as tmp:
        work = Path(tmp)
        per_item = phase_a(work, args.recipients)
        throughput = phase_b(work, args.recipients, args.processes)

    lease_ms = Settings().lease_ms
    acceptance = {
        "all_succeeded": throughput["counts"] == {"succeeded": args.recipients},
        "max_pdf_within_limit": throughput["pdf_bytes"]["max"] <= MAX_PDF_BYTES,
        "max_item_under_10pct_of_lease": per_item["per_item_ms"]["max"] < 0.1 * lease_ms,
    }
    result = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip(),
        # True if certgen/ or scripts/ differ from git_commit.
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", "certgen", "scripts"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        ),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "versions": {**{p: metadata.version(p) for p in PACKAGES}, "sqlite": sqlite3.sqlite_version},
        "processes": args.processes,
        "recipients": args.recipients,
        **throughput,
        **per_item,
        # Estimate (median x count) of a synchronous POST, not a measurement.
        "serial_estimate_ms": round(args.recipients * per_item["per_item_ms"]["p50"], 1),
        "peak_rss_bytes": peak_rss_bytes(),
        "lease_ms": lease_ms,
        "work_dir": str(args.work_dir.resolve().relative_to(ROOT))
        if args.work_dir.resolve().is_relative_to(ROOT)
        else "outside repository",
        "acceptance": acceptance,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stdout.write(
        json.dumps({k: result[k] for k in ("processes", "wall_ms", "throughput_per_s", "acceptance")}) + "\n"
    )
    return 0 if all(acceptance.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
