"""Pure validation and normalization of a create-job request (§9, §24.5, §24.11).

No DB, HTTP, files or clock: "today" and settings are passed in, so every rule is unit-testable.
Policy: accept exactly what will render correctly, and reject the rest with a reason instead of
silently changing it.
"""

from __future__ import annotations

import heapq
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from email_validator import EmailNotValidError, validate_email

from certgen.errors import FieldCode, FieldError
from certgen.fonts import SUPPORTED_CHARS
from certgen.layout import EMAIL, ISSUER, NAME, TITLE, Block, fit_text
from certgen.models import CreateJobRequest

SPACE_RUNS = re.compile(" {2,}")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAX_JSON_DEPTH = 8  # root = 1; the deepest legitimate value (a row field) sits at depth 4
MAX_NAME_LENGTH = 120
MAX_CERTIFICATE_TEXT_LENGTH = 200
MAX_EMAIL_LENGTH = 254  # RFC 5321 path limit; also what email-validator enforces, but only after parsing
RTL_BIDI_CLASSES = frozenset({"R", "AL", "AN"})
ROW_FIELDS = ("name", "email")
MAX_UNKNOWN_FIELD_ERRORS = 10  # per row; further unknown keys are counted in one summary error
MAX_QUOTED_KEY = 64  # characters of an unknown key repeated in its error message


class JsonBodyError(ValueError):
    """The body is not acceptable JSON; the message is safe to show to the client."""


def canonical_json(value: Any) -> str:
    """One canonical form for the audit copy and the idempotency hash (§24.4)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode_json_body(raw: bytes) -> Any:
    """Strict JSON (§24.5): UTF-8 only, no duplicate keys, no NaN/Infinity, depth <= 8."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise JsonBodyError("The request body is not valid UTF-8.") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            parse_int=_bounded_int,
        )
    except JsonBodyError:
        raise
    except RecursionError:
        raise JsonBodyError("The request body is nested too deeply.") from None
    except json.JSONDecodeError as exc:
        raise JsonBodyError(f"The request body is not valid JSON: {exc.msg}.") from None
    if _depth_exceeds(value, MAX_JSON_DEPTH):
        raise JsonBodyError(f"The request body is nested deeper than {MAX_JSON_DEPTH} levels.")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            # Python would keep the last value silently; the client may have meant the first.
            raise JsonBodyError(f"Duplicate key {key!r} in a JSON object.")
        out[key] = value
    return out


def _finite_float(literal: str) -> float:
    # 1e400 parses to inf, which cannot be stored back as valid JSON.
    value = float(literal)
    if not math.isfinite(value):
        raise JsonBodyError(f"Number {literal[:20]} is out of range.")
    return value


def _bounded_int(literal: str) -> int:
    try:
        return int(literal)
    except ValueError:  # Python refuses integers longer than its digit limit
        raise JsonBodyError("An integer in the request body has too many digits.") from None


def _reject_constant(name: str) -> Any:
    raise JsonBodyError(f"{name} is not valid JSON.")


def _depth_exceeds(value: Any, limit: int) -> bool:
    if limit == 0:
        return True
    if isinstance(value, dict):
        return any(_depth_exceeds(v, limit - 1) for v in value.values())
    if isinstance(value, list):
        return any(_depth_exceeds(v, limit - 1) for v in value)
    return False


def norm(value: str) -> str:
    """NFC, then trim and collapse U+0020 only; other whitespace is rejected by C1, not silently fixed."""
    return SPACE_RUNS.sub(" ", unicodedata.normalize("NFC", value).strip(" "))


def _charset_error(field: str, value: str) -> FieldError | None:
    """C1 then C2 (§9); the message names the first offending character."""
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] == "C" or (category[0] == "Z" and ch != " "):
            return FieldError(
                field, FieldCode.INVALID_CHARACTERS, f"Character U+{ord(ch):04X} is not allowed."
            )
    for ch in value:
        # Combining marks left after NFC and right-to-left text need shaping ReportLab cannot do.
        if (
            unicodedata.category(ch)[0] == "M"
            or unicodedata.bidirectional(ch) in RTL_BIDI_CLASSES
            or ch not in SUPPORTED_CHARS
        ):
            return FieldError(
                field,
                FieldCode.UNSUPPORTED_CHARACTERS,
                f"Character U+{ord(ch):04X} is not supported by the certificate font.",
            )
    return None


def _check_text(field: str, value: str, max_length: int, block: Block) -> FieldError | None:
    """Shared rule chain for name, title and issuer; `value` is already normalized."""
    if not value:
        return FieldError(field, FieldCode.REQUIRED, f"{field} is required.")
    if len(value) > max_length:
        return FieldError(field, FieldCode.TOO_LONG, f"{field} must be at most {max_length} characters.")
    charset = _charset_error(field, value)
    if charset is not None:
        return charset
    if fit_text(value, block) is None:
        return FieldError(field, FieldCode.DOES_NOT_FIT, f"{field} does not fit on the certificate.")
    return None


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

    @property
    def all_errors(self) -> list[FieldError]:
        """§24.11 order: request-level first, then rows ascending (each row's errors already ordered)."""
        return [*self.request_errors, *self.row_errors]


def validate_request(
    body: CreateJobRequest, *, today: date, print_email: bool, max_recipients: int
) -> ValidatedRequest:
    title = norm(body.certificate.title)
    issuer = norm(body.certificate.issuer)
    request_errors = [
        error
        for error in (
            _check_text("certificate.title", title, MAX_CERTIFICATE_TEXT_LENGTH, TITLE),
            _check_text("certificate.issuer", issuer, MAX_CERTIFICATE_TEXT_LENGTH, ISSUER),
            _check_issued_on(body.certificate.issued_on, today),
        )
        if error is not None
    ]
    rows: tuple[ValidatedRow, ...] = ()
    if not body.recipients:
        request_errors.append(FieldError("recipients", FieldCode.REQUIRED, "recipients must not be empty."))
    elif len(body.recipients) > max_recipients:
        # Rows are not validated: the request must be split anyway, and this bounds the work done.
        request_errors.append(
            FieldError(
                "recipients",
                FieldCode.TOO_MANY_RECIPIENTS,
                f"At most {max_recipients} recipients are allowed per request; got {len(body.recipients)}.",
            )
        )
    else:
        rows = _validate_rows(body.recipients, print_email)
    return ValidatedRequest(
        title=title,
        issuer=issuer,
        issued_on=body.certificate.issued_on,
        print_email=print_email,
        rows=rows,
        request_errors=tuple(request_errors),
    )


def _check_issued_on(value: str, today: date) -> FieldError | None:
    field = "certificate.issued_on"
    try:
        if not ISO_DATE.fullmatch(value):
            raise ValueError
        issued = date.fromisoformat(value)
    except ValueError:
        return FieldError(field, FieldCode.INVALID_FORMAT, f"{field} must be a date in YYYY-MM-DD format.")
    # One day of slack: a client east of UTC may already be on tomorrow's date.
    if issued > today + timedelta(days=1):
        return FieldError(field, FieldCode.DATE_IN_FUTURE, f"{field} must not be in the future.")
    return None


def _validate_rows(raw_rows: list[dict[str, Any]], print_email: bool) -> tuple[ValidatedRow, ...]:
    rows = []
    seen_emails: dict[str, int] = {}
    for index, raw in enumerate(raw_rows):
        name, name_error = _check_name(raw)
        email, email_error = _check_email(raw, print_email)
        errors = [e for e in (name_error, email_error) if e is not None]
        errors += _unknown_field_errors(raw)
        # Duplicates are judged only among otherwise valid rows, so a broken row never "uses up" an email.
        if not errors and email is not None:
            key = email.lower()
            first = seen_emails.setdefault(key, index)
            if first != index:
                errors.append(
                    FieldError(
                        "email",
                        FieldCode.DUPLICATE_RECIPIENT,
                        f"Duplicate of row {first} (same email, case-insensitive).",
                        duplicate_of_row_index=first,
                    )
                )
        rows.append(
            ValidatedRow(
                row_index=index,
                name=name,
                email=email,
                errors=tuple(
                    FieldError(e.field, e.code, e.message, index, e.duplicate_of_row_index) for e in errors
                ),
                input_json=canonical_json(raw),
            )
        )
    return tuple(rows)


def _unknown_field_errors(raw: dict[str, Any]) -> list[FieldError]:
    """The first MAX_UNKNOWN_FIELD_ERRORS unknown keys (sorted), then one summary error for the rest.

    Bounded so that a row with millions of keys cannot turn into a response, a stored error list and
    a replay many times larger than the request (audit F-02).
    """
    unknown = [key for key in raw if key not in ROW_FIELDS]
    errors = [
        FieldError(key, FieldCode.UNKNOWN_FIELD, f"Unknown field {_quote(key)}.")
        for key in heapq.nsmallest(MAX_UNKNOWN_FIELD_ERRORS, unknown)
    ]
    hidden = len(unknown) - len(errors)
    if hidden:
        errors.append(FieldError(None, FieldCode.UNKNOWN_FIELD, f"{hidden} more unknown fields not listed."))
    return errors


def _quote(key: str) -> str:
    """repr() of a key for a message, shortened so a huge key is not copied into the message as well."""
    return repr(key) if len(key) <= MAX_QUOTED_KEY else repr(key[:MAX_QUOTED_KEY]) + "..."


def _check_name(raw: dict[str, Any]) -> tuple[str | None, FieldError | None]:
    value = raw.get("name")
    if value is None:
        return None, FieldError("name", FieldCode.REQUIRED, "name is required.")
    if not isinstance(value, str):
        return None, FieldError("name", FieldCode.INVALID_TYPE, "name must be a string.")
    name = norm(value)
    return name, _check_text("name", name, MAX_NAME_LENGTH, NAME)


def _check_email(raw: dict[str, Any], print_email: bool) -> tuple[str | None, FieldError | None]:
    value = raw.get("email")
    if value is None:
        return None, FieldError("email", FieldCode.REQUIRED, "email is required.")
    if not isinstance(value, str):
        return None, FieldError("email", FieldCode.INVALID_TYPE, "email must be a string.")
    email = value.strip(" ")
    if not email:
        return email, FieldError("email", FieldCode.REQUIRED, "email is required.")
    # Checked before email-validator, whose syntax pass is quadratic in the input length (audit F-01).
    if len(email) > MAX_EMAIL_LENGTH:
        message = f"The email address must be at most {MAX_EMAIL_LENGTH} characters."
        return email, FieldError("email", FieldCode.INVALID_EMAIL, message)
    # allow_smtputf8=False still admits internationalized domains; ASCII-only is checked explicitly
    # so every accepted address is renderable and comparable byte-for-byte.
    if not email.isascii():
        return email, FieldError("email", FieldCode.INVALID_EMAIL, "The email address must be ASCII only.")
    try:
        validate_email(email, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as exc:
        return email, FieldError("email", FieldCode.INVALID_EMAIL, str(exc))
    # Unreachable: 254 copies of the widest ASCII glyph ("@") fit at 8 pt (§10.3); kept as a guard.
    if print_email and fit_text(email, EMAIL) is None:
        return email, FieldError("email", FieldCode.DOES_NOT_FIT, "email does not fit on the certificate.")
    return email, None
