from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_core.models import Candle
from modular_core.timeframes import (
    TimeframeAggregator,
    TimeframeSpec,
    TimeframeStore,
)


def _bar(index: int, *, start: datetime | None = None) -> Candle:
    origin = start or datetime(2024, 1, 1, tzinfo=UTC)
    price = 100.0 + index
    return Candle(
        timestamp=origin + timedelta(minutes=5 * index),
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.5,
        volume=10.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def test_utc_fast_bucket_preserves_completed_bar_boundary() -> None:
    aggregator = TimeframeAggregator(
        ratio=48,
        output_timeframe="4h",
        expected_base_interval=timedelta(minutes=5),
        align_to_clock=True,
        alignment_timezone="UTC",
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    completed = None
    for index in range(48):
        completed = aggregator.push(_bar(index, start=start))

    assert completed is not None
    assert completed.timestamp == start
    assert completed.close_time == start + timedelta(hours=4)
    assert completed.timeframe == "4h"


def test_snapshot_view_is_reused_until_a_completed_htf_bar_arrives() -> None:
    store = TimeframeStore(
        (TimeframeSpec(3, "15m", max_history=16),),
        expected_base_interval=timedelta(minutes=5),
    )
    store.push(_bar(0))
    first = store.snapshot_view()
    store.push(_bar(1))
    assert store.snapshot_view() is first

    store.push(_bar(2))
    second = store.snapshot_view()
    assert second is not first
    assert len(second["15m"]) == 1


def test_chunked_compaction_bounds_visible_history_and_preserves_saved_views() -> None:
    store = TimeframeStore(
        (TimeframeSpec(1, "5m", max_history=3),),
        expected_base_interval=timedelta(minutes=5),
    )
    for index in range(3):
        store.push(_bar(index))
    saved = store.snapshot_view()["5m"]
    saved_values = tuple(saved)

    for index in range(3, 80):
        store.push(_bar(index))

    current = store.snapshot_view()["5m"]
    assert len(current) == 3
    assert store.retention_counts()["5m"] <= 67
    assert [bar.timestamp for bar in current] == [_bar(index).timestamp for index in range(77, 80)]
    assert tuple(saved) == saved_values
