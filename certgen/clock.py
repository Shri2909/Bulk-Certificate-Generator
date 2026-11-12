"""Time source, injected so tests can drive lease expiry with a fake clock."""

from __future__ import annotations

import time
from collections.abc import Callable

Clock = Callable[[], int]  # epoch milliseconds, UTC


def system_clock() -> int:
    return time.time_ns() // 1_000_000
