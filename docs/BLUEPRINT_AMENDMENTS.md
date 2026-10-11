# Blueprint amendments

`docs/BLUEPRINT.md` is frozen (`docs/BLUEPRINT.sha256`, tag `blueprint-locked`). Every deviation is recorded here.
"auto-approved" = pre-approved by the Phase 5 instructions (contradiction resolved by the authority order PDF > §24 > §§0–23, simplest gap reading, same-package pin fallback, benchmark-rule constant, or an added test case for a named behavior).

| # | Date | Sections | What | Why | Impact | Status |
|---|---|---|---|---|---|---|
| A-01 | 2026-10-11 | §14 (tree) vs §15, §24.12 | Add `certgen/__main__.py` (3 lines: `from certgen.main import serve; serve()`). | §15 and §24.12 require `python -m certgen`; Python needs `certgen/__main__.py` for that, but §14 omits it. §24 wins over §14. | One file; no new behavior beyond what §15/§24.12 specify. | auto-approved (contradiction, authority order) |
| A-02 | 2026-10-11 | §16 `test_worker.py` | Add `test_worker.py::test_job_snapshot_ignores_worker_settings` (R5). | §4/§10.4 require the job's `print_email` snapshot to drive rendering, with no named test; mutation proof 9 needs it. | Test only. | auto-approved (test case for a named behavior) |
| A-03 | 2026-10-11 | §6.1, §24.17, §18 (D3/D4 file lists) | Re-add `ProblemCode.NOTHING_TO_RETRY` (D3) and, in M10, `IDEMPOTENCY_KEY_REUSED` (D4) to `certgen/errors.py`. | §6.1 lists both codes as part of the error contract; §24.17 (higher) required their removal before `v1.0-required`; §18's D3/D4 file lists omit `errors.py`, the only place codes live. | One enum value + status mapping per D; no new behavior beyond §6.1. | auto-approved (contradiction, authority order) |
