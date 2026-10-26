from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, cast


ARTIFACT_SCHEMA_VERSION = "2"


@dataclass(frozen=True, slots=True)
class RuntimeCounters:
    bars_processed: int
    events: int
    skip_ranges: int
    equity_points: int
    retained_equity_points: int
    diagnostics: int
    trades: int
    rss_bytes: int
    peak_rss_bytes: int
    retained_events: int = 0
    retained_base_candles: int = 0
    retained_timeframe_candles: int = 0
    journal_index_entries: int = 0

    def to_dict(self) -> dict[str, int]:
        return cast(dict[str, int], asdict(self))


class ResourceLimitExceeded(RuntimeError):
    """Base class for fail-closed resource-limit termination."""

    def __init__(
        self,
        message: str,
        *,
        counters: RuntimeCounters | None = None,
    ) -> None:
        super().__init__(message)
        self.counters = counters

    def attach_counters(self, counters: RuntimeCounters) -> None:
        if self.counters is None:
            self.counters = counters


class MemoryLimitExceeded(ResourceLimitExceeded):
    """Raised before the process enters destructive swap pressure."""


class DurationLimitExceeded(ResourceLimitExceeded):
    """Raised when a cooperative wall-clock execution deadline expires."""


class ObservabilityLimitExceeded(ResourceLimitExceeded):
    """Raised when a configured bounded-observability limit is exceeded."""


@dataclass(frozen=True, slots=True)
class PerformanceConfig:
    mode: str = "canonical_audit"
    max_memory_mb: int | None = 9216
    max_events: int | None = 2_000_000
    max_skip_ranges: int | None = 250_000
    max_diagnostics: int | None = 250_000
    max_equity_points: int | None = 5_000_000
    progress_interval_bars: int = 10_000
    retained_event_window: int = 2_000
    equity_sampling: str | None = None
    equity_every_n_bars: int = 12
    max_base_history: int | None = 65_536
    max_timeframe_history: int | None = 4_096
    disable_cyclic_gc_in_hot_loop: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"canonical_audit", "research_fast"}:
            raise ValueError("performance.mode must be canonical_audit or research_fast")
        for name in (
            "max_memory_mb",
            "max_events",
            "max_skip_ranges",
            "max_diagnostics",
            "max_equity_points",
            "max_base_history",
            "max_timeframe_history",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"performance.{name} must be a positive integer or null")
        for name in (
            "progress_interval_bars",
            "retained_event_window",
            "equity_every_n_bars",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"performance.{name} must be a positive integer")
        if not isinstance(self.disable_cyclic_gc_in_hot_loop, bool):
            raise TypeError("performance.disable_cyclic_gc_in_hot_loop must be bool")
        policy = self.equity_sampling or (
            "every_bar" if self.mode == "canonical_audit" else "hybrid"
        )
        if policy not in {
            "every_bar",
            "every_n_bars",
            "hourly",
            "daily",
            "on_change",
            "on_trade",
            "hybrid",
        }:
            raise ValueError("unsupported equity sampling policy")
        object.__setattr__(self, "equity_sampling", policy)

    def execution_deadline_scope(self, deadline_at: float | None) -> Any:
        """Bind a caller-owned absolute deadline without changing run semantics."""

        from .deadline import execution_deadline

        return execution_deadline(deadline_at)
