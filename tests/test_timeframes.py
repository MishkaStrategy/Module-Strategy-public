from datetime import datetime, timedelta, timezone

import pytest

from modular_core import Candle
from modular_core.timeframes import (
    TimeframeAggregator,
    TimeframeSpec,
    TimeframeStore,
)


def _bar(
    minute: int,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    close: float = 100.0,
) -> Candle:
    return Candle(
        datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
        close,
        close + 1,
        close - 1,
        close,
        1,
        symbol,
        timeframe,
    )


def test_fixed_ratio_aggregation(bars_factory):
    bars = bars_factory([(100, 102, 99, 101), (101, 104, 100, 103), (103, 105, 102, 104)])
    aggregator = TimeframeAggregator(3, "15m")
    assert aggregator.push(bars[0]) is None
    assert aggregator.push(bars[1]) is None
    output = aggregator.push(bars[2])
    assert output is not None
    assert (
        output.open,
        output.high,
        output.low,
        output.close,
        output.volume,
    ) == (100, 105, 99, 104, 3)


def test_max_history_does_not_mutate_old_snapshot(bars_factory):
    bars = bars_factory([100, 101])
    store = TimeframeStore(
        (TimeframeSpec(1, "5m-copy", max_history=1),),
        expected_base_interval=timedelta(minutes=5),
    )
    store.push(bars[0])
    old_snapshot = store.snapshot()["5m-copy"]
    store.push(bars[1])
    new_snapshot = store.snapshot()["5m-copy"]
    assert [bar.close for bar in old_snapshot] == [100]
    assert [bar.close for bar in new_snapshot] == [101]


@pytest.mark.parametrize("value", [True, 0.5, "3"])
def test_timeframe_ratio_requires_real_integer(value: object) -> None:
    with pytest.raises(TypeError, match="ratio must be int"):
        TimeframeSpec(value, "15m")  # type: ignore[arg-type]


def test_timeframe_boundaries_reject_invalid_flags_and_intervals() -> None:
    with pytest.raises(TypeError, match="gap_safe must be bool"):
        TimeframeSpec(3, "15m", gap_safe=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expected_base_interval must be positive"):
        TimeframeAggregator(
            3,
            "15m",
            expected_base_interval=timedelta(0),
        )
    with pytest.raises(TypeError, match="expected_base_interval must be timedelta"):
        TimeframeStore((), expected_base_interval=300)  # type: ignore[arg-type]


def test_aggregator_rejects_duplicate_and_out_of_order_timestamps() -> None:
    aggregator = TimeframeAggregator(
        3,
        "15m",
        expected_base_interval=timedelta(minutes=5),
    )
    first = _bar(5)
    aggregator.push(first)
    with pytest.raises(ValueError, match="duplicate"):
        aggregator.push(first)
    with pytest.raises(ValueError, match="strictly chronological"):
        aggregator.push(_bar(0))


def test_aggregator_rejects_mixed_stream_identity() -> None:
    aggregator = TimeframeAggregator(
        3,
        "15m",
        expected_base_interval=timedelta(minutes=5),
    )
    aggregator.push(_bar(0))
    with pytest.raises(ValueError, match="mix symbols"):
        aggregator.push(_bar(5, symbol="ETHUSDT"))
    aggregator.reset()
    aggregator.push(_bar(0))
    with pytest.raises(ValueError, match="mix base timeframes"):
        aggregator.push(_bar(5, timeframe="1m"))


def test_public_reset_clears_time_and_stream_identity() -> None:
    aggregator = TimeframeAggregator(
        2,
        "10m",
        expected_base_interval=timedelta(minutes=5),
    )
    aggregator.push(_bar(5))
    aggregator.reset()
    assert aggregator.push(_bar(0, symbol="ETHUSDT")) is None
    output = aggregator.push(_bar(5, symbol="ETHUSDT"))
    assert output is not None
    assert output.symbol == "ETHUSDT"
