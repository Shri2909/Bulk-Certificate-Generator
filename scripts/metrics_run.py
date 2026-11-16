#!/usr/bin/env python3
"""Measured run of certgen's Prometheus metrics: scraped /metrics must match the database. Stdlib only.

usage: python scripts/metrics_run.py clean M REPS [--jobsize 100] [--threads 2] [--bad-per-job 0]
       [--port 18300]
       python scripts/metrics_run.py lease [--port 18340]
Run with the project's interpreter; the service is started as a subprocess from the repository root
(override with CERTGEN_WT). Run data goes to <tmp>/certgen-metrics-runs (override with CERTGEN_RUNS).
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

WT = Path(os.environ.get("CERTGEN_WT", Path(__file__).resolve().parent.parent))
BASE = Path(os.environ.get("CERTGEN_RUNS", Path(tempfile.gettempdir()) / "certgen-metrics-runs"))
TOKEN = "m3-harness-token-0123456789abcdef"
STATUSES = ("rejected", "pending", "processing", "succeeded", "failed")


def req(url: str, method: str = "GET", body: object = None, auth: bool = True) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if auth:
        r.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def parse(text: str) -> dict[str, float]:
    """Exposition text -> {'name{label="v"}': value}; keys keep the label string verbatim."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.rpartition(" ")
            out[k] = float(v)
    return out


def scrape(base: str, auth: bool = True) -> tuple[dict[str, float], float]:
    t = time.perf_counter()
    status, body = req(base + "/metrics", auth=auth)
    dt = time.perf_counter() - t
    assert status == 200, (status, body[:200])
    return parse(body.decode()), dt


def items_of(s: dict[str, float]) -> dict[str, int]:
    return {st: int(s[f'certgen_items{{status="{st}"}}']) for st in STATUSES}


def db_counts(db: Path) -> dict[str, int]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = dict(c.execute("SELECT status, COUNT(*) FROM items GROUP BY status"))
    finally:
        c.close()
    return {st: int(rows.get(st, 0)) for st in STATUSES}


def env_for(data: Path, port: int, **extra: str) -> dict[str, str]:
    e = {
        **os.environ,
        "PYTHONPATH": str(WT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CERTGEN_DATA_DIR": str(data),
        "CERTGEN_API_TOKENS": TOKEN,
        "CERTGEN_PORT": str(port),
        "CERTGEN_HOST": "127.0.0.1",
        "CERTGEN_LOG_LEVEL": "WARNING",
        "CERTGEN_POLL_INTERVAL_SECONDS": "0.05",
    }
    e.update(extra)
    return e


def wait_health(base: str, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req(base + "/healthz", auth=False)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("service did not come up")


def start_server(data: Path, port: int, **extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "certgen"],
        cwd=WT,
        env=env_for(data, port, **extra),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def payload(job: int, n: int, bad: int) -> dict:
    rec = [{"name": f"Recipient {job}-{i}", "email": f"r{job}-{i}@example.com"} for i in range(n - bad)]
    rec += [{"name": f"Bad {job}-{i}", "email": "not-an-email"} for i in range(bad)]
    return {
        "certificate": {
            "title": "Python for Data Engineering",
            "issuer": "Acme Academy",
            "issued_on": "2026-10-01",
        },
        "recipients": rec,
    }


def clean_rep(M: int, rep: int, a: argparse.Namespace) -> dict:
    data = BASE / f"clean_M{M}_r{rep}"
    subprocess.run(["rm", "-rf", str(data)])
    port = a.port + rep
    base = f"http://127.0.0.1:{port}"
    srv = start_server(data, port, CERTGEN_EMBEDDED_WORKER="1", CERTGEN_WORKER_THREADS=str(a.threads))
    res: dict = {"M": M, "rep": rep, "threads": a.threads}
    try:
        wait_health(base)
        s0, _ = scrape(base)
        assert items_of(s0) == dict.fromkeys(STATUSES, 0) and s0["certgen_queue_depth"] == 0
        # Unauthenticated scrape must be refused.
        assert req(base + "/metrics", auth=False)[0] == 401
        samples: list[tuple[float, dict[str, int], float, float, float]] = []
        stop = threading.Event()
        errors: list[str] = []

        def scraper() -> None:
            while not stop.is_set():
                try:
                    s, _ = scrape(base)
                    samples.append(
                        (
                            time.perf_counter(),
                            items_of(s),
                            s["certgen_queue_depth"],
                            s["certgen_render_seconds_count"],
                            s["certgen_lease_lost_total"],
                        )
                    )
                except Exception as e:
                    errors.append(repr(e))
                time.sleep(0.05)

        th = threading.Thread(target=scraper)
        th.start()
        jobs = 0
        valid = rejected = 0
        t0 = time.perf_counter()
        j = 0
        total_rows = 0
        while valid < M:
            n = min(a.jobsize, (M - valid) + a.bad_per_job)  # rows in this POST
            bad = a.bad_per_job if n > a.bad_per_job else 0
            st, body = req(base + "/v1/certificate-jobs", "POST", payload(j, n, bad))
            assert st == 202, (st, body[:300])
            jobs += 1
            j += 1
            valid += n - bad
            rejected += bad
            total_rows += n
        t_submitted = time.perf_counter() - t0
        # Wait for the queue to drain, judged only by the metrics themselves.
        end = time.time() + 600
        while time.time() < end:
            s, _ = scrape(base)
            it = items_of(s)
            if it["pending"] == 0 and it["processing"] == 0 and it["succeeded"] + it["failed"] == valid:
                break
            time.sleep(0.05)
        t_done = time.perf_counter() - t0
        stop.set()
        th.join()
        # Final scrape vs database vs filesystem vs API.
        sf, _ = scrape(base)
        itf = items_of(sf)
        dbc = db_counts(data / "certgen.sqlite3")
        pdfs = len(list((data / "certificates").rglob("*.pdf")))
        buckets = {k: v for k, v in sf.items() if k.startswith("certgen_render_seconds_bucket")}
        cum = [
            v
            for _, v in sorted(
                buckets.items(),
                key=lambda kv: (
                    float("inf") if "+Inf" in kv[0] else float(kv[0].split('le="')[1].rstrip('"}'))
                ),
            )
        ]
        costs = [scrape(base)[1] for _ in range(20)]
        qd = [s[2] for s in samples]
        checks = {
            "succeeded_gauge_eq_M": itf["succeeded"] == M,
            "succeeded_gauge_eq_db": itf == dbc,
            "rejected_gauge_eq_expected": itf["rejected"] == rejected,
            "failed_zero": itf["failed"] == 0,
            "pending_processing_zero": itf["pending"] == 0 and itf["processing"] == 0,
            "queue_depth_zero_end": sf["certgen_queue_depth"] == 0,
            "queue_depth_gt0_midrun": max(qd) > 0,
            "queue_depth_eq_pending_every_sample": all(s[2] == s[1]["pending"] for s in samples),
            "render_count_eq_M": int(sf["certgen_render_seconds_count"]) == M,
            "render_inf_bucket_eq_count": sf['certgen_render_seconds_bucket{le="+Inf"}']
            == sf["certgen_render_seconds_count"],
            "buckets_cumulative": cum == sorted(cum),
            "render_sum_positive": sf["certgen_render_seconds_sum"] > 0,
            "lease_lost_zero": sf["certgen_lease_lost_total"] == 0,
            "pdf_files_eq_M": pdfs == M,
            "rows_conserved_every_sample": all(sum(s[1].values()) <= total_rows for s in samples),
            "gauges_monotone_succeeded": all(
                a_[1]["succeeded"] <= b_[1]["succeeded"] for a_, b_ in itertools.pairwise(samples)
            ),
            "render_count_monotone": all(a_[3] <= b_[3] for a_, b_ in itertools.pairwise(samples)),
            "scrape_errors_none": not errors,
        }
        res.update(
            jobs_posted=jobs,
            rows_submitted=total_rows,
            valid=valid,
            rejected_expected=rejected,
            final_items=itf,
            db_items=dbc,
            pdfs=pdfs,
            render_count=int(sf["certgen_render_seconds_count"]),
            render_sum_s=round(sf["certgen_render_seconds_sum"], 3),
            render_mean_ms=round(
                1000 * sf["certgen_render_seconds_sum"] / max(1, sf["certgen_render_seconds_count"]), 2
            ),
            buckets={k.split('"')[1]: int(v) for k, v in buckets.items()},
            max_queue_depth_seen=max(qd),
            n_midrun_scrapes=len(samples),
            submit_s=round(t_submitted, 2),
            drain_s=round(t_done, 2),
            throughput_cert_per_s=round(M / t_done, 1),
            scrape_ms_median=round(1000 * statistics.median(costs), 2),
            scrape_ms_max=round(1000 * max(costs), 2),
            checks=checks,
            ok=all(checks.values()),
        )
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(timeout=20)
        except subprocess.TimeoutExpired:
            srv.kill()
    return res


def lease(a: argparse.Namespace) -> dict:
    data = BASE / "lease"
    subprocess.run(["rm", "-rf", str(data)])
    port, pa, pb = a.port, a.port + 1, a.port + 2
    base = f"http://127.0.0.1:{port}"
    srv = start_server(data, port, CERTGEN_EMBEDDED_WORKER="0")
    procs = []
    out: dict = {}
    wenv = dict(CERTGEN_LEASE_SECONDS="1", CERTGEN_MAX_ATTEMPTS="3", CERTGEN_EMBEDDED_WORKER="0")
    db = data / "certgen.sqlite3"
    try:
        wait_health(base)
        st, _ = req(base + "/v1/certificate-jobs", "POST", payload(0, 1, 0))
        assert st == 202
        A = subprocess.Popen(
            [sys.executable, "-m", "certgen.worker"],
            cwd=WT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env_for(
                data, port, CERTGEN_WORKER_METRICS_PORT=str(pa), CERTGEN_TEST_SLOW_RENDER_MS="800", **wenv
            ),
        )
        procs.append(A)
        end = time.time() + 20
        while db_counts(db)["processing"] != 1 and time.time() < end:
            time.sleep(0.005)
        A.send_signal(signal.SIGSTOP)  # frozen mid-render: its lease will expire
        time.sleep(1.4)  # > 1 s lease
        B = subprocess.Popen(
            [sys.executable, "-m", "certgen.worker"],
            cwd=WT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env_for(data, port, CERTGEN_WORKER_METRICS_PORT=str(pb), **wenv),
        )
        procs.append(B)
        end = time.time() + 30
        while db_counts(db)["succeeded"] != 1 and time.time() < end:
            time.sleep(0.05)
        out["db_after_B"] = db_counts(db)
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        out["attempts_after_B"] = c.execute("SELECT attempts FROM items").fetchone()[0]
        c.close()
        pre_b = parse(urllib.request.urlopen(f"http://127.0.0.1:{pb}/metrics", timeout=5).read().decode())
        out["B_lease_lost_before_resume"] = pre_b["certgen_lease_lost_total"]
        A.send_signal(signal.SIGCONT)  # A finishes its render and tries to write: fenced out
        end = time.time() + 20
        a_lost = 0.0
        a_s = {}
        while time.time() < end:
            try:
                a_s = parse(
                    urllib.request.urlopen(f"http://127.0.0.1:{pa}/metrics", timeout=2).read().decode()
                )
                a_lost = a_s["certgen_lease_lost_total"]
                if a_lost >= 1:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        out["A_lease_lost_total"] = a_lost
        out["A_render_count"] = a_s.get("certgen_render_seconds_count")
        out["A_render_sum_s"] = round(a_s.get("certgen_render_seconds_sum", 0), 3)
        out["A_buckets"] = {
            k.split('"')[1]: int(v) for k, v in a_s.items() if k.startswith("certgen_render_seconds_bucket")
        }
        b_s = parse(urllib.request.urlopen(f"http://127.0.0.1:{pb}/metrics", timeout=5).read().decode())
        out["B_lease_lost_total"] = b_s["certgen_lease_lost_total"]
        out["B_render_count"] = b_s["certgen_render_seconds_count"]
        api, _ = scrape(base)
        out["API_lease_lost_total"] = api["certgen_lease_lost_total"]
        out["API_render_count"] = api["certgen_render_seconds_count"]
        out["API_items"] = items_of(api)
        out["db_final"] = db_counts(db)
        out["pdfs"] = len(list((data / "certificates").rglob("*.pdf")))
        out["ok"] = (
            a_lost == 1
            and out["attempts_after_B"] == 2
            and out["B_lease_lost_before_resume"] == 0
            and out["db_final"]["succeeded"] == 1
            and out["pdfs"] == 1
        )
    finally:
        for p in procs:
            with contextlib.suppress(ProcessLookupError):
                p.send_signal(signal.SIGCONT)
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(timeout=20)
        except subprocess.TimeoutExpired:
            srv.kill()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["clean", "lease"])
    ap.add_argument("M", type=int, nargs="?", default=200)
    ap.add_argument("reps", type=int, nargs="?", default=3)
    ap.add_argument("--jobsize", type=int, default=100)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--bad-per-job", type=int, default=0)
    ap.add_argument("--port", type=int, default=18300)
    a = ap.parse_args()
    BASE.mkdir(exist_ok=True)
    if a.mode == "lease":
        r = lease(a)
        print(json.dumps(r, indent=1))
        return 0 if r["ok"] else 1
    bad = 0
    for rep in range(a.reps):
        r = clean_rep(a.M, rep, a)
        print(json.dumps(r))
        bad += not r["ok"]
    print(f"SUMMARY M={a.M} reps={a.reps} failed_reps={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
