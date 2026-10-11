# ADR-010: Error model — RFC 9457 problem+json with stable machine codes

Status: Accepted · Requirements: R2, R10 · Evidence: RESEARCH §4

## Options
1. **FastAPI defaults** (simplest). `{"detail": ...}` and Pydantic's 422 list. Inconsistent between our errors and framework errors, and no stable codes.
2. **Custom envelope** `{"error": {"code", "message"}}`. Consistent, but non-standard.
3. **RFC 9457 problem details + `code` + `errors[]`** (chosen). Standard media type, and the RFC's own example uses an `errors` extension.

## Decision
Option 3. Every non-2xx response is `application/problem+json`:
```json
{ "type": "about:blank", "title": "Validation failed", "status": 422,
  "code": "VALIDATION_FAILED", "detail": "2 of 2 recipients are invalid",
  "errors": [ {"row_index": 1, "field": "email", "code": "INVALID_EMAIL", "message": "..."} ] }
```
Pydantic `RequestValidationError` is mapped into the same shape (`field` = dotted path, `row_index` when under `recipients[i]`). **One error shape everywhere:** a flat list of `{row_index?, field, code, message, ...extras}`. It's used by 422, the `202` body, the dry-run report, and (without `row_index`, since the item carries it) `items[].errors`.

**Request-level codes:** `VALIDATION_FAILED` (422), `NOT_FOUND` (404), `IDEMPOTENCY_KEY_REUSED` (409), `CERTIFICATE_NOT_READY` (409, pending/processing), `CERTIFICATE_GENERATION_FAILED` (409, failed; see `/retry`), `NOTHING_TO_RETRY` (409), `CERTIFICATE_FILE_MISSING` (410), `PAYLOAD_TOO_LARGE` (413), `INTERNAL_ERROR` (500, no stack trace in body).
**Row-level codes** (in the flat `errors[]` of 422 / 202 / dry-run bodies, and in `items[].errors[].code`): ADR-005 table, plus generation codes `GENERATION_FAILED` (renderer raised, after `MAX_ATTEMPTS`) and `WORKER_LOST` (lease expired at max attempts).

Codes are a `StrEnum` in `certgen/errors.py`, the single source. `StrEnum` needs Python ≥ 3.11, which is why `requires-python = ">=3.11"` (audit #5). Tests assert codes, never English messages.

## Consequences
Clients branch on `code`. Messages may change freely.

## At 100× scale
Unchanged. Add `instance` = request ID for log correlation.
