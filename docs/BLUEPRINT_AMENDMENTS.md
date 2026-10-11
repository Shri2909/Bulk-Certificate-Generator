# Blueprint amendments

`docs/BLUEPRINT.md` is frozen (`docs/BLUEPRINT.sha256`, tag `blueprint-locked`). Every deviation is recorded here.
"auto-approved" = pre-approved by the Phase 5 instructions (contradiction resolved by the authority order PDF > §24 > §§0–23, simplest gap reading, same-package pin fallback, benchmark-rule constant, or an added test case for a named behavior).

| # | Date | Sections | What | Why | Impact | Status |
|---|---|---|---|---|---|---|
| A-01 | 2026-10-11 | §14 (tree) vs §15, §24.12 | Add `certgen/__main__.py` (3 lines: `from certgen.main import serve; serve()`). | §15 and §24.12 require `python -m certgen`; Python needs `certgen/__main__.py` for that, but §14 omits it. §24 wins over §14. | One file; no new behavior beyond what §15/§24.12 specify. | auto-approved (contradiction, authority order) |
| A-02 | 2026-10-11 | §16 `test_worker.py` | Add `test_worker.py::test_job_snapshot_ignores_worker_settings` (R5). | §4/§10.4 require the job's `print_email` snapshot to drive rendering, with no named test; mutation proof 9 needs it. | Test only. | auto-approved (test case for a named behavior) |
