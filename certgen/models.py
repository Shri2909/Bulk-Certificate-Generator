"""Pydantic models: the JSON shape of requests and responses only (§6). Rules live in validation.py."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr


class CertificateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: StrictStr
    issuer: StrictStr
    issued_on: StrictStr


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certificate: CertificateIn
    # Rows stay plain dicts so one malformed row becomes a rejected item, not a whole-request 422.
    recipients: list[dict[str, Any]]


class CertificateOut(BaseModel):
    title: str
    issuer: str
    issued_on: str


class Counts(BaseModel):
    total: int
    rejected: int
    pending: int
    processing: int
    succeeded: int
    failed: int


class JobLinks(BaseModel):
    self: str
    items: str
    archive: str


class JobResource(BaseModel):
    id: str
    status: str
    certificate: CertificateOut
    print_email: bool
    counts: Counts
    progress: float
    created_at: str
    finished_at: str | None
    links: JobLinks


class JobCreated(JobResource):
    # Flat row errors of rejected rows; dicts because `duplicate_of_row_index` appears only on duplicates.
    errors: list[dict[str, Any]]


class Health(BaseModel):
    status: str
    db: str
    embedded_workers_alive: int
    embedded_workers_expected: int


class ItemResource(BaseModel):
    row_index: int
    name: str | None
    email: str | None
    status: str
    attempts: int
    errors: list[dict[str, Any]]
    certificate_id: str | None
    download_url: str | None


class ItemsPage(BaseModel):
    job_id: str
    limit: int
    next_after: int | None
    items: list[ItemResource]


class DryRunCounts(BaseModel):
    total: int
    valid: int
    rejected: int


class DryRunReport(BaseModel):
    """D3: what a real submission would do; nothing is stored."""

    dry_run: bool
    would_create_job: bool
    counts: DryRunCounts
    errors: list[dict[str, Any]]
