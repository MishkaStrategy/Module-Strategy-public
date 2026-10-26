from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_backtest.scalability.buffers import BoundedRecordBuffer
from modular_backtest.scalability.config import (
    PerformanceConfig,
    ResourceLimitExceeded,
    RuntimeCounters,
)
from modular_backtest.scalability.equity import CompactEquitySeries, EquitySampler
from modular_backtest.scalability.memory import MemoryGuard
from modular_core.models import EquityPoint


_START = datetime(2024, 1, 1, tzinfo=UTC)


def _point(
    *,
    timestamp: datetime = _START,
    equity: float = 100.0,
    position_open: bool = False,
) -> EquityPoint:
    return EquityPoint(
        timestamp=timestamp,
        cash=equity,
        unrealized_pnl=0.0,
        equity=equity,
        position_open=position_open,
    )


def _counters(*, rss_bytes: int = 0) -> RuntimeCounters:
    return RuntimeCounters(
        bars_processed=7,
        events=3,
        skip_ranges=1,
        equity_points=7,
        retained_equity_points=4,
        diagnostics=2,
        trades=1,
        rss_bytes=rss_bytes,
        peak_rss_bytes=rss_bytes,
        retained_events=2,
        retained_base_candles=3,
        retained_timeframe_candles=4,
        journal_index_entries=5,
    )


def test_bounded_record_buffer_rejects_invalid_capacity_and_covers_tail_helpers() -> None:
    with pytest.raises(TypeError, match="maxlen must be int"):
        BoundedRecordBuffer[int](True)
    with pytest.raises(TypeError, match="maxlen must be int"):
        BoundedRecordBuffer[int](1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        BoundedRecordBuffer[int](0)

    buffer = BoundedRecordBuffer[int](2)
    assert buffer.maxlen == 2
    buffer.append(1)
    buffer.append(2)
    assert buffer[-1] == 2
    assert buffer != object()
    buffer.clear()
    assert list(buffer) == []


def test_resource_limit_counters_attach_only_once() -> None:
    first = _counters(rss_bytes=1)
    second = _counters(rss_bytes=2)
    error = ResourceLimitExceeded("limit")
    error.attach_counters(first)
    error.attach_counters(second)
    assert error.counters is first
    assert first.to_dict()["journal_index_entries"] == 5


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "unsupported"}, "performance.mode"),
        ({"max_memory_mb": 0}, "max_memory_mb"),
        ({"max_events": True}, "max_events"),
        ({"max_skip_ranges": -1}, "max_skip_ranges"),
        ({"progress_interval_bars": 0}, "progress_interval_bars"),
        ({"retained_event_window": True}, "retained_event_window"),
        ({"equity_every_n_bars": -1}, "equity_every_n_bars"),
        ({"disable_cyclic_gc_in_hot_loop": 1}, "must be bool"),
        ({"equity_sampling": "unsupported"}, "unsupported equity sampling"),
    ],
)
def test_performance_config_rejects_fail_open_values(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        PerformanceConfig(**kwargs)  # type: ignore[arg-type]


def test_performance_config_selects_mode_defaults() -> None:
    assert PerformanceConfig(mode="canonical_audit").equity_sampling == "every_bar"
    assert PerformanceConfig(mode="research_fast").equity_sampling == "hybrid"
    assert PerformanceConfig(max_memory_mb=None, equity_sampling="daily").equity_sampling == "daily"


def test_compact_equity_series_rejects_bad_points_and_indices() -> None:
    series = CompactEquitySeries()
    with pytest.raises(TypeError, match="requires EquityPoint"):
        series.append(object())  # type: ignore[arg-type]

    first = _point()
    second = _point(timestamp=_START + timedelta(minutes=5), equity=101.0, position_open=True)
    series.append(first)
    series.append(second)

    assert series[-1] == second
    assert series[:] == [first, second]
    with pytest.raises(IndexError):
        _ = series[2]
    with pytest.raises(IndexError):
        _ = series[-3]

    replacement = _point(timestamp=_START + timedelta(minutes=10), equity=102.0)
    series[-1] = replacement
    assert series[1] == replacement
    with pytest.raises(TypeError, match="requires EquityPoint"):
        series[0] = object()  # type: ignore[assignment]
    with pytest.raises(IndexError):
        series[3] = replacement
    with pytest.raises(IndexError):
        series[-3] = replacement
    assert list(iter(series)) == [first, replacement]


def test_equity_sampler_covers_each_retention_policy() -> None:
    point = _point()
    later_hour = _point(timestamp=_START + timedelta(hours=1))
    next_day = _point(timestamp=_START + timedelta(days=1))

    every_bar = EquitySampler("every_bar", 2)
    assert every_bar.accept(point, bar_index=1, trade_count_changed=False)

    forced = EquitySampler("on_trade", 2)
    assert forced.accept(point, bar_index=1, trade_count_changed=False, force=True)

    every_n = EquitySampler("every_n_bars", 2)
    assert not every_n.accept(point, bar_index=1, trade_count_changed=False)
    assert every_n.accept(point, bar_index=2, trade_count_changed=False)

    hourly = EquitySampler("hourly", 2)
    assert hourly.accept(point, bar_index=0, trade_count_changed=False)
    assert not hourly.accept(point, bar_index=1, trade_count_changed=False)
    assert hourly.accept(later_hour, bar_index=2, trade_count_changed=False)

    daily = EquitySampler("daily", 2)
    assert daily.accept(point, bar_index=0, trade_count_changed=False)
    assert not daily.accept(point, bar_index=1, trade_count_changed=False)
    assert daily.accept(next_day, bar_index=2, trade_count_changed=False)

    on_change = EquitySampler("on_change", 2)
    assert on_change.accept(point, bar_index=0, trade_count_changed=False)
    assert not on_change.accept(point, bar_index=1, trade_count_changed=False)
    assert on_change.accept(
        _point(equity=101.0),
        bar_index=2,
        trade_count_changed=False,
    )

    on_trade = EquitySampler("on_trade", 2)
    assert not on_trade.accept(point, bar_index=0, trade_count_changed=False)
    assert on_trade.accept(point, bar_index=1, trade_count_changed=True)

    hybrid = EquitySampler("hybrid", 3)
    assert hybrid.accept(point, bar_index=1, trade_count_changed=False)
    assert not hybrid.accept(point, bar_index=2, trade_count_changed=False)
    assert hybrid.accept(point, bar_index=3, trade_count_changed=False)
    assert hybrid.accept(later_hour, bar_index=4, trade_count_changed=False)
    assert hybrid.accept(later_hour, bar_index=5, trade_count_changed=True)
    assert not hybrid.accept(
        _point(timestamp=later_hour.timestamp, equity=101.0),
        bar_index=5,
        trade_count_changed=False,
    )
    assert hybrid.accept(
        _point(timestamp=later_hour.timestamp, equity=101.0, position_open=True),
        bar_index=5,
        trade_count_changed=False,
    )


def test_memory_guard_disabled_and_exceeded_paths() -> None:
    MemoryGuard(None).check(_counters(rss_bytes=10**9))

    guard = MemoryGuard(1)
    guard.check(_counters(rss_bytes=1024))
    with pytest.raises(ResourceLimitExceeded, match="memory guard exceeded") as exc_info:
        guard.check(_counters(rss_bytes=2 * 1024 * 1024))
    assert exc_info.value.counters is not None
    assert exc_info.value.counters.bars_processed == 7
