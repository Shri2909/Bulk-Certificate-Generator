# ADR-014: Metrics — Prometheus text format, database-read gauges, per-process worker timings

Status: Accepted · Requirements: R6, R11 (an operability extension; the assignment does not ask for metrics)

## Options
1. **No metrics; read the API and the logs.** Nothing to build, but queue depth and lease losses are invisible until a job stalls.
2. **`prometheus_client` multiprocess mode** (one shared directory of memory-mapped files). Aggregates every process, but the directory must be fixed by an environment variable *before* the library is imported, which makes several apps in one process (the test suite) share counters, and stale files from dead workers need cleanup.
3. **Gauges read from the database at scrape time; worker timings kept in each process and exposed per process** (chosen).
4. **Persist counters and durations in SQLite.** Survives restarts, but adds a table and a write to the hot path of every item, on the single writer ADR-002 already identifies as the bottleneck.

## Decision
**Option 3.** `certgen/metrics.py` owns one `CollectorRegistry` per `Metrics` object.
- `certgen_queue_depth` and `certgen_items{status}` are computed from `SELECT status, COUNT(*) FROM items GROUP BY status` each time `/metrics` is scraped. They are exact, survive restarts and need no coordination between processes. Every status is always reported, so a zero is a zero and not a gap. The query is an index-only scan (`test_scrape_query_reads_a_covering_index`).
- `certgen_render_seconds` (histogram, 1 ms to 30 s) times each render attempt, successful or not, with `time.perf_counter` and not the injected clock. `certgen_lease_lost_total` counts outcome writes the fencing token rejected (ADR-004). Embedded workers share the API process's registry; a standalone `python -m certgen.worker` serves its own when `CERTGEN_WORKER_METRICS_PORT` is set (off by default; bound to `CERTGEN_HOST`). Prometheus scrapes each process as its own target, the usual model.
- `GET /metrics` needs the same bearer token as `/v1`. It is not open like `/healthz`: counts of work in flight are operational information, and Prometheus supports bearer tokens.
- Nothing carries names, emails or ids (`test_metrics_carry_no_recipient_data`).

## Consequences
- Worker-side numbers reset when a process restarts, as Prometheus counters normally do; `rate()` handles that. The gauges do not reset because they come from the database.
- A scrape scans the status index of every item ever stored. Items are never deleted (no retention), so scrape cost grows with total items; keep the scrape interval at 15 s or more on large data directories.
- Queue depth counts `pending` items, including those waiting out a retry delay. An item whose lease expired is still `processing` until a worker reclaims it, so with zero workers running, depth stays flat while `processing` does not drain.
- No tracing, no alert rules and no dashboard are provided.
