from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_strategy_fibonacci.contracts import (
    Candle,
    CorrectionTouch,
    FibonacciLevel,
    FibonacciProjection,
    FibonacciWaveSnapshot,
    FibonacciWorkflowEvent,
    ImpulseCandidate,
    NoFibonacciProjection,
    WorkflowEventType,
    WorkflowPhase,
)


def test_candle_normalizes_aware_timestamp_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    candle = Candle(
        open_time=datetime(2025, 1, 1, 2, tzinfo=plus_two),
        close_time=datetime(2025, 1, 1, 2, 15, tzinfo=plus_two),
        open=Decimal("10"),
        high=Decimal("12"),
        low=Decimal("9"),
        close=Decimal("11"),
        volume=Decimal("1"),
    )
    assert candle.open_time == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Candle(
            open_time=datetime(2025, 1, 1),
            close_time=datetime(2025, 1, 1, 0, 15),
            open=Decimal("10"),
            high=Decimal("12"),
            low=Decimal("9"),
            close=Decimal("11"),
            volume=Decimal("1"),
        )


def test_invalid_ohlc_and_negative_volume_rejected() -> None:
    base = dict(
        open_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        close_time=datetime(2025, 1, 1, 0, 15, tzinfo=timezone.utc),
        open=Decimal("10"),
        low=Decimal("9"),
        close=Decimal("11"),
    )
    with pytest.raises(ValueError, match="high"):
        Candle(**base, high=Decimal("10.5"), volume=Decimal("1"))
    with pytest.raises(ValueError, match="volume"):
        Candle(**base, high=Decimal("12"), volume=Decimal("-1"))


def test_endpoint_confirmation_fields_must_be_consistent() -> None:
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="set together"):
        FibonacciWaveSnapshot(
            wave_id="wave-1",
            sequence_number=1,
            direction="bullish",
            phase=WorkflowPhase.AWAITING_CORRECTION,
            start_time=base_time,
            start_price=Decimal("100"),
            end_time=base_time + timedelta(minutes=15),
            end_price=Decimal("120"),
            endpoint_revision=0,
            correction=None,
            completion_target=None,
            completion_time=None,
            completion_price=None,
            start_candle_index=0,
            end_candle_index=1,
            phase_started_index=1,
            endpoint_confirmed_at=base_time + timedelta(minutes=30),
        )


def _times():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return start, start + timedelta(minutes=15), start + timedelta(minutes=30)


def test_nonfinite_decimals_and_close_order_are_rejected() -> None:
    start, end, _ = _times()
    with pytest.raises(ValueError, match="finite"):
        FibonacciLevel(Decimal("NaN"), Decimal("1"))
    with pytest.raises(ValueError, match="after"):
        Candle(start, start, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0"))
    with pytest.raises(ValueError, match="low"):
        Candle(start, end, Decimal("10"), Decimal("12"), Decimal("11"), Decimal("10"), Decimal("1"))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"start_index": -1}, "indexes"),
        ({"end_index": 0}, "indexes"),
        ({"end_time": datetime(2024, 1, 1, tzinfo=timezone.utc)}, "times"),
        ({"end_price": Decimal("99")}, "bullish"),
        ({"score": 1.1}, "score"),
        ({"confirmation_index": 0}, "confirmation_index"),
        ({"confirmed_at": datetime(2024, 1, 1, tzinfo=timezone.utc)}, "confirmed_at"),
    ],
)
def test_impulse_candidate_validation(updates, message) -> None:
    start, end, confirmed = _times()
    payload = dict(
        candidate_id="candidate",
        direction="bullish",
        start_index=0,
        end_index=1,
        start_time=start,
        end_time=end,
        start_price=Decimal("100"),
        end_price=Decimal("120"),
        score=0.8,
        evidence=(),
        confirmation_index=2,
        confirmed_at=confirmed,
    )
    payload.update(updates)
    with pytest.raises(ValueError, match=message):
        ImpulseCandidate(**payload)


def test_bearish_candidate_must_end_below_start() -> None:
    start, end, _ = _times()
    with pytest.raises(ValueError, match="bearish"):
        ImpulseCandidate(
            "candidate",
            "bearish",
            0,
            1,
            start,
            end,
            Decimal("100"),
            Decimal("110"),
            0.5,
            (),
        )


@pytest.mark.parametrize(
    ("ratio", "index", "message"),
    [
        (Decimal("-0.01"), 1, "ratio"),
        (Decimal("1"), 1, "ratio"),
        (Decimal("0.5"), -1, "candle_index"),
    ],
)
def test_correction_touch_validation(ratio, index, message) -> None:
    start, _, _ = _times()
    with pytest.raises(ValueError, match=message):
        CorrectionTouch(start, Decimal("100"), ratio, Decimal("0.5"), index)


def test_workflow_event_requires_identity_and_index() -> None:
    start, _, _ = _times()
    with pytest.raises(ValueError, match="wave_id"):
        FibonacciWorkflowEvent(WorkflowEventType.WAVE_STARTED, start, "", 0)
    with pytest.raises(ValueError, match="candle_index"):
        FibonacciWorkflowEvent(WorkflowEventType.WAVE_STARTED, start, "wave", -1)


def _valid_wave(**updates):
    start, end, confirmed = _times()
    payload = dict(
        wave_id="wave",
        sequence_number=1,
        direction="bullish",
        phase=WorkflowPhase.AWAITING_CORRECTION,
        start_time=start,
        start_price=Decimal("100"),
        end_time=end,
        end_price=Decimal("120"),
        endpoint_revision=0,
        correction=None,
        completion_target=None,
        completion_time=None,
        completion_price=None,
        start_candle_index=0,
        end_candle_index=1,
        phase_started_index=1,
        endpoint_confirmed_at=confirmed,
        endpoint_confirmation_index=2,
    )
    payload.update(updates)
    return FibonacciWaveSnapshot(**payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"sequence_number": 0}, "sequence_number"),
        ({"end_time": datetime(2024, 1, 1, tzinfo=timezone.utc)}, "end_time"),
        ({"endpoint_revision": -1}, "endpoint_revision"),
        ({"end_price": Decimal("90")}, "bullish"),
        ({"start_candle_index": -2}, "indexes"),
        ({"endpoint_confirmed_at": datetime(2025, 1, 1, tzinfo=timezone.utc)}, "confirmation"),
        ({"endpoint_confirmation_index": 0}, "confirmation index"),
    ],
)
def test_wave_validation(updates, message) -> None:
    with pytest.raises(ValueError, match=message):
        _valid_wave(**updates)


def _valid_projection(**updates):
    start, end, confirmed = _times()
    payload = dict(
        projection_id="projection",
        symbol="BTCUSDT",
        market="binance-usdtm-futures",
        timeframe="15m",
        cutoff_time=confirmed,
        direction="bullish",
        impulse_start_time=start,
        impulse_start_price=Decimal("100"),
        impulse_end_time=end,
        impulse_end_price=Decimal("120"),
        levels=(FibonacciLevel(Decimal("0"), Decimal("120")),),
        confidence=0.8,
        candidate_score=0.7,
        model_version="v1",
        config_hash="cfg",
        dataset_hash="data",
        evidence=(),
        endpoint_confirmed_at=confirmed,
        endpoint_confirmation_index=2,
    )
    payload.update(updates)
    return FibonacciProjection(**payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"cutoff_time": datetime(2025, 1, 1, tzinfo=timezone.utc)}, "after cutoff"),
        ({"confidence": -0.1}, "confidence"),
        ({"candidate_score": 2.0}, "candidate_score"),
        ({"levels": ()}, "levels"),
        ({"sequence_number": 0}, "sequence_number"),
        ({"endpoint_revision": -1}, "endpoint_revision"),
        ({"correction_actual_ratio": Decimal("1")}, "correction_actual_ratio"),
        ({"correction_profile_sample_size": -1}, "sample_size"),
        ({"endpoint_confirmation_index": -1}, "confirmation_index"),
        ({"endpoint_confirmed_at": None}, "set together"),
        ({"correction_level_probabilities": ((Decimal("0.5"), 1.1),)}, "probability"),
    ],
)
def test_projection_validation(updates, message) -> None:
    with pytest.raises(ValueError, match=message):
        _valid_projection(**updates)


def test_no_projection_validation() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        NoFibonacciProjection("", 0.5, ())
    with pytest.raises(ValueError, match="confidence"):
        NoFibonacciProjection("none", 1.1, ())
