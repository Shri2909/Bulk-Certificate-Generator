"""HTTP <-> service mapping (§6). Routes are plain `def`, so FastAPI runs them in its threadpool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import anyio.from_thread
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from certgen import archive, service
from certgen.errors import ApiError, FieldCode, FieldError, ProblemCode, body_errors_from_pydantic
from certgen.models import (
    CertificateOut,
    Counts,
    CreateJobRequest,
    Health,
    ItemResource,
    ItemsPage,
    JobCreated,
    JobLinks,
    JobResource,
)
from certgen.storage import HEX32
from certgen.validation import JsonBodyError, canonical_json, decode_json_body, validate_request

router = APIRouter()

JOBS_PATH = "/v1/certificate-jobs"
ItemStatus = Literal["rejected", "pending", "processing", "succeeded", "failed"]


def iso_ms(epoch_ms: int) -> str:
    seconds, millis = divmod(epoch_ms, 1000)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def not_found() -> ApiError:
    return ApiError(ProblemCode.NOT_FOUND, "The requested resource was not found.")


def job_resource(view: service.JobView) -> dict[str, Any]:
    base = f"{JOBS_PATH}/{view.id}"
    return JobResource(
        id=view.id,
        status=view.status,
        certificate=CertificateOut(title=view.title, issuer=view.issuer, issued_on=view.issued_on),
        print_email=view.print_email,
        counts=Counts(**view.counts),
        progress=view.progress,
        created_at=iso_ms(view.created_at),
        finished_at=iso_ms(view.finished_at) if view.finished_at is not None else None,
        links=JobLinks(self=base, items=f"{base}/items", archive=f"{base}/archive"),
    ).model_dump()


def invalid(errors: list[FieldError], detail: str = "Request is invalid.") -> ApiError:
    return ApiError(ProblemCode.VALIDATION_FAILED, detail, errors)


def is_json_media_type(content_type: str | None) -> bool:
    """application/json, optionally with charset=utf-8, case-insensitive (§24.5 step 1)."""
    if content_type is None:
        return False
    media_type, *params = (part.strip().lower() for part in content_type.split(";"))
    return media_type == "application/json" and all(p.replace('"', "") == "charset=utf-8" for p in params)


def read_json_body(request: Request) -> Any:
    """§24.5: the body is parsed here instead of by FastAPI, so duplicate keys, NaN and deep nesting
    are rejected rather than silently accepted."""
    if not is_json_media_type(request.headers.get("content-type")):
        raise ApiError(ProblemCode.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json.")
    # Request.body() is async; this sync route runs in a worker thread, which may call into the loop.
    raw: bytes = anyio.from_thread.run(request.body)
    if not raw:
        raise invalid([FieldError(None, FieldCode.REQUIRED, "A JSON request body is required.")])
    try:
        return decode_json_body(raw)
    except JsonBodyError as exc:
        raise invalid([FieldError(None, FieldCode.INVALID_JSON, str(exc))]) from None


@router.post(JOBS_PATH, status_code=202, response_model=JobCreated)
def create_job(request: Request, response: Response) -> JobCreated:
    state = request.app.state
    obj = read_json_body(request)
    try:
        body = CreateJobRequest.model_validate(obj)
    except ValidationError as exc:
        raise invalid(body_errors_from_pydantic(exc.errors())) from None
    today = datetime.fromtimestamp(state.clock() // 1000, UTC).date()
    validated = validate_request(
        body,
        today=today,
        print_email=state.settings.print_email,
        max_recipients=state.settings.max_recipients,
    )
    if validated.request_errors:
        raise invalid(validated.all_errors)
    if validated.valid_count == 0:
        n = len(validated.rows)
        raise invalid(validated.all_errors, f"{n} of {n} recipients are invalid; no job was created.")
    view = service.create_job(state.engines, state.settings, state.clock, validated, canonical_json(obj))
    response.headers["Location"] = f"{JOBS_PATH}/{view.id}"
    return JobCreated(**job_resource(view), errors=[e.as_flat() for e in validated.row_errors])


@router.get(JOBS_PATH + "/{job_id}", response_model=JobResource)
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    view = service.get_job(request.app.state.engines, job_id) if HEX32.fullmatch(job_id) else None
    if view is None:
        raise not_found()
    return job_resource(view)


@router.get("/v1/certificates/{certificate_id}", response_class=Response)
def download_certificate(request: Request, certificate_id: str) -> Response:
    state = request.app.state
    record = (
        service.find_certificate(state.engines, certificate_id) if HEX32.fullmatch(certificate_id) else None
    )
    if record is None:
        raise not_found()
    if record.status in ("pending", "processing"):
        raise ApiError(ProblemCode.CERTIFICATE_NOT_READY, "The certificate has not been generated yet.")
    if record.status == "failed":
        raise ApiError(
            ProblemCode.CERTIFICATE_GENERATION_FAILED,
            "Generating this certificate failed; see the job's items for the reason.",
        )
    data = state.storage.read(record.job_id, record.certificate_id)
    if data is None:
        raise ApiError(ProblemCode.CERTIFICATE_FILE_MISSING, "The certificate file is missing on the server.")
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="certificate-{record.certificate_id}.pdf"'},
    )


@router.get("/healthz", response_model=Health)
def healthz(request: Request) -> Health:
    state = request.app.state
    try:
        service.ping(state.engines)
    except OperationalError:
        raise ApiError(ProblemCode.DB_UNAVAILABLE, "The database cannot be read.") from None
    pool = state.workers
    expected = pool.size if pool is not None else 0
    alive = pool.alive() if pool is not None else 0
    # Degraded still answers 200: the API serves reads and accepts jobs; work waits for a worker.
    return Health(
        status="ok" if alive >= expected else "degraded",
        db="ok",
        embedded_workers_alive=alive,
        embedded_workers_expected=expected,
    )


@router.get(JOBS_PATH + "/{job_id}/items", response_model=ItemsPage)
def list_items(
    request: Request,
    job_id: str,
    status: Annotated[ItemStatus | None, Query()] = None,
    after: Annotated[int, Query(ge=-1)] = -1,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ItemsPage:
    page = (
        service.list_items(request.app.state.engines, job_id, status, after, limit)
        if HEX32.fullmatch(job_id)
        else None
    )
    if page is None:
        raise not_found()
    return ItemsPage(
        job_id=job_id,
        limit=limit,
        next_after=page.next_after,
        items=[
            ItemResource(
                row_index=item.row_index,
                name=item.name,
                email=item.email,
                status=item.status,
                attempts=item.attempts,
                errors=item.errors,
                certificate_id=item.certificate_id,
                download_url=f"/v1/certificates/{item.certificate_id}"
                if item.status == "succeeded"
                else None,
            )
            for item in page.items
        ],
    )


@router.get(JOBS_PATH + "/{job_id}/archive", response_class=StreamingResponse)
def download_archive(request: Request, job_id: str) -> StreamingResponse:
    state = request.app.state
    if not HEX32.fullmatch(job_id):
        raise not_found()
    # Non-blocking: a third concurrent build is refused instead of queueing behind 240 MB ZIPs.
    if not state.archive_slots.acquire(blocking=False):
        raise ApiError(ProblemCode.ARCHIVE_BUSY, "Too many archives are being built; retry shortly.")
    try:
        rows = service.archive_rows(state.engines, job_id)
        if rows is None:
            raise not_found()
        zip_file = archive.build_archive(rows, state.storage)
    finally:
        state.archive_slots.release()
    return StreamingResponse(
        archive.stream(zip_file),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates-{job_id}.zip"'},
    )
