from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

modular_core = pytest.importorskip("modular_core")

from modular_trading_runner.errors import (  # noqa: E402
    CriticalQueueOverflow,
    MarketDataRejected,
    MarketIdentityConflict,
)
from modular_trading_runner.market_data import (  # noqa: E402
    BoundedCriticalQueue,
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventStatus,
    MarketEventValidator,
)

Candle = modular_core.Candle
NOW = datetime(2026, 7, 26, 12, 5, 2, tzinfo=UTC)


def policy() -> MarketDataPolicy:
    return MarketDataPolicy(
        market="usdt_perpetual",
        instruments=("BTCUSDT",),
        sources=("binance-usdtm",),
        timeframes=("5m",),
        max_event_age_seconds=10.0,
        max_future_skew_seconds=1.0,
        max_tracked_events=3,
    )


def event(
    *,
    event_id: str = "event-1",
    sequence: int = 1,
    timestamp: datetime = datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    received_at: datetime = datetime(2026, 7, 26, 12, 5, 1, tzinfo=UTC),
    close: float = 101.0,
) -> MarketEventEnvelope:
    candle = Candle(
        timestamp=timestamp,
        open=100.0,
        high=102.0,
        low=99.0,
        close=close,
        volume=10.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketEventEnvelope(
        event_id=event_id,
        source_id="binance-usdtm",
        market="usdt_perpetual",
        sequence=sequence,
        available_at=candle.close_time,
        received_at=received_at,
        candle=candle,
    )


def test_accepts_valid_closed_candle_and_exact_duplicate() -> None:
    validator = MarketEventValidator(policy())
    first = validator.validate(event(), now=NOW)
    duplicate = validator.validate(event(), now=NOW)
    assert first.status is MarketEventStatus.ACCEPTED
    assert duplicate.status is MarketEventStatus.DUPLICATE


def test_duplicate_transport_receipt_time_does_not_change_source_identity() -> None:
    validator = MarketEventValidator(policy())
    validator.validate(event(), now=NOW)
    duplicate = validator.validate(
        event(received_at=datetime(2026, 7, 26, 12, 5, 2, tzinfo=UTC)),
        now=datetime(2026, 7, 26, 12, 5, 3, tzinfo=UTC),
    )
    assert duplicate.status is MarketEventStatus.DUPLICATE


def test_known_duplicate_is_deduplicated_before_stale_policy() -> None:
    validator = MarketEventValidator(policy())
    candidate = event()
    validator.validate(candidate, now=NOW)
    duplicate = validator.validate(
        candidate,
        now=datetime(2026, 7, 26, 13, 0, tzinfo=UTC),
    )
    assert duplicate.status is MarketEventStatus.DUPLICATE


def test_freshness_api_rejects_stale_known_duplicate_while_inspect_stays_duplicate_first() -> None:
    validator = MarketEventValidator(policy())
    candidate = event()
    validator.validate(candidate, now=NOW)
    later = NOW + timedelta(hours=1)

    with pytest.raises(MarketDataRejected) as captured:
        validator.validate_freshness(candidate, now=later)
    assert captured.value.reason_code == "stale_event"
    assert validator.inspect(candidate, now=later).status is MarketEventStatus.DUPLICATE


def test_freshness_api_does_not_advance_validator_cursor_or_dedupe_state() -> None:
    validator = MarketEventValidator(policy())
    candidate = event()

    assert validator.inspect(candidate, now=NOW).status is MarketEventStatus.ACCEPTED
    validator.validate_freshness(candidate, now=NOW)
    assert validator.inspect(candidate, now=NOW).status is MarketEventStatus.ACCEPTED
    assert validator.validate(candidate, now=NOW).status is MarketEventStatus.ACCEPTED


def test_same_event_id_with_changed_content_fails_closed() -> None:
    validator = MarketEventValidator(policy())
    validator.validate(event(), now=NOW)
    with pytest.raises(MarketIdentityConflict) as captured:
        validator.validate(event(close=100.5), now=NOW)
    assert captured.value.reason_code == "event_identity_conflict"


def test_sequence_or_candle_regression_is_rejected() -> None:
    validator = MarketEventValidator(policy())
    validator.validate(event(sequence=5), now=NOW)
    with pytest.raises(MarketDataRejected) as captured:
        validator.validate(event(event_id="older", sequence=4), now=NOW)
    assert captured.value.reason_code == "out_of_order_sequence"


def test_sequence_and_time_gaps_are_explicit() -> None:
    validator = MarketEventValidator(policy())
    validator.validate(event(sequence=1), now=NOW)
    later = event(
        event_id="event-3",
        sequence=3,
        timestamp=datetime(2026, 7, 26, 12, 15, tzinfo=UTC),
        received_at=datetime(2026, 7, 26, 12, 20, 1, tzinfo=UTC),
    )
    result = validator.validate(later, now=datetime(2026, 7, 26, 12, 20, 2, tzinfo=UTC))
    assert result.status is MarketEventStatus.GAP
    assert result.sequence_gap == 1
    assert result.time_gap is True


@pytest.mark.parametrize(
    ("received_at", "now", "reason"),
    [
        (
            datetime(2026, 7, 26, 12, 5, 1, tzinfo=UTC),
            datetime(2026, 7, 26, 12, 5, 20, tzinfo=UTC),
            "stale_event",
        ),
        (
            datetime(2026, 7, 26, 12, 5, 5, tzinfo=UTC),
            datetime(2026, 7, 26, 12, 5, 2, tzinfo=UTC),
            "future_timestamp",
        ),
    ],
)
def test_stale_and_future_events_are_rejected(
    received_at: datetime,
    now: datetime,
    reason: str,
) -> None:
    validator = MarketEventValidator(policy())
    candidate = event(received_at=received_at)
    with pytest.raises(MarketDataRejected) as captured:
        validator.validate(candidate, now=now)
    assert captured.value.reason_code == reason


def test_delayed_old_candle_is_stale_even_when_received_recently() -> None:
    validator = MarketEventValidator(policy())
    candidate = event(
        timestamp=datetime(2026, 7, 26, 11, 55, tzinfo=UTC),
        received_at=datetime(2026, 7, 26, 12, 5, 1, tzinfo=UTC),
    )
    with pytest.raises(MarketDataRejected) as captured:
        validator.validate(candidate, now=NOW)
    assert captured.value.reason_code == "stale_event"


def test_critical_queue_never_drops_on_overflow() -> None:
    queue: BoundedCriticalQueue[int] = BoundedCriticalQueue(2)
    queue.put(1)
    queue.put(2)
    with pytest.raises(CriticalQueueOverflow):
        queue.put(3)
    assert queue.snapshot() == (1, 2)
    assert queue.high_water_mark == 2
    assert queue.get() == 1
    assert queue.get() == 2


def test_event_identity_memory_is_bounded() -> None:
    validator = MarketEventValidator(policy())
    timestamp = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    for index in range(5):
        received = timestamp + timedelta(minutes=5, seconds=1)
        validator.validate(
            event(
                event_id=f"event-{index}",
                sequence=index,
                timestamp=timestamp,
                received_at=received,
            ),
            now=received + timedelta(seconds=1),
        )
        timestamp += timedelta(minutes=5)
    assert len(validator._event_hashes) == 3
