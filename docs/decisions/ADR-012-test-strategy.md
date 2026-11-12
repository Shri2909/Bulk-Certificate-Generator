# ADR-012: Test strategy — deterministic, no sleeps, requirement-tagged

Status: Accepted · Requirements: R14–R19 (and every R via traceability)

## Options
1. **API tests only, real background thread, poll with sleep** (simplest to write). Flaky under CI load, and slow.
2. **Unit tests only with mocks**. Fast, but cannot prove the HTTP contract or the crash story.
3. **Layered, deterministic** (chosen).

## Decision
- **Isolation:** each test gets a fresh temporary directory holding its own SQLite file and certificate directory, and a fresh app from `create_app(settings)`. There is no global state.
- **Determinism:** the embedded worker is disabled in tests. A `drain` fixture runs `Worker.run_until_idle()` synchronously in the test thread. The clock is injectable (`Clock`, epoch milliseconds), so lease expiry and retry delays are tested by advancing a fake clock, not by sleeping.
- **Fault injection:** the renderer is a dependency of the worker (`Renderer = Callable[[CertificateData], bytes]`). Tests pass a renderer that raises for one named recipient (R18) or fails N times and then succeeds (retry).
- **Real-process tests (marked `slow`, still in the default run):** an injectable clock cannot reach a subprocess, so these tests shrink real time instead, through environment variables: a short `CERTGEN_LEASE_SECONDS` and `CERTGEN_TEST_SLOW_RENDER_MS` (a test-only render delay, rejected if it is not shorter than the lease). The kill is **state-triggered**: the test polls the database until one item has succeeded and one is processing, then sends SIGKILL, which makes it independent of machine speed. A second worker then finishes, and the test asserts completion with no duplicates. Skipped on Windows (no SIGKILL), with the reason reported.
- **HTTP contract and hardening:** tests drive the app through `TestClient` and, where the behaviour lives in the server (slow bodies, request slots, Host header, token file), through real ASGI scopes or a real server process.
- **Requirement tags:** every test carries `@pytest.mark.req(...)`. `tests/test_spec_traceability.py` enforces that **every requirement is cited by at least one test** and that no test cites an unknown id. It is a static scan: it does not prove the test asserts the right thing or is not skipped.
- **PDF content assertions:** `pypdf` (a test-only dependency) extracts text, so R5 is proven, not assumed.
- **Docs tests:** `tests/test_docs.py` asserts that the README has the sections the assignment requires (R20–R25), that every relative link in the README, ARCHITECTURE.md and METHODOLOGY.md resolves, that the README curl example is accepted by the API, that its benchmark figures match the evidence files in `tests/fixtures/benchmarks/`. Whether the README **commands actually work** is checked by running them verbatim in a clean virtual environment, not by pytest.
- **Static checks:** `ruff check`, `ruff format --check` and `mypy --strict certgen` (see the README's "Tests and benchmarks").

## Consequences
Tests exercise the same `run_until_idle` / `process` code that production threads loop over. The thread wrapper is small and covered by smoke and runtime tests that start the real app with the embedded worker and poll with bounded timeouts.

## At 100× scale
Add a PostgreSQL CI matrix leg before enabling PostgreSQL. Add a load test against a deployed stack.
