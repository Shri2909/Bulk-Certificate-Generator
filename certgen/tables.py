"""Schema (§4, §24.4). CHECK constraints make invalid row shapes impossible rather than merely untested."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

jobs = Table(
    "jobs",
    metadata,
    Column("id", Text, CheckConstraint("length(id) = 32"), primary_key=True),
    Column("title", Text, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("issued_on", Text, nullable=False),
    # Snapshots taken at creation, so changing the deployment's settings never alters accepted jobs.
    Column("print_email", Integer, CheckConstraint("print_email IN (0, 1)"), nullable=False),
    Column("verify_base_url", Text, nullable=False),
    Column("idempotency_key", Text, unique=True),
    Column("request_hash", Text),
    Column("request_json", Text, CheckConstraint("json_valid(request_json)"), nullable=False),
    Column("created_at", Integer, nullable=False),
    CheckConstraint("(idempotency_key IS NULL) = (request_hash IS NULL)"),
)

items = Table(
    "items",
    metadata,
    Column("id", Integer, primary_key=True),  # rowid; claim order is FIFO by id
    Column("job_id", Text, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("row_index", Integer, CheckConstraint("row_index >= 0"), nullable=False),
    Column("name", Text),
    Column("email", Text),
    Column(
        "status",
        Text,
        CheckConstraint("status IN ('rejected','pending','processing','succeeded','failed')"),
        nullable=False,
    ),
    Column("attempts", Integer, CheckConstraint("attempts >= 0"), nullable=False, server_default="0"),
    Column("errors", Text),
    Column("certificate_id", Text, unique=True),
    Column("lease_owner", Text),  # worker id for logs only; fencing never reads it
    Column("lease_token", Text),
    Column("lease_expires_at", Integer),
    Column("available_at", Integer),
    Column("completed_at", Integer),
    Column("input_json", Text, CheckConstraint("json_valid(input_json)"), nullable=False),
    UniqueConstraint("job_id", "row_index"),
    CheckConstraint("(status = 'rejected') = (certificate_id IS NULL)"),
    CheckConstraint("(status = 'processing') = (lease_token IS NOT NULL)"),
    CheckConstraint("(status = 'processing') = (lease_expires_at IS NOT NULL)"),
    CheckConstraint("(status IN ('succeeded','failed')) = (completed_at IS NOT NULL)"),
    CheckConstraint("status <> 'rejected' OR attempts = 0"),
    CheckConstraint("status = 'rejected' OR (name IS NOT NULL AND email IS NOT NULL)"),
)

Index("ix_items_status_id", items.c.status, items.c.id)
Index("ix_items_status_lease", items.c.status, items.c.lease_expires_at)
Index("ix_items_job_status", items.c.job_id, items.c.status, items.c.row_index)
