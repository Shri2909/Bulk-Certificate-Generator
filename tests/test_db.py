"""SQLite engines, pragmas, BEGIN modes, schema creation and versioning (§4, §24.9)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT, job_payload, post_job
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from certgen import service, worker
from certgen.db import check_sqlite_version, create_engines, create_schema
from certgen.tables import items

# Every hot query, taken from the modules that run it, so the plan check covers the real SQL (§24.13).
QUERIES = {
    "retire": worker.RETIRE_SQL,
    "reclaim": worker.RECLAIM_SQL,
    "claim": worker.CLAIM_PENDING_SQL,
    "capacity": service.COUNT_ACTIVE_SQL,
    "job_counts": service.JOB_COUNTS_SQL,
    "list_items": service.LIST_ITEMS_SQL,
    "list_items_by_status": service.LIST_ITEMS_BY_STATUS_SQL,
}


@pytest.mark.req("R13")
def test_pragmas_applied(tmp_path: Path) -> None:
    engines = create_engines(tmp_path / "db.sqlite3")
    try:
        for engine in (engines.read, engines.write):
            with engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
                assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL, the default
                assert conn.connection.dbapi_connection.isolation_level is None  # type: ignore[union-attr]
    finally:
        engines.dispose()


@pytest.mark.req("R13")
def test_write_engine_takes_write_lock_at_begin_and_read_engine_does_not(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    engines = create_engines(path)
    create_schema(engines)
    probe = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        with engines.read.begin() as conn:
            conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
            probe.execute("BEGIN IMMEDIATE")  # a read transaction never holds the write lock
            probe.execute("ROLLBACK")
        # BEGIN IMMEDIATE already holds the write lock before any statement runs.
        with engines.write.begin(), pytest.raises(sqlite3.OperationalError, match="locked"):
            probe.execute("BEGIN IMMEDIATE")
    finally:
        probe.close()
        engines.dispose()


@pytest.mark.req("R13")
def test_schema_creation_idempotent(tmp_path: Path) -> None:
    engines = create_engines(tmp_path / "db.sqlite3")
    try:
        create_schema(engines)
        create_schema(engines)
        with engines.read.connect() as conn:
            names = set(
                conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                ).scalars()
            )
            assert conn.exec_driver_sql("PRAGMA user_version").scalar() == 1
        assert {
            "jobs",
            "items",
            "ix_items_status_id",
            "ix_items_status_lease",
            "ix_items_job_status",
        } <= names
    finally:
        engines.dispose()


@pytest.mark.req("R13")
@pytest.mark.parametrize("version", [2, 99])
def test_unknown_schema_version_refused(tmp_path: Path, version: int) -> None:
    path = tmp_path / "db.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {version}")  # test setup on a raw connection, constant int
    raw.close()
    engines = create_engines(path)
    try:
        with pytest.raises(RuntimeError, match=f"schema version {version}"):
            create_schema(engines)
    finally:
        engines.dispose()


@pytest.mark.req("R13")
def test_version_zero_with_foreign_tables_refused(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE something_else (x INTEGER)")
    raw.close()
    engines = create_engines(path)
    try:
        with pytest.raises(RuntimeError, match="schema version 0"):
            create_schema(engines)
    finally:
        engines.dispose()


@pytest.mark.req("R11")
def test_open_read_does_not_block_claim(client: Any, app: Any, make_worker: Any) -> None:
    post_job(client, job_payload(n=2))
    worker = make_worker()
    # A long-running status poll keeps a read transaction open while a worker claims.
    with app.state.engines.read.begin() as reader:
        reader.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
        started = time.monotonic()
        claim = worker.claim_next()
        elapsed = time.monotonic() - started
        reader.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
    assert claim is not None
    assert elapsed < 1.0  # no wait for busy_timeout (5 s): readers never hold the write lock


VALID_ITEM = {
    "job_id": "a" * 32,
    "row_index": 0,
    "name": "N",
    "email": "e@example.com",
    "status": "pending",
    "attempts": 0,
    "certificate_id": "c" * 32,
    "input_json": "{}",
}


@pytest.mark.req("R6")
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "rejected"},  # rejected must have no certificate_id
        {"status": "pending", "certificate_id": None},  # valid rows always have one
        {"status": "processing"},  # processing needs a lease token and expiry
        {"lease_token": "t"},  # a token without processing
        {"status": "succeeded"},  # finished rows need completed_at
        {"completed_at": 1},  # completed_at only on finished rows
        {"status": "rejected", "certificate_id": None, "attempts": 1},  # rejected rows are never attempted
        {"name": None},  # valid rows keep name and email
        {"status": "unknown"},
        {"row_index": -1},
        {"attempts": -1},
        {"input_json": "not json"},
    ],
)
def test_check_constraints_reject_invalid_rows(tmp_path: Path, overrides: dict[str, Any]) -> None:
    engines = create_engines(tmp_path / "db.sqlite3")
    try:
        create_schema(engines)
        with engines.write.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, title, issuer, issued_on, print_email, verify_base_url, "
                    "request_json, created_at) "
                    "VALUES (:id, 't', 'i', '2026-10-01', 1, 'http://localhost:8000', '{}', 0)"
                ),
                {"id": "a" * 32},
            )
            conn.execute(items.insert(), VALID_ITEM)  # the baseline row is accepted
        with pytest.raises(IntegrityError), engines.write.begin() as conn:
            conn.execute(
                items.insert(), {**VALID_ITEM, "row_index": 1, "certificate_id": "d" * 32, **overrides}
            )
    finally:
        engines.dispose()


SCHEMA_SCRIPT = """
import sys
from pathlib import Path
from certgen.db import create_engines, create_schema
engines = create_engines(Path(sys.argv[1]))
create_schema(engines)
engines.dispose()
"""


@pytest.mark.slow
@pytest.mark.req("R13")
def test_concurrent_schema_creation_two_processes(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", SCHEMA_SCRIPT, str(db)],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        for _ in range(4)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    raw = sqlite3.connect(db)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    assert tables == {"jobs", "items"}


@pytest.mark.req("R13")
def test_sqlite_version_check() -> None:
    check_sqlite_version((3, 35, 0))
    check_sqlite_version(sqlite3.sqlite_version_info)
    with pytest.raises(RuntimeError, match=r"SQLite 3\.34\.1 is too old.*3\.35\.0"):
        check_sqlite_version((3, 34, 1))


@pytest.mark.req("R11")
def test_query_plans_use_indexes(tmp_path: Path) -> None:
    engines = create_engines(tmp_path / "db.sqlite3")
    params = {
        "now": 0,
        "max_attempts": 3,
        "job_id": "a" * 32,
        "worker_lost": "[]",
        "status": "failed",
        "after": -1,
        "limit": 101,
    }
    try:
        create_schema(engines)
        with engines.read.connect() as conn:
            for name, sql in QUERIES.items():
                plan = conn.execute(text("EXPLAIN QUERY PLAN " + sql), params).all()
                details = " | ".join(str(row[-1]) for row in plan)
                assert "SCAN items" not in details, f"{name}: {details}"
                assert "USING" in details, f"{name}: {details}"
    finally:
        engines.dispose()
