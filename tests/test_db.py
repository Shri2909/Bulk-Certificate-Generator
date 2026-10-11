"""SQLite engines, pragmas, BEGIN modes, schema creation and versioning (§4, §24.9)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from certgen.db import create_engines, create_schema


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
