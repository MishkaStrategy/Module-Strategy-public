from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core.adapters import HistoricalDataFeed
from modular_core.events import EventEnvelope
from modular_core.models import Candle
from modular_core.replay import verify_event_stream


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candle(index: int = 0) -> Candle:
    return Candle(
        timestamp=NOW + timedelta(minutes=5 * index),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _event(
    sequence: int,
    *,
    run_id: str = "run-1",
    event_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or f"evt-{sequence}",
        run_id=run_id,
        sequence=sequence,
        timestamp=NOW + timedelta(seconds=sequence),
        event_type="test.event",
        source="test",
        payload={},
    )


def test_historical_data_feed_materializes_one_shot_iterable() -> None:
    feed = HistoricalDataFeed(_candle(index) for index in range(2))

    assert tuple(feed) == (_candle(0), _candle(1))
    assert tuple(feed) == (_candle(0), _candle(1))


@pytest.mark.parametrize("value", [object(), "not-candles", b"not-candles"])
def test_historical_data_feed_rejects_non_iterable_or_text(value: object) -> None:
    with pytest.raises(TypeError, match="iterable of Candle"):
        HistoricalDataFeed(value)  # type: ignore[arg-type]


def test_historical_data_feed_rejects_non_candle_elements() -> None:
    with pytest.raises(TypeError, match="contain Candle"):
        HistoricalDataFeed([_candle(), object()])  # type: ignore[list-item]


@pytest.mark.parametrize("value", [object(), "not-events", b"not-events"])
def test_verify_event_stream_rejects_non_iterable_or_text(value: object) -> None:
    with pytest.raises(TypeError, match="iterable of EventEnvelope"):
        verify_event_stream(value)  # type: ignore[arg-type]


def test_verify_event_stream_rejects_non_event_elements() -> None:
    with pytest.raises(TypeError, match="contain EventEnvelope"):
        verify_event_stream([_event(1), object()])  # type: ignore[list-item]


def test_verify_event_stream_accepts_contiguous_nonzero_sequence_range() -> None:
    report = verify_event_stream([_event(7), _event(8)])

    assert report.valid is True
    assert report.events == 2
    assert report.first_sequence == 7
    assert report.last_sequence == 8
    assert report.errors == ()


def test_verify_event_stream_reports_gap_and_span_mismatch() -> None:
    report = verify_event_stream([_event(7), _event(9)])

    assert report.valid is False
    assert "sequence_gap:evt-9:7->9" in report.errors
    assert "sequence_count_mismatch:2!=3" in report.errors


def test_verify_event_stream_rejects_noncanonical_run_identity() -> None:
    report = verify_event_stream([_event(1, run_id=" run-1 ")])

    assert report.valid is False
    assert report.run_id == " run-1 "
    assert report.errors == ("non_canonical_run_id:evt-1",)
