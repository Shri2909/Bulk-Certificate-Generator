"""Per-request cost of two worst cases, in a fresh database each, for whichever certgen is on sys.path.

    PYTHONPATH=<repo> python audit/tools/compare_cost.py <scratch-dir> <label>

1. storage: one valid row + one row holding a single unknown key that fills the body limit;
   bytes persisted in jobs.request_json, items.input_json and items.errors.
2. cpu: 10,000 valid rows with 120-character Greek names and 254-character emails; request time.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from certgen.config import Settings
from certgen.main import create_app

CERT = {"title": "T", "issuer": "I", "issued_on": "2026-10-01"}
TOKEN = "audit-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def settings(scratch: Path) -> Settings:
    names = {f.name for f in dataclasses.fields(Settings)}  # the baseline lacks the newer settings
    extra: dict[str, object] = {}
    if "allowed_hosts" in names:
        extra["allowed_hosts"] = ("testserver",)
    if "api_tokens" in names:
        extra["api_tokens"] = (TOKEN,)
    return Settings(data_dir=Path(tempfile.mkdtemp(dir=scratch)) / "d", embedded_worker=False, **extra)


def main() -> None:
    scratch, label = Path(sys.argv[1]), sys.argv[2]
    scratch.mkdir(parents=True, exist_ok=True)
    s = settings(scratch)
    key = "k" * (s.max_body_bytes - 300)
    body = (
        '{"certificate":'
        + json.dumps(CERT)
        + ',"recipients":[{"name":"A","email":"a@example.com"},{"'
        + key
        + '":0}]}'
    ).encode()
    with TestClient(create_app(s), headers=AUTH) as c:
        r = c.post("/v1/certificate-jobs", content=body, headers={"Content-Type": "application/json"})
    con = sqlite3.connect(s.db_path)
    req = con.execute("select sum(length(request_json)) from jobs").fetchone()[0] or 0
    inp, err = con.execute("select sum(length(input_json)), sum(length(errors)) from items").fetchone()
    con.close()
    db_file = s.db_path.stat().st_size + (
        s.db_path.with_name(s.db_path.name + "-wal").stat().st_size
        if s.db_path.with_name(s.db_path.name + "-wal").exists()
        else 0
    )
    print(
        f"[{label}] storage: body {len(body) / 1e6:.1f} MB -> {r.status_code}; "
        f"persisted request_json {req / 1e6:.1f} MB + input_json {(inp or 0) / 1e6:.1f} MB + "
        f"errors {(err or 0) / 1e6:.1f} MB = {(req + (inp or 0) + (err or 0)) / len(body):.2f}x body; "
        f"DB files {db_file / 1e6:.0f} MB",
        flush=True,
    )

    s = settings(scratch)
    name = ("Αλέξανδρος " * 11)[:120].strip()
    rows = [
        {
            "name": name,
            "email": f"x{i:05d}" + "a" * 58 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org",
        }
        for i in range(10_000)
    ]
    body = json.dumps({"certificate": CERT, "recipients": rows}).encode()
    with TestClient(create_app(s), headers=AUTH) as c:
        started = time.perf_counter()
        r = c.post("/v1/certificate-jobs", content=body, headers={"Content-Type": "application/json"})
        took = time.perf_counter() - started
    print(
        f"[{label}] cpu: 10,000 maximal valid rows ({len(body) / 1e6:.1f} MB) -> {r.status_code} "
        f"in {took:.2f} s",
        flush=True,
    )


if __name__ == "__main__":
    main()
