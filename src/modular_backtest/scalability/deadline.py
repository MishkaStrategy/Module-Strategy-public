from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .config import DurationLimitExceeded, RuntimeCounters


_ACTIVE_DEADLINE: ContextVar[float | None] = ContextVar(
    "modular_backtest_execution_deadline",
    default=None,
)


def deadline_from_now(seconds: int | float) -> float:
    """Return an absolute monotonic deadline for a positive bounded window."""

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("execution deadline seconds must be numeric")
    value = float(seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("execution deadline seconds must be finite and positive")
    return time.monotonic() + value


@contextmanager
def execution_deadline(deadline_at: float | None) -> Iterator[None]:
    """Scope one absolute monotonic deadline to the current execution context."""

    if deadline_at is not None:
        if isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)):
            raise TypeError("execution deadline must be numeric or null")
        deadline_at = float(deadline_at)
        if not math.isfinite(deadline_at) or deadline_at < 0:
            raise ValueError("execution deadline must be finite and non-negative")
    token = _ACTIVE_DEADLINE.set(deadline_at)
    try:
        yield
    finally:
        _ACTIVE_DEADLINE.reset(token)


def check_execution_deadline(counters: RuntimeCounters) -> None:
    """Fail closed when the active cooperative wall-clock deadline has expired."""

    deadline_at = _ACTIVE_DEADLINE.get()
    if deadline_at is None:
        return
    observed = time.monotonic()
    if observed < deadline_at:
        return
    raise DurationLimitExceeded(
        "backtest execution deadline exceeded: "
        f"observed_monotonic={observed:.6f} deadline_monotonic={deadline_at:.6f} "
        f"bars={counters.bars_processed} events={counters.events} "
        f"skip_ranges={counters.skip_ranges} equity={counters.equity_points}",
        counters=counters,
    )
