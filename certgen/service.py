"""Database use cases (§7.1). No FastAPI, no rendering: callers get plain dataclasses back."""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from certgen.clock import Clock
from certgen.config import Settings
from certgen.db import Engines
from certgen.errors import ApiError, ProblemCode
from certgen.status import derive_job_status
from certgen.tables import items, jobs
from certgen.validation import ValidatedRequest, ValidatedRow

ITEM_STATUSES = ("rejected", "pending", "processing", "succeeded", "failed")
MAX_ID_ATTEMPTS = 5  # §24.10: collisions are practically unreachable at 128 bits; this bounds the loop


def new_job_id() -> str:
    return uuid.uuid4().hex


def new_certificate_id() -> str:
    return secrets.token_hex(16)


def dump_errors(errors: list[dict[str, Any]]) -> str | None:
    return json.dumps(errors, ensure_ascii=False, separators=(",", ":")) if errors else None


@dataclass(frozen=True)
class JobView:
    id: str
    title: str
    issuer: str
    issued_on: str
    print_email: bool
    created_at: int
    counts: dict[str, int]
    status: str
    progress: float
    finished_at: int | None


@dataclass(frozen=True)
class CertificateRecord:
    job_id: str
    certificate_id: str
    status: str


COUNT_ACTIVE_SQL = "SELECT COUNT(*) FROM items WHERE status IN ('pending', 'processing')"
JOB_COUNTS_SQL = (
    "SELECT status, COUNT(*) AS n, SUM(attempts > 0) AS attempted, MAX(completed_at) AS last_done "
    "FROM items WHERE job_id = :job_id GROUP BY status"
)


def count_active_items(conn: Connection) -> int:
    return int(conn.execute(text(COUNT_ACTIVE_SQL)).scalar_one())


def ping(engines: Engines) -> None:
    """Health probe: raises if the database cannot be read."""
    with engines.read.connect() as conn:
        conn.execute(text("SELECT 1")).scalar_one()


def create_job(
    engines: Engines, settings: Settings, clock: Clock, validated: ValidatedRequest, request_json: str
) -> JobView:
    """Insert the job and all its items in one write transaction; any exception rolls everything back."""
    with engines.write.begin() as conn:
        now = clock()  # read after BEGIN IMMEDIATE holds the lock, so timestamps follow commit order
        active = count_active_items(conn)
        if active + validated.valid_count > settings.max_active_items:
            raise ApiError(
                ProblemCode.CAPACITY_EXCEEDED,
                f"The queue holds {active} unfinished certificates; accepting {validated.valid_count} more "
                f"would exceed the limit of {settings.max_active_items}. Retry later.",
            )
        job_id = _insert_job(conn, settings, validated, request_json, now)
        _insert_items(conn, job_id, validated.rows)
        view = _load_job(conn, job_id)
    assert view is not None
    return view


def _insert_job(
    conn: Connection, settings: Settings, validated: ValidatedRequest, request_json: str, now: int
) -> str:
    for _ in range(MAX_ID_ATTEMPTS):
        job_id = new_job_id()
        try:
            with conn.begin_nested():  # savepoint: a collision undoes only this insert
                conn.execute(
                    jobs.insert().values(
                        id=job_id,
                        title=validated.title,
                        issuer=validated.issuer,
                        issued_on=validated.issued_on,
                        print_email=int(validated.print_email),
                        verify_base_url=settings.public_base_url,
                        request_json=request_json,
                        created_at=now,
                    )
                )
            return job_id
        except IntegrityError as exc:
            if "jobs.id" not in str(exc.orig):
                raise
    raise RuntimeError(f"no unique job id after {MAX_ID_ATTEMPTS} attempts")


def _insert_items(conn: Connection, job_id: str, rows: tuple[ValidatedRow, ...]) -> None:
    for _ in range(MAX_ID_ATTEMPTS):
        # IDs are drawn fresh on every attempt; none has been shown to anyone yet, so redrawing the
        # whole batch is equivalent to redrawing only the colliding one and needs no per-row lookup.
        values = [_item_values(job_id, row) for row in rows]
        try:
            with conn.begin_nested():
                conn.execute(items.insert(), values)
            return
        except IntegrityError as exc:
            if "items.certificate_id" not in str(exc.orig):
                raise
    raise RuntimeError(f"no unique certificate ids after {MAX_ID_ATTEMPTS} attempts")


def _item_values(job_id: str, row: ValidatedRow) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "row_index": row.row_index,
        "name": row.name,
        "email": row.email,
        "status": "pending" if row.valid else "rejected",
        "attempts": 0,
        "errors": dump_errors([e.as_item_error() for e in row.errors]),
        "certificate_id": new_certificate_id() if row.valid else None,
        "input_json": row.input_json,
    }


def get_job(engines: Engines, job_id: str) -> JobView | None:
    with engines.read.begin() as conn:  # one transaction: job row and counts see the same snapshot
        return _load_job(conn, job_id)


def _load_job(conn: Connection, job_id: str) -> JobView | None:
    job = (
        conn.execute(
            text("SELECT id, title, issuer, issued_on, print_email, created_at FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        )
        .mappings()
        .first()
    )
    if job is None:
        return None
    counts = dict.fromkeys(ITEM_STATUSES, 0)
    attempted = 0
    finished_at: int | None = None
    groups = conn.execute(text(JOB_COUNTS_SQL), {"job_id": job_id}).mappings()
    for group in groups:
        counts[group["status"]] = group["n"]
        attempted += group["attempted"]
        if group["last_done"] is not None:
            finished_at = max(finished_at or 0, group["last_done"])
    status, progress = derive_job_status(counts, attempted)
    terminal = status in ("completed", "completed_with_errors", "failed")
    return JobView(
        id=job["id"],
        title=job["title"],
        issuer=job["issuer"],
        issued_on=job["issued_on"],
        print_email=bool(job["print_email"]),
        created_at=job["created_at"],
        counts={"total": sum(counts.values()), **counts},
        status=status,
        progress=progress,
        finished_at=finished_at if terminal else None,
    )


def find_certificate(engines: Engines, certificate_id: str) -> CertificateRecord | None:
    with engines.read.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT job_id, certificate_id, status FROM items WHERE certificate_id = :certificate_id"
                ),
                {"certificate_id": certificate_id},
            )
            .mappings()
            .first()
        )
    return CertificateRecord(**row) if row is not None else None
