"""Job status derived from item counts on every read (§5.2). Never stored, so it cannot drift."""

from __future__ import annotations

from collections.abc import Mapping


def derive_job_status(counts: Mapping[str, int], attempted: int) -> tuple[str, float]:
    """Return (status, progress). `counts` maps item status -> count; `attempted` = items with attempts > 0.

    First matching row of the §5.2 table wins.
    """
    rejected = counts.get("rejected", 0)
    pending = counts.get("pending", 0)
    succeeded = counts.get("succeeded", 0)
    failed = counts.get("failed", 0)
    total = sum(counts.values())
    valid = total - rejected
    done = succeeded + failed

    if valid == 0:  # J1: unreachable through the API (a job needs >= 1 valid row); kept as a guard
        return "completed_with_errors", 1.0
    progress = round(done / valid, 4)
    if done == valid:
        if failed == 0 and rejected == 0:
            return "completed", progress  # J2
        if succeeded == 0:
            return "failed", progress  # J3
        return "completed_with_errors", progress  # J4
    # J5 needs attempted == 0 so an item waiting out its retry delay never makes the job look new again.
    if pending == valid and attempted == 0:
        return "queued", progress
    return "processing", progress  # J6
