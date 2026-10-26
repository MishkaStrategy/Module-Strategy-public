from datetime import datetime, timedelta, timezone

import pytest

from modular_core import Candle, EngineConfig
from modular_core.data import DataValidationError, validate_candles
from modular_core.run_manifest import build_run_manifest
from modular_core.timeframes import AggregatedCandle, TimeframeAggregator


def _hourly_bar(timestamp: datetime, close: float = 100.0) -> Candle:
    return Candle(
        timestamp,
        close,
        close + 1,
        close - 1,
        close,
        1,
        "BTCUSDT",
        "1h",
    )


def test_fixed_ratio_bar_is_published_only_after_actual_close() -> None:
    aggregator = TimeframeAggregator(
        3,
        "15m",
        expected_base_interval=timedelta(minutes=5),
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert aggregator.push(Candle(start, 100, 101, 99, 100, 1, "X", "5m")) is None
    assert (
        aggregator.push(
            Candle(
                start + timedelta(minutes=5),
                100,
                101,
                99,
                100,
                1,
                "X",
                "5m",
            )
        )
        is None
    )
    output = aggregator.push(
        Candle(
            start + timedelta(minutes=10),
            100,
            101,
            99,
            100,
            1,
            "X",
            "5m",
        )
    )
    assert isinstance(output, AggregatedCandle)
    assert output.timestamp == start
    assert output.closed_at == start + timedelta(minutes=15)
    assert output.close_time == start + timedelta(minutes=15)


def test_parseable_output_duration_must_match_ratio() -> None:
    with pytest.raises(ValueError, match="output timeframe duration"):
        TimeframeAggregator(
            3,
            "1h",
            expected_base_interval=timedelta(minutes=5),
        )


@pytest.mark.parametrize(
    ("start", "count", "duration_hours"),
    [
        (datetime(2026, 3, 28, 23, tzinfo=timezone.utc), 23, 23),
        (datetime(2026, 10, 24, 22, tzinfo=timezone.utc), 25, 25),
    ],
)
def test_calendar_day_uses_actual_dst_close_time(
    start: datetime,
    count: int,
    duration_hours: int,
) -> None:
    aggregator = TimeframeAggregator(
        24,
        "1d",
        expected_base_interval=timedelta(hours=1),
        align_to_clock=True,
        alignment_timezone="Europe/Amsterdam",
    )
    output = None
    for index in range(count):
        current = aggregator.push(_hourly_bar(start + timedelta(hours=index)))
        if index < count - 1:
            assert current is None
        else:
            output = current
    assert isinstance(output, AggregatedCandle)
    assert output.closed_at == start + timedelta(hours=duration_hours)
    assert output.close_time - output.timestamp == timedelta(hours=duration_hours)


def test_aggregated_close_time_is_utc_and_strictly_after_open() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candle = AggregatedCandle(
        start,
        100,
        101,
        99,
        100,
        1,
        "X",
        "1d",
        closed_at=start + timedelta(hours=23),
    )
    assert candle.close_time == start + timedelta(hours=23)
    with pytest.raises(ValueError, match="closed_at must be after"):
        AggregatedCandle(
            start,
            100,
            101,
            99,
            100,
            1,
            "X",
            "1d",
            closed_at=start,
        )


def test_input_candle_intervals_cannot_overlap() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = AggregatedCandle(
        start,
        100,
        101,
        99,
        100,
        1,
        "X",
        "5m",
        closed_at=start + timedelta(minutes=10),
    )
    second = Candle(
        start + timedelta(minutes=5),
        100,
        101,
        99,
        100,
        1,
        "X",
        "5m",
    )
    with pytest.raises(DataValidationError, match="cannot overlap"):
        validate_candles((first, second))


def test_run_manifest_hash_includes_actual_close_time() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = AggregatedCandle(
        start,
        100,
        101,
        99,
        100,
        1,
        "X",
        "1d",
        closed_at=start + timedelta(hours=23),
    )
    second = AggregatedCandle(
        start,
        100,
        101,
        99,
        100,
        1,
        "X",
        "1d",
        closed_at=start + timedelta(hours=25),
    )
    manifest_a = build_run_manifest(
        kernel_version="0.4.2",
        created_at=start,
        bars=(first,),
        config=EngineConfig(),
        manifests=(),
    )
    manifest_b = build_run_manifest(
        kernel_version="0.4.2",
        created_at=start,
        bars=(second,),
        config=EngineConfig(),
        manifests=(),
    )
    assert manifest_a.data_hash != manifest_b.data_hash
    assert manifest_a.run_id != manifest_b.run_id
