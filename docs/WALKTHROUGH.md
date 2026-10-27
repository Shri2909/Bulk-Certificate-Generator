# Walkthrough — certgen

For the interview: how the system works, where each step lives, the questions you are likely to get, and
exactly what to change for typical new requirements. Section numbers (§) refer to
[BLUEPRINT.md](BLUEPRINT.md).

## 1. Two-minute pitch

A client posts one JSON request with shared certificate info and up to 10,000 recipients. The API parses the
body strictly, validates every row, and in **one transaction** stores a `jobs` row plus one `items` row per
recipient: valid rows `pending` with a random `certificate_id`, invalid rows `rejected` with their errors.
It answers `202` right away. Nothing is rendered inside the request.

The `items` table **is the queue**. Workers, either threads inside the API process or separate
`python -m certgen.worker` processes, claim one item at a time in a single `BEGIN IMMEDIATE` transaction.
That transaction takes SQLite's write lock first, so two workers can never take the same item. A claim
sets a 30-second lease and a fresh random token. The worker renders the PDF with ReportLab outside any
transaction, writes it atomically (temp file, fsync, rename), and marks the item `succeeded` with
`UPDATE … WHERE lease_token = ?`. If a worker dies, its lease expires and another worker takes the item
over with a new token; the dead worker's late write then matches nothing. A failing item is retried twice
after 2 s, then it is `failed`; the others are unaffected.

Job status is never stored: it is derived from item counts on every read. Clients poll the job, page
through items (`?status=failed`), download single PDFs, or one ZIP with a `manifest.csv`. Everything runs
with `pip install` and one command: SQLite in WAL mode, files on local disk, no broker.

## 2. Request lifecycle, traced to code

| Step | What happens | Where |
|---|---|---|
| 1 | Body over the derived limit → 413 before parsing (Content-Length or counted while buffering) | `certgen/middleware.py:BodySizeLimitMiddleware.__call__` |
| 2 | Media type must be `application/json` → else 415; raw body read | `certgen/api.py:read_json_body`, `is_json_media_type` |
| 3 | Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity/overflow, depth ≤ 8 | `certgen/validation.py:decode_json_body` |
| 4 | (D4) `Idempotency-Key` checked; same key + same body hash → 200 replay | `certgen/api.py:idempotency_key`, `certgen/service.py:find_replay` |
| 5 | Envelope shape (Pydantic, `extra="forbid"`) → structural 422 | `certgen/models.py:CreateJobRequest`, `certgen/errors.py:body_errors_from_pydantic` |
| 6 | Field rules: NFC + space collapse, charset C1/C2, length, `fit_text`, email, duplicates | `certgen/validation.py:validate_request`, `_validate_rows`, `_check_text` |
| 7 | Request errors or all rows invalid → 422; (D3) `dry_run=true` → 200 report | `certgen/api.py:create_job` |
| 8 | One write transaction: capacity check, insert job (ID-collision savepoint retry), bulk insert items | `certgen/service.py:create_job`, `_create_job`, `_insert_job`, `_insert_items` |
| 9 | 202 + `Location` + flat row errors | `certgen/api.py:create_job`, `job_resource` |
| 10 | Worker loop: sweep old `*.tmp`, then claim / process / back off | `certgen/worker.py:Worker.run_forever`, `run_once` |
| 11 | Claim: retire exhausted leases (T8), take an expired lease (T4) or the oldest pending (T3) | `certgen/worker.py:Worker.claim_next`, `retire_exhausted` |
| 12 | Load render inputs from the DB rows only (job snapshot: `print_email`) | `certgen/worker.py:Worker.load_certificate_data` |
| 13 | Render the one template; text fitted by the same function validation used | `certgen/rendering.py:render_certificate`, `certgen/layout.py:fit_text` |
| 14 | Atomic write under a 32-hex-only path | `certgen/storage.py:Storage.save_atomic` |
| 15 | Fenced outcome: T5 success, T6 retry later, T7 failed | `certgen/worker.py:Worker.record_success`, `record_failure` |
| 16 | `GET /jobs/{id}`: counts in one read transaction → derived status/progress | `certgen/service.py:get_job`, `certgen/status.py:derive_job_status` |
| 17 | Items page (keyset on `row_index`), single download (409/410 states), ZIP + manifest | `certgen/api.py:list_items`, `download_certificate`, `download_archive`; `certgen/archive.py:build_archive` |
| 18 | (D3) `POST …/retry`: failed → pending in one transaction (T9) | `certgen/service.py:retry_failed` |
| 19 | Embedded mode: worker threads + a monitor that restarts dead threads and retires exhausted leases | `certgen/worker.py:WorkerPool`, wired in `certgen/main.py:create_app` lifespan |

## 3. Claim, lease, fence — in plain words

- **Claim.** A worker asks the database "give me one item nobody is working on" and, in the same
  transaction, writes "I'm working on it". Because the transaction starts with `BEGIN IMMEDIATE`, SQLite
  hands out its single write lock before the question is even asked, so two workers' questions are answered
  one after the other, never at the same time. That is why no item is ever claimed twice
  (`test_two_concurrent_workers_never_double_process`, `test_many_threads_claim_each_item_exactly_once`).
- **Lease.** The claim is a loan with an expiry: `lease_expires_at = now + 30 s`. A normal render takes
  milliseconds. If the worker crashes, nobody has to notice: once the loan expires, the next claim by any
  worker may take the item back. After 3 attempts the item is retired as `failed` / `WORKER_LOST`, so a
  certificate that crashes every worker cannot loop forever.
- **Fence.** Every claim gets a new random `lease_token`. Finishing is `UPDATE … WHERE id = ? AND
  lease_token = ?`. A worker that was paused, lost its lease and wakes up still holds the old token, so its
  update changes 0 rows and it just logs `lease_lost`. The worker id is never used for this: a restarted
  process could reuse a name, but never a token (`test_stale_worker_done_write_rejected_by_fencing`).
- **Why a re-run is harmless.** Same item, same `certificate_id`, same path, same input rows, and ReportLab's
  invariant mode, so the bytes are identical. A re-run overwrites the file with the same content. The
  guarantee is "at-least-once execution, idempotent effect", not exactly-once (§7.3).

**The two-engine split.** SQLite allows one writer at a time. A transaction that starts reading and later
writes can fail with "database is locked" when it tries to upgrade. So there are two SQLAlchemy engines on
the same file: the **write engine** always starts with `BEGIN IMMEDIATE` (take the write lock first, wait up
to 5 s), and the **read engine** uses a plain `BEGIN`. In WAL mode readers never block the writer and the
writer never blocks readers, so polling a job can never stall a claim
(`test_open_read_does_not_block_claim`; mutation 2 in `evidence/mutation-checks.txt`). Code: `certgen/db.py`.

## 4. Twenty likely questions

1. **Why background processing and not synchronous?** 10,000 certificates take seconds to minutes (serial
   estimate in `evidence/benchmark-1p.json`); a synchronous POST would hit proxy timeouts, show no progress
   and lose everything on disconnect. §7.4, ADR-003.
2. **Why not Celery or FastAPI BackgroundTasks?** Celery needs a broker (breaks "pip install + one command");
   BackgroundTasks are in-memory and vanish on restart. The DB queue gives progress, durability and failure
   isolation for free. §7.4.
3. **How do you guarantee one certificate per valid recipient?** Items are created with the job in one
   transaction (`test_job_and_items_are_atomic`); claims are atomic; completion is token-fenced; the file path
   is derived from the item's fixed `certificate_id`. §5, §7.3.
4. **What happens if a worker is killed mid-render?** Lease expires, next claim reclaims (attempt 2), output
   identical. Proven with a real SIGKILL: `test_kill_mid_job_no_duplicates`, `evidence/kill-demo.txt`. §8.
5. **Why no heartbeat?** Work per item is milliseconds against a 30 s lease. A heartbeat adds a thread and
   failure modes for no benefit. The benchmark checks the slowest item stays under 10% of the lease. §7.3, §20.
6. **What's the downside of no heartbeat?** A stall longer than the lease (paused VM, stuck disk) causes a
   re-run and, after 3 such expiries, a false `WORKER_LOST`. Documented in README Limitations.
7. **Why derive job status instead of storing it?** A stored status can drift from the items; a derived one
   cannot. It costs one indexed `GROUP BY` per read. §5.2.
8. **How does "one failure doesn't block others" work?** Per-item `try/except` and per-item transactions;
   2 retries after 2 s, then `failed` with a fixed message. `test_one_failing_recipient_does_not_block_others`. §7.2.
9. **Why accept partially valid requests?** "Generate a certificate for each valid recipient": invalid rows
   are stored `rejected` with errors, valid rows are generated; 422 only when nothing can be generated. ADR-005, §9.
10. **Why reject CJK or Arabic names?** The bundled font has no CJK glyphs and ReportLab cannot shape RTL;
    rendering them would print a wrong certificate silently. Rejecting with a code is honest. §9 C2, §20.
11. **How do you know text fits?** One `fit_text` function is used by validation and by rendering; a row is
    accepted only if it will be drawn inside its box. §10.3.
12. **Why SQLite?** Zero services, transactional, WAL gives concurrent reads. It is the right size for one
    machine; §22 lists the PostgreSQL migration (`FOR UPDATE SKIP LOCKED`).
13. **How do you prevent path traversal?** Paths are built only from server-generated 32-hex ids, asserted
    in `storage.py`; symlinks are refused; downloads match ids with a regex first. §11.2, §24.8.
14. **Why is every route a plain `def`?** FastAPI runs sync routes in a threadpool, so CPU-heavy validation
    and SQLite waits never block the event loop. Only the middleware is async. §3.
15. **What does `Idempotency-Key` do?** Same key + same body → 200 replay of the current job; different body
    → 409; concurrent duplicates create one job because the key check repeats inside `BEGIN IMMEDIATE`. §13.
16. **How do you retry failed certificates?** `POST …/retry` moves every failed item back to `pending` in one
    transaction, keeping its `certificate_id`; capacity is checked like a new job. §6.8, §24.1.
17. **What limits protect the server?** 10k recipients, a derived body cap, field caps, 1000-item pages,
    2 concurrent archive builds, a global cap of 50k unfinished items, uvicorn concurrency 64. §12, §24.1.
18. **How are errors returned?** RFC 9457 problem+json with a stable `code`; 422 carries every row error with
    `row_index`, `field`, `code`, ordered deterministically. §6.1, §24.11.
19. **How do you know your tests actually test something?** Mutation proofs: each key guarantee was broken on
    purpose and its test failed (`evidence/mutation-checks.txt`); traceability guard links every requirement
    to a test. §16, §24.16.
20. **Why was the QR/verify feature dropped?** Its kill criterion was "no required test regresses";
    embedding the verify URL changes every worker PDF and breaks a required test that compares worker output
    with a direct render. The rules forbid editing required tests, so it stays on branch `dropped/d1-verify`. §18.

## 5. Changed-requirement scenarios

| Scenario | What to change | Files |
|---|---|---|
| **Email the certificate to each recipient** | Add a delivery step after T5: either a second queue (`deliveries` table with the same claim/lease/fence pattern) or a `delivery_status` on items claimed by a mailer worker. Keep rendering and sending separate so a mail outage never blocks generation. Add SMTP settings and an outbound rate limit. | `certgen/tables.py` (new table/columns + schema version 2 migration, §24.9), `certgen/worker.py` (mailer loop), `certgen/config.py` (SMTP settings), `certgen/service.py` + `certgen/api.py` (delivery status in items), tests: new `tests/test_delivery.py` |
| **Multiple templates** | Add `template_id` to the request and snapshot it on the job; a registry maps id → layout constants + drawing function; validation fits text with that template's blocks. | `certgen/models.py`, `certgen/validation.py` (block lookup), `certgen/layout.py` (blocks per template), `certgen/rendering.py` (registry, `CertificateData.template_id`), `certgen/tables.py` (`jobs.template_id`), `certgen/worker.py:load_certificate_data` |
| **100,000 recipients per job** | Raise `CERTGEN_MAX_RECIPIENTS`/`MAX_ACTIVE_ITEMS`; switch the body to CSV/NDJSON upload streamed to disk and validated by a worker (new `validating` job phase) so the POST stays fast; insert items in batches; build archives once in the background and serve them as files. For real throughput move to PostgreSQL with `SKIP LOCKED` and batch claims. | `certgen/config.py`, `certgen/api.py`, `certgen/service.py`, `certgen/validation.py` (callable from the worker), `certgen/archive.py`, `certgen/db.py` (§22) |
| **Webhooks when a job finishes** | Store `callback_url` on the job; when the last item reaches a terminal state, enqueue a webhook delivery (outbox table, retries with backoff, HMAC signature header). Detect "last item" inside the T5/T7/T8 transaction by re-counting. | `certgen/models.py`, `certgen/tables.py` (`jobs.callback_url`, `webhook_deliveries`), `certgen/worker.py` (outbox loop + terminal detection), `certgen/config.py` (signing secret) |
| **Per-job retention / deletion** | `DELETE /v1/certificate-jobs/{id}` (or `retention_days` on the job) plus a reaper: delete files first, then rows (`ON DELETE CASCADE` already links items to jobs); refuse while items are `processing`. | `certgen/api.py`, `certgen/service.py` (delete use case), `certgen/storage.py` (remove a job directory safely), `certgen/worker.py` or `certgen/main.py` (reaper thread), `certgen/tables.py` (`expires_at`) |
| **Multi-tenancy (per-client job isolation)** | Bearer tokens already exist (`certgen/auth.py`, `TokenAuthMiddleware`; Idempotency-Keys are stored as `<client id>:<key>`). Isolation adds a `client_id` column on `jobs` (schema version 2 migration), sets it from `request.state.client_id` on create, and filters every job/item/certificate query by it; per-client quotas replace the global capacity cap. | `certgen/tables.py`, `certgen/db.py` (migration), `certgen/service.py` (filters), `certgen/api.py`, tests |

## 6. Where to start reading

1. `certgen/worker.py` — `claim_next`, `process`, `record_success`/`record_failure`, `retire_exhausted`:
   the whole correctness story in ~150 lines.
2. `certgen/service.py:create_job` and `certgen/validation.py:validate_request` — how one request becomes a
   job and rows, and why invalid rows are kept rather than refused.
3. `certgen/db.py` — the two engines, the `BEGIN` modes, WAL, and schema versioning.
