# ADR-012: Test strategy — deterministic, no sleeps, requirement-tagged

Status: Accepted · Requirements: R14–R19 (and every R via traceability) · Evidence: RESEARCH §4, §7

## Options
1. **API tests only, real background thread, poll with sleep** (simplest to write). Flaky under CI load, and slow.
2. **Unit tests only with mocks**. Fast, but can't prove the HTTP contract or the crash story.
3. **Layered, deterministic** (chosen).

## Decision
- **Isolation:** each test gets a fresh temp dir holding its own SQLite file and storage dir (`tmp_path` fixture), and a fresh app via `create_app(settings)`. There is no global state.
- **Determinism:** the embedded worker is disabled in tests. `worker.run_until_idle(session_factory, renderer)` drains the queue synchronously in the test thread. The clock is injectable (`now: Callable[[], datetime]`), so lease expiry and backoff are tested by advancing time, not sleeping.
- **Fault injection:** the renderer is a dependency of the worker (`Renderer = Callable[[CertificateData], bytes]`). Tests pass a renderer that raises for one named recipient (R18) or fails N times then succeeds (retry).
- **Real-process tests (marked `slow`, still in the default run):** the injectable clock can't reach a subprocess, so these tests shrink real time instead, via env: `LEASE_SECONDS=2`, `CERTGEN_TEST_SLOW_RENDER_MS=300` (a test-only renderer delay, ignored unless set). The kill is **state-triggered**: the test polls the DB until ≥ 1 item succeeded and 1 is processing, then SIGKILLs. That makes it independent of machine speed (audit #9). A second worker then finishes, and the test asserts completion with no duplicates. Expected ≈ 5 s. Skipped on Windows (no SIGKILL), with the reason printed.
- **Requirement tags:** every test carries `@pytest.mark.req(...)`. `test_spec_traceability.py` enforces that **every requirement is cited by at least one test**. It's a static scan: it does not prove the test asserts the right thing or isn't skipped, and the README says exactly that (audit #25).
- **PDF content assertions:** `pypdf` (test-only dependency) extracts text, so R5 is proven, not assumed.
- **Docs tests:** `test_docs.py` asserts that the README has the 6 required sections (R20–R25) and that every ADR linked from the README exists. Whether the README **commands actually work** is checked by running them verbatim in a clean venv in Phase 6 (manual, with transcript), not by pytest.
- **Static checks:** `ruff check` + `mypy --strict certgen/` in the README's "Run tests" section. Phase 6 runs them.

## Consequences
Tests exercise the same `run_until_idle`/`process_item` code that production threads loop over. The thread wrapper is ~10 lines, covered by one smoke test that starts the real app with the embedded worker and polls with a 10 s timeout.

## At 100× scale
Add a Postgres CI matrix leg before enabling Postgres. Add a load test (locust) against a deployed stack.
