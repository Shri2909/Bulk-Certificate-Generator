"""Database use cases (§7.1). No FastAPI, no rendering: callers get plain dataclasses back."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from certgen.archive import ArchiveRow
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


def request_hash(request_json: str) -> str:
    """D4 fingerprint of the canonical request (§24.4): the audit copy and the key check agree."""
    return hashlib.sha256(request_json.encode("utf-8")).hexdigest()


def key_reused() -> ApiError:
    return ApiError(
        ProblemCode.IDEMPOTENCY_KEY_REUSED,
        "This Idempotency-Key was already used with a different request body.",
    )


def _job_for_key(conn: Connection, key: str, fingerprint: str) -> str | None:
    """Job id for a replay (same key, same body); raises 409 if the key belongs to another body."""
    row = conn.execute(
        text("SELECT id, request_hash FROM jobs WHERE idempotency_key = :key"), {"key": key}
    ).first()
    if row is None:
        return None
    if row.request_hash != fingerprint:
        raise key_reused()
    return str(row.id)


def find_replay(engines: Engines, key: str, fingerprint: str) -> JobView | None:
    """D4 fast path before validation: an earlier job created with this key and body."""
    with engines.read.begin() as conn:
        job_id = _job_for_key(conn, key, fingerprint)
        return _load_job(conn, job_id) if job_id is not None else None


def rejected_row_errors(engines: Engines, job_id: str) -> list[dict[str, Any]]:
    """Flat row errors of a job's rejected rows, as the 202 body listed them (for replays)."""
    with engines.read.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT row_index, errors FROM items WHERE job_id = :job_id AND status = 'rejected' "
                "ORDER BY row_index"
            ),
            {"job_id": job_id},
        ).all()
    return [{"row_index": r.row_index, **e} for r in rows for e in json.loads(r.errors)]


def create_job(
    engines: Engines,
    settings: Settings,
    clock: Clock,
    validated: ValidatedRequest,
    request_json: str,
    idempotency_key: str | None = None,
) -> tuple[JobView, bool]:
    """Insert the job and all its items in one write transaction; any exception rolls everything back.

    Returns (job, replayed). With a key, the lookup is repeated under the write lock, so concurrent
    requests with the same key create exactly one job; the others become replays.
    """
    fingerprint = request_hash(request_json) if idempotency_key is not None else None
    try:
        return _create_job(engines, settings, clock, validated, request_json, idempotency_key, fingerprint)
    except IntegrityError as exc:
        # Backstop only: BEGIN IMMEDIATE serializes creators, so the re-check below sees every key first.
        if idempotency_key is None or fingerprint is None or "jobs.idempotency_key" not in str(exc.orig):
            raise
        replay = find_replay(engines, idempotency_key, fingerprint)
        if replay is None:
            raise
        return replay, True


def _create_job(
    engines: Engines,
    settings: Settings,
    clock: Clock,
    validated: ValidatedRequest,
    request_json: str,
    idempotency_key: str | None,
    fingerprint: str | None,
) -> tuple[JobView, bool]:
    with engines.write.begin() as conn:
        if idempotency_key is not None and fingerprint is not None:
            existing = _job_for_key(conn, idempotency_key, fingerprint)
            if existing is not None:  # replays skip the capacity check: no new work is admitted
                view = _load_job(conn, existing)
                assert view is not None
                return view, True
        now = clock()  # read after BEGIN IMMEDIATE holds the lock, so timestamps follow commit order
        active = count_active_items(conn)
        if active + validated.valid_count > settings.max_active_items:
            raise ApiError(
                ProblemCode.CAPACITY_EXCEEDED,
                f"The queue holds {active} unfinished certificates; accepting {validated.valid_count} more "
                f"would exceed the limit of {settings.max_active_items}. Retry later.",
            )
        job_id = _insert_job(conn, settings, validated, request_json, now, idempotency_key, fingerprint)
        _insert_items(conn, job_id, validated.rows)
        view = _load_job(conn, job_id)
    assert view is not None
    return view, False


def _insert_job(
    conn: Connection,
    settings: Settings,
    validated: ValidatedRequest,
    request_json: str,
    now: int,
    idempotency_key: str | None = None,
    fingerprint: str | None = None,
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
                        idempotency_key=idempotency_key,
                        request_hash=fingerprint,
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


# Keyset pagination: "row_index > :after" walks an index instead of skipping rows like OFFSET does.
LIST_ITEMS_SQL = (
    "SELECT row_index, name, email, status, attempts, errors, certificate_id "
    "FROM items WHERE job_id = :job_id AND row_index > :after ORDER BY row_index LIMIT :limit"
)
LIST_ITEMS_BY_STATUS_SQL = (
    "SELECT row_index, name, email, status, attempts, errors, certificate_id "
    "FROM items WHERE job_id = :job_id AND status = :status AND row_index > :after "
    "ORDER BY row_index LIMIT :limit"
)
ARCHIVE_ROWS_SQL = (
    "SELECT job_id, row_index, name, email, status, certificate_id, errors "
    "FROM items WHERE job_id = :job_id ORDER BY row_index"
)


@dataclass(frozen=True)
class ItemView:
    row_index: int
    name: str | None
    email: str | None
    status: str
    attempts: int
    errors: list[dict[str, Any]]
    certificate_id: str | None


@dataclass(frozen=True)
class ItemsPage:
    items: list[ItemView]
    next_after: int | None


def _job_exists(conn: Connection, job_id: str) -> bool:
    return conn.execute(text("SELECT 1 FROM jobs WHERE id = :job_id"), {"job_id": job_id}).first() is not None


def list_items(engines: Engines, job_id: str, status: str | None, after: int, limit: int) -> ItemsPage | None:
    """One page in row order; None if the job does not exist. Each page reflects the current state."""
    with engines.read.begin() as conn:
        if not _job_exists(conn, job_id):
            return None
        params = {"job_id": job_id, "status": status, "after": after, "limit": limit + 1}
        query = text(LIST_ITEMS_SQL) if status is None else text(LIST_ITEMS_BY_STATUS_SQL)
        rows = conn.execute(query, params).mappings().all()
    has_more = len(rows) > limit  # fetched one extra row only to learn whether another page exists
    page = [
        ItemView(
            row_index=r["row_index"],
            name=r["name"],
            email=r["email"],
            status=r["status"],
            attempts=r["attempts"],
            errors=json.loads(r["errors"]) if r["errors"] else [],
            certificate_id=r["certificate_id"],
        )
        for r in rows[:limit]
    ]
    return ItemsPage(items=page, next_after=page[-1].row_index if has_more else None)


def archive_rows(engines: Engines, job_id: str) -> list[ArchiveRow] | None:
    """Every row of the job, read in one snapshot; the connection is closed before any file is read."""
    with engines.read.begin() as conn:
        if not _job_exists(conn, job_id):
            return None
        return [ArchiveRow(**r) for r in conn.execute(text(ARCHIVE_ROWS_SQL), {"job_id": job_id}).mappings()]


# T9 (D3): every failed item of the job back to pending, with a clean slate. A stale worker that still
# holds an old claim is fenced out: failed rows carry no token, and the next claim issues a new one.
RETRY_FAILED_SQL = (
    "UPDATE items SET status = 'pending', attempts = 0, errors = NULL, available_at = NULL, "
    "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, completed_at = NULL "
    "WHERE job_id = :job_id AND status = 'failed'"
)


def retry_failed(engines: Engines, settings: Settings, clock: Clock, job_id: str) -> JobView | None:
    """D3: re-queue the job's failed items in one write transaction; None if the job does not exist."""
    with engines.write.begin() as conn:
        if not _job_exists(conn, job_id):
            return None
        failed = int(
            conn.execute(
                text("SELECT COUNT(*) FROM items WHERE job_id = :job_id AND status = 'failed'"),
                {"job_id": job_id},
            ).scalar_one()
        )
        if failed == 0:
            raise ApiError(ProblemCode.NOTHING_TO_RETRY, "The job has no failed certificates to retry.")
        active = count_active_items(conn)
        if active + failed > settings.max_active_items:  # same admission rule as a new job (§24.1)
            raise ApiError(
                ProblemCode.CAPACITY_EXCEEDED,
                f"The queue holds {active} unfinished certificates; re-queuing {failed} more "
                f"would exceed the limit of {settings.max_active_items}. Retry later.",
            )
        conn.execute(text(RETRY_FAILED_SQL), {"job_id": job_id})
        view = _load_job(conn, job_id)
    assert view is not None
    return view
