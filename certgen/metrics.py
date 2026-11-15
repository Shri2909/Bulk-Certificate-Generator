"""Prometheus metrics: what an operator needs to see queue health without reading the database.

Two kinds, with different homes:

* Queue gauges (`certgen_queue_depth`, `certgen_items{status}`) are read from the database at scrape
  time, so they are exact, survive restarts and need no coordination between processes.
* Worker-side measurements (`certgen_render_seconds`, `certgen_lease_lost_total`) live in the
  process that did the work. Embedded workers share the API's registry; each standalone worker
  process serves its own (CERTGEN_WORKER_METRICS_PORT), the usual one-target-per-process model.

Every Metrics object owns a private registry, so tests and several apps in one process never share
counters. Nothing here carries names, emails or ids: only counts and durations.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import text

from certgen.db import Engines

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
ITEM_STATUSES = ("rejected", "pending", "processing", "succeeded", "failed")

# One covering-index scan of items(status, id); it is the only query a scrape runs.
ITEM_COUNTS_SQL = "SELECT status, COUNT(*) FROM items GROUP BY status"

# Renders take about 4 ms on the benchmark machine; the buckets span 1 ms to the 30 s lease.
RENDER_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 30.0)


class QueueCollector(Collector):
    """Reads item counts from the database each time the registry is collected."""

    def __init__(self, engines: Engines) -> None:
        self.engines = engines

    def collect(self) -> Iterable[Metric]:
        with self.engines.read.begin() as conn:
            counts = {str(status): int(n) for status, n in conn.execute(text(ITEM_COUNTS_SQL))}
        depth = GaugeMetricFamily(
            "certgen_queue_depth",
            "Items waiting to be claimed (pending, including those waiting out a retry delay).",
        )
        depth.add_metric([], counts.get("pending", 0))
        by_status = GaugeMetricFamily("certgen_items", "Items by status across all jobs.", labels=["status"])
        for status in ITEM_STATUSES:  # every status is always present, so a zero is a zero, not a gap
            by_status.add_metric([status], counts.get(status, 0))
        return (depth, by_status)


class Metrics:
    def __init__(self, engines: Engines | None = None) -> None:
        self.registry = CollectorRegistry()
        self.render_seconds = Histogram(
            "certgen_render_seconds",
            "Wall time of one certificate render attempt, successful or not.",
            buckets=RENDER_BUCKETS,
            registry=self.registry,
        )
        self.lease_lost = Counter(
            "certgen_lease_lost",
            "Outcome writes rejected by the fencing token: the worker no longer held the lease.",
            registry=self.registry,
        )
        if engines is not None:
            self.registry.register(QueueCollector(engines))

    def render(self) -> bytes:
        return generate_latest(self.registry)
