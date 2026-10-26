from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from modular_strategy_fibonacci.corrections.profile import (
    CorrectionDepthObservation,
    CorrectionProfile,
    classify_correction_depth,
)

LEVELS = (
    Decimal("0.5"),
    Decimal("0.618"),
    Decimal("0.786"),
    Decimal("0.886"),
)


def observation(
    index: int,
    *,
    symbol: str,
    timeframe: str,
    level: str,
    ratio: str | None = None,
) -> CorrectionDepthObservation:
    return CorrectionDepthObservation(
        symbol=symbol,
        timeframe=timeframe,
        direction="bullish",
        observed_at=datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
        actual_ratio=Decimal(ratio or level),
        reference_level=Decimal(level),
        wave_id=f"wave-{symbol}-{timeframe}-{index}",
        dataset_hash="dataset-v1",
    )


def test_classification_keeps_deeper_levels_distinct() -> None:
    assert classify_correction_depth(Decimal("0.48"), LEVELS, shallow_tolerance=Decimal("0.03")) == Decimal("0.5")
    assert classify_correction_depth(Decimal("0.617"), LEVELS, shallow_tolerance=Decimal("0.03")) == Decimal("0.5")
    assert classify_correction_depth(Decimal("0.618"), LEVELS, shallow_tolerance=Decimal("0.03")) == Decimal("0.618")
    assert classify_correction_depth(Decimal("0.80"), LEVELS, shallow_tolerance=Decimal("0.03")) == Decimal("0.786")
    assert classify_correction_depth(Decimal("0.92"), LEVELS, shallow_tolerance=Decimal("0.03")) == Decimal("0.886")


def test_profiles_are_scoped_by_symbol_and_timeframe() -> None:
    profile = CorrectionProfile(
        [
            observation(1, symbol="BTCUSDT", timeframe="15m", level="0.5"),
            observation(2, symbol="BTCUSDT", timeframe="15m", level="0.5"),
            observation(3, symbol="BTCUSDT", timeframe="15m", level="0.618"),
            observation(4, symbol="BTCUSDT", timeframe="4h", level="0.786"),
            observation(5, symbol="ETHUSDT", timeframe="15m", level="0.886"),
        ]
    )

    btc_15m = profile.snapshot(
        symbol="BTCUSDT",
        timeframe="15m",
        levels=LEVELS,
        as_of=datetime(2025, 2, 1, tzinfo=timezone.utc),
    )
    assert btc_15m.sample_size == 3
    assert btc_15m.preferred_levels[0] == Decimal("0.5")
    assert dict(btc_15m.level_counts)[Decimal("0.5")] == 2
    assert dict(btc_15m.level_counts)[Decimal("0.786")] == 0

    btc_4h = profile.snapshot(
        symbol="BTCUSDT",
        timeframe="4h",
        levels=LEVELS,
        as_of=datetime(2025, 2, 1, tzinfo=timezone.utc),
    )
    assert btc_4h.sample_size == 1
    assert btc_4h.preferred_levels == (Decimal("0.786"),)


def test_profile_never_uses_observations_after_as_of() -> None:
    profile = CorrectionProfile(
        [
            observation(1, symbol="BTCUSDT", timeframe="15m", level="0.5"),
            observation(20, symbol="BTCUSDT", timeframe="15m", level="0.886"),
        ]
    )
    snapshot = profile.snapshot(
        symbol="BTCUSDT",
        timeframe="15m",
        levels=LEVELS,
        as_of=datetime(2025, 1, 10, tzinfo=timezone.utc),
    )
    assert snapshot.sample_size == 1
    assert snapshot.preferred_levels == (Decimal("0.5"),)


def test_smoothed_level_score_is_available_for_downstream_decisions() -> None:
    profile = CorrectionProfile(
        [
            observation(1, symbol="BTCUSDT", timeframe="15m", level="0.786"),
            observation(2, symbol="BTCUSDT", timeframe="15m", level="0.786"),
            observation(3, symbol="BTCUSDT", timeframe="15m", level="0.5"),
        ]
    )
    as_of = datetime(2025, 2, 1, tzinfo=timezone.utc)
    deep_score = profile.score_level(
        Decimal("0.786"),
        symbol="BTCUSDT",
        timeframe="15m",
        levels=LEVELS,
        as_of=as_of,
    )
    shallow_score = profile.score_level(
        Decimal("0.5"),
        symbol="BTCUSDT",
        timeframe="15m",
        levels=LEVELS,
        as_of=as_of,
    )
    assert deep_score > shallow_score


def test_observations_can_be_extracted_from_completed_workflow() -> None:
    from datetime import timedelta

    from modular_strategy_fibonacci.config import FibonacciConfig
    from modular_strategy_fibonacci.contracts import Candle, WorkflowPhase
    from modular_strategy_fibonacci.corrections import observations_from_workflow
    from modular_strategy_fibonacci.workflow import SequentialFibonacciEngine

    engine = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(min_atr_multiple=Decimal("0.5")),
    )
    engine.seed_wave(
        direction="bullish",
        start_time=datetime(2024, 12, 31, 22, tzinfo=timezone.utc),
        start_price=Decimal("100"),
        end_time=datetime(2024, 12, 31, 23, tzinfo=timezone.utc),
        end_price=Decimal("200"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )

    def make(index: int, high: str, low: str) -> Candle:
        opened = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index)
        return Candle(
            open_time=opened,
            close_time=opened + timedelta(minutes=15),
            open=Decimal("160"),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal("160"),
            volume=Decimal("1"),
        )

    engine.process(make(0, "195", "121.4"))
    snapshot = engine.process(make(1, "218", "130"))
    observations = observations_from_workflow(snapshot)
    assert len(observations) == 1
    assert observations[0].reference_level == Decimal("0.786")
    assert observations[0].actual_ratio == Decimal("0.786")
