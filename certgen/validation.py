"""Pure validation and normalization of a create-job request (§9). No DB, HTTP, files or clock."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from certgen.errors import FieldError
from certgen.models import CreateJobRequest

SPACE_RUNS = re.compile(" {2,}")


def canonical_json(value: Any) -> str:
    """One canonical form for the audit copy and the idempotency hash (§24.4)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def norm(value: str) -> str:
    """NFC, then trim and collapse U+0020 only; other whitespace is rejected later, not silently fixed."""
    return SPACE_RUNS.sub(" ", unicodedata.normalize("NFC", value).strip(" "))


@dataclass(frozen=True)
class ValidatedRow:
    row_index: int
    name: str | None
    email: str | None
    errors: tuple[FieldError, ...]
    input_json: str

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ValidatedRequest:
    title: str
    issuer: str
    issued_on: str
    print_email: bool
    rows: tuple[ValidatedRow, ...]
    request_errors: tuple[FieldError, ...]

    @property
    def valid_count(self) -> int:
        return sum(1 for row in self.rows if row.valid)

    @property
    def row_errors(self) -> list[FieldError]:
        return [error for row in self.rows for error in row.errors]


def validate_request(body: CreateJobRequest, *, print_email: bool) -> ValidatedRequest:
    rows = tuple(
        ValidatedRow(
            row_index=i,
            name=norm(raw["name"]),
            email=raw["email"].strip(" "),
            errors=(),
            input_json=canonical_json(raw),
        )
        for i, raw in enumerate(body.recipients)
    )
    return ValidatedRequest(
        title=norm(body.certificate.title),
        issuer=norm(body.certificate.issuer),
        issued_on=body.certificate.issued_on,
        print_email=print_email,
        rows=rows,
        request_errors=(),
    )
