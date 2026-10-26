from __future__ import annotations

import os
import resource
import sys

from .config import MemoryLimitExceeded, RuntimeCounters
from .deadline import check_execution_deadline


def rss_bytes() -> int:
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # pragma: no cover - optional dependency/platform fallback
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


class MemoryGuard:
    def __init__(self, max_memory_mb: int | None) -> None:
        self.limit_bytes = None if max_memory_mb is None else max_memory_mb * 1024 * 1024

    def check(self, counters: RuntimeCounters) -> None:
        # Keep the cooperative wall-clock guard on the same bounded hot-loop cadence
        # as memory checks. This guarantees a controlled ResourceLimitExceeded path
        # without adding per-bar clock calls to canonical execution.
        check_execution_deadline(counters)
        if self.limit_bytes is None:
            return
        # Enforce against the current resident set. ``ru_maxrss`` is a process-
        # lifetime high-water mark and can include unrelated work completed before
        # this backtest. Using it as the trigger makes a later bounded run fail even
        # after memory has been released. Keep peak RSS in diagnostics, but do not
        # use the historical high-water mark as the fail-fast decision.
        observed = counters.rss_bytes
        if observed > self.limit_bytes:
            raise MemoryLimitExceeded(
                "backtest memory guard exceeded: "
                f"observed={observed} limit={self.limit_bytes} "
                f"bars={counters.bars_processed} events={counters.events} "
                f"skip_ranges={counters.skip_ranges} equity={counters.equity_points} "
                f"retained_events={counters.retained_events} "
                f"retained_base_candles={counters.retained_base_candles} "
                f"retained_timeframe_candles={counters.retained_timeframe_candles} "
                f"journal_index_entries={counters.journal_index_entries}",
                counters=counters,
            )
