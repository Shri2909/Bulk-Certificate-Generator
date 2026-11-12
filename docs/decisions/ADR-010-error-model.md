# ADR-010: Error model — RFC 9457 problem+json with stable machine codes

Status: Accepted · Requirements: R2, R10

## Options
1. **FastAPI defaults** (simplest). `{"detail": ...}` and Pydantic's 422 list. Inconsistent between our errors and framework errors, and no stable codes.
2. **Custom envelope** `{"error": {"code", "message"}}`. Consistent, but non-standard.
3. **RFC 9457 problem details + `code` + `errors[]`** (chosen). Standard media type, and the RFC's own example uses an `errors` extension.

## Decision
Option 3. Every non-2xx response is `application/problem+json`:
```json
{ "type": "about:blank", "title": "Validation failed", "status": 422,
  "code": "VALIDATION_FAILED", "detail": "2 of 2 recipients are invalid; no job was created.",
  "errors": [ {"row_index": 1, "field": "email", "code": "INVALID_EMAIL", "message": "..."} ] }
```
Pydantic `RequestValidationError` and Starlette's own HTTP errors (404, 405) are mapped into the same shape (`field` = dotted path, `row_index` when under `recipients[i]`). **One error shape everywhere:** a flat list of `{row_index?, field, code, message, ...extras}`. It is used by 422, the `202` body, the dry-run report, and (without `row_index`, since the item carries it) `items[].errors`. Every 503 carries `Retry-After: 1`, and every response is `Cache-Control: no-store`.

**Request-level codes** (`ProblemCode` in `certgen/errors.py`; the status of each is in `PROBLEMS`):

| Code | HTTP | When |
|---|---|---|
| `INVALID_HOST` | 400 | the `Host` header is not `localhost`, `127.0.0.1`, `[::1]` or a name in `CERTGEN_ALLOWED_HOSTS` |
| `UNAUTHORIZED` | 401 | a `/v1` request without a valid `Authorization: Bearer <token>` (`WWW-Authenticate: Bearer`) |
| `NOT_FOUND` | 404 | unknown or malformed job/certificate id, unknown path |
| `METHOD_NOT_ALLOWED` | 405 | wrong HTTP method |
| `REQUEST_TIMEOUT` | 408 | the request body did not arrive completely within 30 s |
| `CERTIFICATE_NOT_READY` | 409 | certificate still pending or processing |
| `CERTIFICATE_GENERATION_FAILED` | 409 | certificate failed after all attempts |
| `IDEMPOTENCY_KEY_REUSED` | 409 | the `Idempotency-Key` was already used with a different body |
| `NOTHING_TO_RETRY` | 409 | `POST .../retry` on a job with no failed certificates |
| `CERTIFICATE_FILE_MISSING` | 410 | item succeeded but its file is gone from disk or damaged |
| `PAYLOAD_TOO_LARGE` | 413 | body over the derived limit (ADR-011) |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | `Content-Type` is not `application/json` (optionally `; charset=utf-8`) |
| `VALIDATION_FAILED` | 422 | request-level errors, every row invalid, or invalid query parameters |
| `INTERNAL_ERROR` | 500 | unexpected error (no stack trace or exception text in the body; details only in the log) |
| `DB_UNAVAILABLE` | 503 | database lock wait over 5 s, or the database cannot be read |
| `ARCHIVE_BUSY` | 503 | two archives are already being built in this process |
| `CAPACITY_EXCEEDED` | 503 | accepting the job would exceed `CERTGEN_MAX_ACTIVE_ITEMS` |
| `SERVER_BUSY` | 503 | 64 requests, `CERTGEN_MAX_REQUESTS_PER_TOKEN` requests of this token, or `CERTGEN_MAX_CONCURRENT_SUBMISSIONS` submissions already in progress in this process |
| `STORAGE_FULL` | 503 | less than `CERTGEN_MIN_FREE_DISK_MB` of disk is free for new jobs or retries |

**Row-level codes** (`FieldCode`; in the flat `errors[]` of 422 / 202 / dry-run bodies, and in `items[].errors[].code`). The validation codes (`REQUIRED`, `INVALID_TYPE`, `TOO_LONG`, `INVALID_CHARACTERS`, `UNSUPPORTED_CHARACTERS`, `DOES_NOT_FIT`, `INVALID_EMAIL`, `UNKNOWN_FIELD`, `DUPLICATE_RECIPIENT`) are defined in ADR-005. The rest:

| Code | Meaning |
|---|---|
| `INVALID_JSON` | body is not strict JSON (syntax, invalid UTF-8, duplicate keys, `NaN`/`Infinity`, depth over 8, unpaired surrogate escape) |
| `INVALID_VALUE` | any other shape error, including bad query parameters |
| `INVALID_FORMAT` | `issued_on` is not a real `YYYY-MM-DD` date |
| `TOO_MANY_RECIPIENTS` | more than `CERTGEN_MAX_RECIPIENTS` rows |
| `DATE_IN_FUTURE` | `issued_on` more than one day after today (UTC) |
| `GENERATION_FAILED` | the renderer or the file write raised on every attempt (`CERTGEN_MAX_ATTEMPTS`) |
| `WORKER_LOST` | the worker died (lease expired) on every attempt |

Codes are `StrEnum`s in `certgen/errors.py`, the single source. `StrEnum` needs Python 3.11 or newer, which is why `requires-python = ">=3.11"`. Tests assert codes, never English messages.

Clients never see exception text or paths: a failed generation is stored as the exception class plus a fixed sentence, and database errors are logged without SQL parameters.

## Consequences
Clients branch on `code`. Messages may change freely.

## At 100× scale
Unchanged. Add `instance` = request id for log correlation.
