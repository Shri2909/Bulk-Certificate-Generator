# ADR-001: Web framework — FastAPI

Status: Proposed · Requirements: R1, R2, R13 · Evidence: RESEARCH §4, §3

## Context
The PDF allows FastAPI, Django+DRF, or Flask. The API is small: about 7 endpoints and JSON in/out. The hard part is bulk validation and background processing, not CRUD.

## Options
| | Flask (simplest) | Django + DRF | FastAPI |
|---|---|---|---|
| Body validation | None built-in; add marshmallow/pydantic | DRF serializers | Pydantic, declarative, emits 422 |
| Background work | Docs recommend Celery + Redis | Celery typical; `select_for_update` no-op on SQLite | `lifespan` hook for worker threads; docs say Celery for heavy work |
| OpenAPI docs | Add-on | Add-on (drf-spectacular) | Built in at `/docs` |
| Files/boilerplate | Few | Many (settings, apps, migrations) | Few |
| Testing | test_client | APITestCase | TestClient (HTTPX) |

## Criteria
Correctness (validation strictness), failure isolation (not framework-driven), ease to run/explain (fewest concepts), testability.

## Decision
**FastAPI**. Pydantic gives per-field structural validation with types, and that code also serves as the API contract. `/docs` gives reviewers an interactive console at zero cost. `lifespan` is a documented place to start and stop the embedded worker. Django's main advantages (ORM migrations, admin) don't help here, and its row-locking abstraction is a no-op on SQLite ([Django docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/)).

## Consequences
- Pydantic's default 422 body shape is overridden by an exception handler so all errors use one model (ADR-010).
- Endpoints are sync `def`, not `async`. All work is blocking (SQLite, ReportLab), and FastAPI runs sync endpoints in a threadpool. That avoids "async function doing blocking I/O" bugs.

## At 100× scale
The framework is not the bottleneck. API pods stay stateless; workers move out of process (ADR-003/004).
