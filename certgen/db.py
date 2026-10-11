"""SQLite engines (§4). Two engines over one file: writers take the write lock at BEGIN, readers never do.

SQLite has a single write lock. A transaction that reads first and writes later (deferred BEGIN) can
fail with "database is locked" when it tries to upgrade; BEGIN IMMEDIATE takes the lock up front and
waits up to busy_timeout instead. Readers use plain BEGIN so, under WAL, status polling never blocks
a claim and a claim never blocks status polling.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event

from certgen.tables import metadata

MIN_SQLITE_VERSION = (3, 35, 0)  # RETURNING
SCHEMA_VERSION = 1
BUSY_TIMEOUT_SECONDS = 5.0  # same as the busy_timeout pragma below
WAL_SWITCH_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class Engines:
    read: Engine
    write: Engine

    def dispose(self) -> None:
        self.read.dispose()
        self.write.dispose()


def check_sqlite_version(version: tuple[int, int, int] = sqlite3.sqlite_version_info) -> None:
    if version < MIN_SQLITE_VERSION:
        found = ".".join(map(str, version))
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        raise RuntimeError(f"SQLite {found} is too old; certgen needs SQLite >= {need} (RETURNING support)")


def _enable_wal(cursor: sqlite3.Cursor) -> None:
    """Switch to WAL once per database file; later connections see it is already set.

    SQLite answers a journal-mode change with SQLITE_BUSY *without* calling the busy handler when
    another connection is mid-switch (deadlock avoidance), so busy_timeout does not cover the first
    concurrent boot of several processes. This gives that one statement the same bounded wait.
    """
    if cursor.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(WAL_SWITCH_POLL_SECONDS)


def _make_engine(db_path: Path, begin_statement: str) -> Engine:
    # hide_parameters: SQL errors would otherwise put bound values (names, emails) into logs.
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, hide_parameters=True
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Hand transaction control to SQLAlchemy's "begin" event below instead of pysqlite's
        # implicit BEGIN, which would always be deferred.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        _enable_wal(cursor)
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql(begin_statement)

    return engine


def create_engines(db_path: Path) -> Engines:
    return Engines(read=_make_engine(db_path, "BEGIN"), write=_make_engine(db_path, "BEGIN IMMEDIATE"))


def create_schema(engines: Engines) -> None:
    """Create tables once; refuse databases from another schema version (§24.9).

    Runs inside BEGIN IMMEDIATE, so N processes booting at once serialize: the first creates,
    the rest see user_version = 1 and return.
    """
    with engines.write.begin() as conn:
        version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
        if version == SCHEMA_VERSION:
            return
        table_count = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).scalar_one()
        if version != 0 or table_count != 0:
            raise RuntimeError(
                f"Refusing to start: database schema version {version} "
                f"({table_count} existing tables) is not supported; expected an empty database "
                f"or schema version {SCHEMA_VERSION}. Schema changes need a migration."
            )
        metadata.create_all(conn)
        conn.exec_driver_sql("PRAGMA user_version = 1")
