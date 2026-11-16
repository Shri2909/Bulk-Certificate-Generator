#!/usr/bin/env python3
"""Fault-injection harness: SIGKILL real `python -m certgen.worker` processes mid-render until a job finishes.

Usage (project venv; --root defaults to the repository root):
    python scripts/chaos_kill.py --seed 1 --recipients 300 --port 18201 --workdir <dir> [--policy safe|any]

What is counted as a kill ("confirmed"): the harness observed an item in status 'processing' with a
fresh lease (claimed by a live worker process), sent SIGKILL to that worker's PID, reaped it, and a
read of the DB afterwards still shows the same item in 'processing' with the same lease token, i.e.
the worker died holding an unfinished claim. A kill that lands after the item's SUCCEED commit is
reported separately and NOT counted.

Policy 'safe'  : never kill a claim whose attempt number == max_attempts (the item would end WORKER_LOST).
Policy 'any'   : kill any in-flight claim; WORKER_LOST failures are expected and reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TOKEN = "chaos-harness-token-0123456789"


def http(port: int, path: str, body: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def q(db: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def all_items(port: int, job_id: str) -> list[dict]:
    items, after = [], -1
    while True:
        _, raw = http(port, f"/v1/certificate-jobs/{job_id}/items?limit=1000&after={after}")
        page = json.loads(raw)
        items += page["items"]
        if page["next_after"] is None or not page["items"]:
            return items
        after = page["next_after"]


class Fleet:
    def __init__(self, data_dir: Path, env: dict[str, str], logdir: Path, root: Path) -> None:
        self.data_dir, self.env, self.logdir, self.root = data_dir, env, logdir, root
        self.procs: list[subprocess.Popen] = []
        self.spawned = 0

    def spawn(self) -> subprocess.Popen:
        self.spawned += 1
        log = open(self.logdir / f"worker-{self.spawned}.log", "wb")  # noqa: SIM115 - must outlive the child
        p = subprocess.Popen(
            [sys.executable, "-m", "certgen.worker"],
            cwd=self.root,
            env=self.env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
        self.procs.append(p)
        return p

    def owner_of(self, p: subprocess.Popen) -> str | None:
        """worker_id (== items.lease_owner) of a process, from its own log line."""
        path = self.logdir / f"worker-{self.procs.index(p) + 1}.log"
        m = re.search(r"worker_started worker_id=([0-9a-f]{32})", path.read_text(errors="replace"))
        return m.group(1) if m else None

    def live(self) -> list[subprocess.Popen]:
        return [p for p in self.procs if p.poll() is None]


def run(args: argparse.Namespace) -> dict:
    root = Path(args.root).resolve()
    work = Path(args.workdir).resolve()
    data = work / "data"
    logdir = work / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    db = data / "certgen.sqlite3"
    rng = random.Random(args.seed)
    base_env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CERTGEN_DATA_DIR": str(data),
        "CERTGEN_API_TOKENS": TOKEN,
        "CERTGEN_LEASE_SECONDS": str(args.lease),
        "CERTGEN_MAX_ATTEMPTS": str(args.max_attempts),
        "CERTGEN_POLL_INTERVAL_SECONDS": "0.05",
        "CERTGEN_TEST_SLOW_RENDER_MS": str(args.render_ms),
        "CERTGEN_EMBEDDED_WORKER": "0",
        "CERTGEN_PORT": str(args.port),
        "CERTGEN_LOG_LEVEL": "INFO",
    }
    api_log = open(logdir / "api.log", "wb")  # noqa: SIM115 - must outlive the child
    api = subprocess.Popen(
        [sys.executable, "-m", "certgen"],
        cwd=root,
        env=base_env,
        stdout=api_log,
        stderr=api_log,
        stdin=subprocess.DEVNULL,
    )
    fleet = Fleet(data, base_env, logdir, root)
    result: dict = {
        "seed": args.seed,
        "policy": args.policy,
        "recipients": args.recipients,
        "workers": args.workers,
        "lease_s": args.lease,
        "render_ms": args.render_ms,
        "max_attempts": args.max_attempts,
    }
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                http(args.port, "/healthz")
                break
            except Exception:
                if api.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit(f"API failed to start; see {logdir / 'api.log'}") from None
                time.sleep(0.1)

        body = {
            "certificate": {"title": "Chaos Course", "issuer": "Chaos Academy", "issued_on": "2026-10-01"},
            "recipients": [
                {"name": f"Recipient {i}", "email": f"r{i}@example.com"} for i in range(args.recipients)
            ],
        }
        status, raw = http(args.port, "/v1/certificate-jobs", body)
        assert status == 202, (status, raw)
        job_id = json.loads(raw)["id"]
        ids_at_submit = {i["row_index"]: i["certificate_id"] for i in all_items(args.port, job_id)}
        assert len(ids_at_submit) == args.recipients

        for _ in range(args.workers):
            fleet.spawn()

        kills: list[dict] = []  # confirmed kills (item still 'processing' after reaping)
        late_kills = 0  # SIGKILL delivered but item had already succeeded/failed
        skipped_last_attempt = 0
        killed_items: dict[int, int] = {}
        next_kill_after = rng.randint(1, args.every)  # distinct fresh claims to let pass before killing
        seen_claims: set[str] = set()
        t0 = time.monotonic()
        hard_deadline = t0 + args.timeout
        render_s = args.render_ms / 1000

        def finished() -> bool:
            r = q(db, "SELECT COUNT(*) n FROM items WHERE status IN ('pending','processing')")
            return r[0]["n"] == 0

        while time.monotonic() < hard_deadline:
            # Keep the fleet at full strength (restart killed workers after a random short delay).
            while len(fleet.live()) < args.workers:
                time.sleep(rng.uniform(0, 0.3))
                fleet.spawn()
            if finished():
                break
            now_ms = int(time.time() * 1000)
            rows = q(
                db,
                "SELECT id, attempts, lease_owner, lease_token, lease_expires_at FROM items "
                "WHERE status = 'processing'",
            )
            fresh = [
                r
                for r in rows
                if r["lease_token"] not in seen_claims
                and r["lease_expires_at"] - now_ms > args.lease * 1000 - 0.8 * args.render_ms
            ]
            for r in fresh:
                seen_claims.add(r["lease_token"])
                if next_kill_after > 0:
                    next_kill_after -= 1
                    continue
                if args.policy == "safe" and r["attempts"] >= args.max_attempts:
                    skipped_last_attempt += 1
                    continue
                victim = next((p for p in fleet.live() if fleet.owner_of(p) == r["lease_owner"]), None)
                if victim is None:
                    continue
                # Random point inside the render window (a little past it: render done, commit pending).
                time.sleep(rng.uniform(0, render_s * 0.9))
                victim.send_signal(signal.SIGKILL)
                victim.wait(timeout=10)
                after = q(db, "SELECT status, attempts, lease_token FROM items WHERE id = ?", (r["id"],))[0]
                if after["status"] == "processing" and after["lease_token"] == r["lease_token"]:
                    kills.append({"item": r["id"], "attempt": r["attempts"], "rc": victim.returncode})
                    killed_items[r["id"]] = killed_items.get(r["id"], 0) + 1
                else:
                    late_kills += 1
                next_kill_after = rng.randint(0, args.every)
                break
            time.sleep(0.004)
        else:
            result["timeout"] = True

        elapsed = time.monotonic() - t0
        for p in fleet.live():
            p.send_signal(signal.SIGTERM)
        for p in fleet.live():
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()

        # ------------------------------ verification ------------------------------
        sys.path.insert(0, str(root))
        from certgen.rendering import CertificateData, render_certificate

        _status, raw = http(args.port, f"/v1/certificate-jobs/{job_id}")
        job = json.loads(raw)
        items = all_items(args.port, job_id)
        by_status: dict[str, int] = {}
        for it in items:
            by_status[it["status"]] = by_status.get(it["status"], 0) + 1
        dbrows = q(
            db,
            "SELECT id, row_index, status, attempts, certificate_id, errors, lease_token, "
            "lease_owner FROM items ORDER BY id",
        )
        db_status: dict[str, int] = {}
        for r in dbrows:
            db_status[r["status"]] = db_status.get(r["status"], 0) + 1
        problems: list[str] = []
        if db_status != by_status:
            problems.append(f"API vs DB status mismatch {by_status} vs {db_status}")
        stuck = [r["id"] for r in dbrows if r["status"] in ("pending", "processing")]
        lost = [r["id"] for r in dbrows if r["errors"] and "WORKER_LOST" in r["errors"]]
        multi = [r["id"] for r in dbrows if r["attempts"] > 1]
        if stuck:
            problems.append(f"stuck items: {stuck[:10]}")
        ids_now = {i["row_index"]: i["certificate_id"] for i in items}
        if ids_now != ids_at_submit:
            problems.append("certificate ids changed between submission and completion")
        if len(set(ids_now.values())) != len(ids_now):
            problems.append("duplicate certificate ids")
        files = sorted((data / "certificates").rglob("*.pdf"))
        tmps = list((data / "certificates").rglob("*.tmp"))
        succeeded = [i for i in items if i["status"] == "succeeded"]
        if len(files) != len(succeeded) or len({f.name for f in files}) != len(files):
            problems.append(f"file count {len(files)} != succeeded {len(succeeded)} (or duplicate names)")
        if tmps:
            problems.append(f"{len(tmps)} leftover .tmp files")
        bad_pdf = bad_api = bad_bytes = 0
        digests = hashlib.sha256()
        for it in sorted(succeeded, key=lambda i: i["row_index"]):
            cid = it["certificate_id"]
            f = data / "certificates" / job_id / f"{cid}.pdf"
            disk = f.read_bytes()
            if not (disk.startswith(b"%PDF-") and disk.rstrip().endswith(b"%%EOF")):
                bad_pdf += 1
            _, api_bytes = http(args.port, f"/v1/certificates/{cid}")
            if api_bytes != disk:
                bad_api += 1
            ref = render_certificate(
                CertificateData(
                    job_id=job_id,
                    certificate_id=cid,
                    name=f"Recipient {it['row_index']}",
                    email=f"r{it['row_index']}@example.com",
                    title="Chaos Course",
                    issuer="Chaos Academy",
                    issued_on="2026-10-01",
                )
            )
            if ref != disk:
                bad_bytes += 1
            digests.update(disk)
        if bad_pdf or bad_api or bad_bytes:
            problems.append(
                f"bad_pdf={bad_pdf} api_mismatch={bad_api} not_equal_to_clean_rerender={bad_bytes}"
            )
        result.update(
            {
                "job_status": job["status"],
                "job_counts": job["counts"],
                "elapsed_s": round(elapsed, 1),
                "sigkills_confirmed": len(kills),
                "sigkills_after_commit_not_counted": late_kills,
                "kills_skipped_last_attempt": skipped_last_attempt,
                "distinct_items_killed": len(killed_items),
                "max_kills_on_one_item": max(killed_items.values(), default=0),
                "worker_processes_spawned": fleet.spawned,
                "kill_returncodes": sorted({k["rc"] for k in kills}),
                "succeeded": len(succeeded),
                "failed": by_status.get("failed", 0),
                "items_with_attempts_gt1": len(multi),
                "max_attempts_seen": max(r["attempts"] for r in dbrows),
                "attempts_histogram": {
                    a: sum(1 for r in dbrows if r["attempts"] == a)
                    for a in sorted({r["attempts"] for r in dbrows})
                },
                "worker_lost_items": len(lost),
                "stuck_items": len(stuck),
                "pdf_files": len(files),
                "all_pdfs_valid_and_equal_to_clean_rerender": not (bad_pdf or bad_api or bad_bytes),
                "ids_stable_and_unique": ids_now == ids_at_submit
                and len(set(ids_now.values())) == len(ids_now),
                "problems": problems,
                "pdf_set_sha256": digests.hexdigest()[:16],
            }
        )
        # Correct final state: every item succeeded, or failed with WORKER_LOST (reported, never hidden).
        result["all_items_correct_final_state"] = (
            not problems and not stuck and len(succeeded) + by_status.get("failed", 0) == args.recipients
        )
        result["zero_failed"] = by_status.get("failed", 0) == 0
        return result
    finally:
        for p in fleet.procs:
            if p.poll() is None:
                p.kill()
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--recipients", type=int, default=300)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--policy", choices=("safe", "any"), default="safe")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--lease", type=int, default=2)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--render-ms", type=int, default=80)
    ap.add_argument("--every", type=int, default=3, help="max fresh claims to let pass between kills")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args()
    if args.render_ms >= args.lease * 1000:
        raise SystemExit("render-ms must be shorter than the lease")
    res = run(args)
    print(json.dumps(res, indent=2))
    sys.exit(0 if res.get("all_items_correct_final_state") and not res.get("timeout") else 1)


if __name__ == "__main__":
    main()
