from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from modular_core import Candle

from tests.support import market_view
from wave_analysis import (
    WaveAnalysisConfigurationError,
    WaveAnalysisDataError,
    WaveAnalysisModule,
    WaveAnalysisStateError,
)
from wave_analysis.analysis import build_hypotheses, update_hypothesis_invalidation
from wave_analysis.enums import HypothesisPattern, HypothesisStatus, WaveClassification, WaveDirection
from wave_analysis.models import BarRecord, TimeframeState, WaveLeg

NOW = datetime(2026, 2, 1, tzinfo=UTC)


def make_leg(
    index: int,
    direction: WaveDirection,
    start: str,
    end: str,
    classification: WaveClassification = WaveClassification.IMPULSE,
    efficiency: str = "0.8",
) -> WaveLeg:
    start_d, end_d = Decimal(start), Decimal(end)
    change = end_d - start_d
    return WaveLeg(
        wave_id=f"w{index}",
        symbol="BTCUSDT",
        timeframe="1m",
        direction=direction,
        start_pivot_id=f"p{index}",
        end_pivot_id=f"p{index + 1}",
        start_time=NOW + timedelta(minutes=index),
        end_time=NOW + timedelta(minutes=index + 1),
        confirmed_at=NOW + timedelta(minutes=index + 2),
        start_price=start_d,
        end_price=end_d,
        price_change=change,
        price_change_pct=change / start_d,
        duration_bars=1,
        duration_seconds=60,
        range=abs(change),
        atr_normalized_size=Decimal("2"),
        volume=Decimal("100"),
        relative_volume=Decimal("1.2"),
        body_efficiency=Decimal("0.7"),
        path_efficiency=Decimal(efficiency),
        maximum_adverse_excursion=Decimal("0.2"),
        maximum_favorable_excursion=abs(change),
        slope=change,
        velocity=change / Decimal("60"),
        acceleration=None,
        classification=classification,
        classification_score=Decimal("0.8"),
        confidence=Decimal("0.8"),
        status="confirmed",
        reason_codes=("test",),
        feature_inputs={},
        config_fingerprint="cfg",
        metadata={"segment_id": 0},
    )


def test_invalid_enum_values_use_configuration_error() -> None:
    for field, value in (("pivot_method", "bad"), ("tie_policy", "bad"), ("price_basis", "bad")):
        kwargs = {"timeframes": ("1m",), "primary_timeframe": "1m", field: value}
        with pytest.raises(WaveAnalysisConfigurationError):
            WaveAnalysisModule(**kwargs)


def test_restore_rejects_non_boolean_gap_flag() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    candle = Candle(NOW, 100, 101, 99, 100, symbol="BTCUSDT", timeframe="1m")
    module.on_bar(market_view((candle,), {"1m": (candle,)}, 0))
    state = dict(module.snapshot_state())
    tf = dict(state["timeframes"])
    item = dict(tf["1m"])
    item["gap_blocked"] = "false"
    tf["1m"] = item
    state["timeframes"] = tf
    with pytest.raises((WaveAnalysisStateError, ValueError)):
        module.restore_state(state)


def test_restore_rejects_naive_last_open_time() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    candle = Candle(NOW, 100, 101, 99, 100, symbol="BTCUSDT", timeframe="1m")
    module.on_bar(market_view((candle,), {"1m": (candle,)}, 0))
    state = dict(module.snapshot_state())
    tf = dict(state["timeframes"])
    item = dict(tf["1m"])
    item["last_open_time"] = "2026-02-01T00:00:00"
    tf["1m"] = item
    state["timeframes"] = tf
    with pytest.raises((WaveAnalysisStateError, ValueError)):
        module.restore_state(state)


def test_restore_rejects_negative_runtime_counter() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    state = dict(module.snapshot_state())
    tf = dict(state["timeframes"])
    item = dict(tf["1m"])
    item["revision_counter"] = -1
    tf["1m"] = item
    state["timeframes"] = tf
    with pytest.raises(WaveAnalysisStateError):
        module.restore_state(state)


def test_elliott_hard_rule_and_diagonal_alternative() -> None:
    config = WaveAnalysisModule(
        timeframes=("1m",), primary_timeframe="1m", elliott_enabled=True, hypothesis_min_confidence="0"
    ).config
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT")
    state.wave_legs = [
        make_leg(0, WaveDirection.UP, "100", "110"),
        make_leg(1, WaveDirection.DOWN, "110", "99", WaveClassification.CORRECTION),
        make_leg(2, WaveDirection.UP, "99", "116"),
        make_leg(3, WaveDirection.DOWN, "116", "108", WaveClassification.CORRECTION),
        make_leg(4, WaveDirection.UP, "108", "122"),
    ]
    result = build_hypotheses(state, config, NOW + timedelta(minutes=10))
    assert any(item.pattern_type is HypothesisPattern.DIAGONAL_LIKE for item in result)
    assert all(item.pattern_type is not HypothesisPattern.IMPULSE_12345 for item in result)


def test_active_hypothesis_transitions_to_invalidated() -> None:
    config = WaveAnalysisModule(
        timeframes=("1m",), primary_timeframe="1m", elliott_enabled=True, hypothesis_min_confidence="0"
    ).config
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT")
    state.wave_legs = [
        make_leg(0, WaveDirection.UP, "100", "110"),
        make_leg(1, WaveDirection.DOWN, "110", "105", WaveClassification.CORRECTION),
        make_leg(2, WaveDirection.UP, "105", "125"),
    ]
    result = build_hypotheses(state, config, NOW + timedelta(minutes=5))
    assert result and result[0].status in {HypothesisStatus.ACTIVE, HypothesisStatus.INCOMPLETE}
    invalidation = result[0].invalidation_price
    bar = BarRecord(
        NOW + timedelta(minutes=6),
        NOW + timedelta(minutes=7),
        invalidation,
        invalidation + Decimal("1"),
        invalidation - Decimal("0.1"),
        invalidation,
        Decimal("100"),
    )
    assert update_hypothesis_invalidation(state, bar) is True
    assert state.hypotheses[0].status is HypothesisStatus.INVALIDATED
    assert state.hypotheses[0].confidence == 0
    assert state.hypotheses[0].metadata["invalidated_at"] == bar.close_time.isoformat()


def test_invalidated_hypothesis_is_preserved_bounded_on_rebuild() -> None:
    config = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        elliott_enabled=True,
        hypothesis_min_confidence="0",
        maximum_hypotheses=2,
    ).config
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT")
    state.wave_legs = [
        make_leg(0, WaveDirection.UP, "100", "110"),
        make_leg(1, WaveDirection.DOWN, "110", "105", WaveClassification.CORRECTION),
        make_leg(2, WaveDirection.UP, "105", "125"),
    ]
    build_hypotheses(state, config, NOW + timedelta(minutes=5))
    inv = state.hypotheses[0].invalidation_price
    bar = BarRecord(
        NOW + timedelta(minutes=6),
        NOW + timedelta(minutes=7),
        inv,
        inv + Decimal("1"),
        inv - Decimal("0.1"),
        inv,
        Decimal("100"),
    )
    update_hypothesis_invalidation(state, bar)
    state.wave_legs.extend(
        [
            make_leg(3, WaveDirection.DOWN, "125", "115", WaveClassification.CORRECTION),
            make_leg(4, WaveDirection.UP, "115", "132"),
        ]
    )
    result = build_hypotheses(state, config, NOW + timedelta(minutes=10))
    assert len(result) <= 2
    assert any(item.status is HypothesisStatus.INVALIDATED for item in result)


def test_invalid_target_does_not_consume_base_event() -> None:
    module = WaveAnalysisModule(timeframes=("5m",), primary_timeframe="5m")
    base = Candle(NOW + timedelta(minutes=14), 100, 101, 99, 100, symbol="BTCUSDT", timeframe="1m")
    wrong = Candle(NOW, 100, 101, 99, 100, symbol="BTCUSDT", timeframe="15m")
    before = module.state_digest
    with pytest.raises(WaveAnalysisDataError):
        module.on_bar(market_view((base,), {"5m": (wrong,)}, 0))
    assert module.state_digest == before
    corrected = Candle(NOW + timedelta(minutes=10), 100, 101, 99, 100, symbol="BTCUSDT", timeframe="5m")
    result = module.on_bar(market_view((base,), {"5m": (corrected,)}, 0))
    assert result.skips != ("wave_analysis.duplicate_bar",)


def test_conflicting_htf_duplicate_does_not_consume_new_base() -> None:
    module = WaveAnalysisModule(timeframes=("5m",), primary_timeframe="5m")
    target = Candle(NOW, 100, 101, 99, 100, symbol="BTCUSDT", timeframe="5m")
    base1 = Candle(NOW + timedelta(minutes=4), 100, 101, 99, 100, symbol="BTCUSDT", timeframe="1m")
    module.on_bar(market_view((base1,), {"5m": (target,)}, 0))
    base2 = Candle(NOW + timedelta(minutes=5), 101, 102, 100, 101, symbol="BTCUSDT", timeframe="1m")
    conflict = Candle(NOW, 100, 102, 99, 101, symbol="BTCUSDT", timeframe="5m")
    with pytest.raises(WaveAnalysisDataError):
        module.on_bar(market_view((base1, base2), {"5m": (conflict,)}, 1))
    result = module.on_bar(market_view((base1, base2), {"5m": (target,)}, 1))
    assert result.skips != ("wave_analysis.duplicate_bar",)


def test_provisional_only_publication_is_selective() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.50",
        publish_provisional=True,
    )
    history = []
    decisions = []
    for index in range(5):
        price = 100 + index
        item = Candle(
            NOW + timedelta(minutes=index),
            price,
            price + 1,
            price - 1,
            price,
            100,
            "BTCUSDT",
            "1m",
        )
        history.append(item)
        decisions.append(module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index)))
    published = [decision for decision in decisions if decision.features]
    assert published
    for decision in published:
        keys = {feature.key for feature in decision.features}
        assert keys <= {"wave_analysis.provisional_pivot", "wave_analysis.current_wave"}
        assert "wave_analysis.mtf_context" not in keys


def test_confirmed_feature_snapshots_are_bounded() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.01",
        feature_snapshot_limit=2,
        maximum_retained_pivots=16,
        maximum_retained_waves=15,
        publish_provisional=False,
    )
    decision = None
    for index in range(30):
        price = 100.0 if index % 2 == 0 else 110.0
        item = Candle(
            NOW + timedelta(minutes=index),
            price,
            price + 0.5,
            price - 0.5,
            price,
            100,
            "BTCUSDT",
            "1m",
        )
        decision = module.on_bar(market_view((item,), {"1m": (item,)}, index))
    assert decision is not None
    by_key = {feature.key: feature.value for feature in decision.features}
    pivots = by_key["wave_analysis.confirmed_pivots"]
    waves = by_key["wave_analysis.wave_legs"]
    assert pivots["count"] > 2
    assert pivots["snapshot_count"] == 2
    assert pivots["snapshot_truncated"] is True
    assert len(pivots["items"]) == 2
    assert waves["count"] > 2
    assert waves["snapshot_count"] == 2
    assert waves["snapshot_truncated"] is True
    assert len(waves["items"]) == 2


def test_signed_zero_volume_is_an_idempotent_duplicate() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    timestamp = NOW + timedelta(days=10)
    positive_zero = Candle(timestamp, 100.0, 101.0, 99.0, 100.0, 0.0, "BTCUSDT", "1m")
    negative_zero = Candle(timestamp, 100.0, 101.0, 99.0, 100.0, -0.0, "BTCUSDT", "1m")
    module.on_bar(market_view((positive_zero,), {"1m": (positive_zero,)}, 0))
    decision = module.on_bar(market_view((negative_zero,), {"1m": (negative_zero,)}, 0))
    assert decision.skips == ("wave_analysis.duplicate_bar",)
    assert module.snapshot_state()["conflicting_duplicates"] == 0
