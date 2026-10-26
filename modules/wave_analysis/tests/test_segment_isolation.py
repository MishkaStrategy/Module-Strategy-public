from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from modular_core import Candle

from tests.support import market_view
from wave_analysis import PivotKind, PivotStatus, StructureState, WaveAnalysisModule
from wave_analysis.analysis import build_hypotheses, build_structure, extension_metrics, retracement_metrics
from wave_analysis.config import WaveAnalysisConfig
from wave_analysis.enums import WaveClassification, WaveDirection
from wave_analysis.models import PivotPoint, TimeframeState, WaveLeg

NOW = datetime(2026, 4, 1, tzinfo=UTC)


def pivot(index: int, kind: PivotKind, price: str, segment_id: int) -> PivotPoint:
    when = NOW + timedelta(minutes=index)
    return PivotPoint(
        pivot_id=f"p-{segment_id}-{index}",
        symbol="BTCUSDT",
        timeframe="1m",
        kind=kind,
        pivot_time=when,
        confirmed_at=when + timedelta(minutes=1),
        price=Decimal(price),
        bar_index=index,
        status=PivotStatus.CONFIRMED,
        config_fingerprint="cfg",
        metadata={"segment_id": segment_id},
    )


def leg(index: int, direction: WaveDirection, start: str, end: str, segment_id: int) -> WaveLeg:
    start_price = Decimal(start)
    end_price = Decimal(end)
    change = end_price - start_price
    return WaveLeg(
        wave_id=f"w-{segment_id}-{index}",
        symbol="BTCUSDT",
        timeframe="1m",
        direction=direction,
        start_pivot_id=f"p-{segment_id}-{index}",
        end_pivot_id=f"p-{segment_id}-{index + 1}",
        start_time=NOW + timedelta(minutes=index),
        end_time=NOW + timedelta(minutes=index + 1),
        confirmed_at=NOW + timedelta(minutes=index + 2),
        start_price=start_price,
        end_price=end_price,
        price_change=change,
        price_change_pct=change / start_price,
        duration_bars=1,
        duration_seconds=60,
        range=abs(change),
        atr_normalized_size=Decimal("2"),
        volume=Decimal("100"),
        relative_volume=Decimal("1"),
        body_efficiency=Decimal("0.7"),
        path_efficiency=Decimal("0.8"),
        maximum_adverse_excursion=Decimal("0"),
        maximum_favorable_excursion=abs(change),
        slope=change,
        velocity=change / Decimal("60"),
        acceleration=None,
        classification=WaveClassification.IMPULSE,
        classification_score=Decimal("0.8"),
        confidence=Decimal("0.8"),
        status="confirmed",
        reason_codes=(),
        feature_inputs={},
        config_fingerprint="cfg",
        metadata={"segment_id": segment_id, "path_truncated": False},
    )


def test_structure_uses_only_active_gap_segment() -> None:
    config = WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m")
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT", segment_id=1)
    state.confirmed_pivots = [
        pivot(0, PivotKind.LOW, "90", 0),
        pivot(1, PivotKind.HIGH, "100", 0),
        pivot(2, PivotKind.LOW, "95", 0),
        pivot(3, PivotKind.HIGH, "110", 0),
        pivot(10, PivotKind.HIGH, "200", 1),
    ]

    snapshot = build_structure(state, config, NOW + timedelta(minutes=20))

    assert snapshot.state is StructureState.UNDEFINED
    assert snapshot.segment_id == 1
    assert snapshot.reason_codes == ("insufficient_confirmed_swings",)


def test_metrics_and_elliott_never_compare_across_gap_segments() -> None:
    config = WaveAnalysisConfig(
        timeframes=("1m",),
        primary_timeframe="1m",
        elliott_enabled=True,
        hypothesis_min_confidence=Decimal("0"),
    )
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT", segment_id=1)
    state.wave_legs = [
        leg(0, WaveDirection.UP, "100", "120", 0),
        leg(1, WaveDirection.DOWN, "120", "110", 0),
        leg(2, WaveDirection.UP, "110", "140", 0),
        leg(3, WaveDirection.DOWN, "140", "125", 0),
        leg(4, WaveDirection.UP, "125", "150", 0),
        leg(20, WaveDirection.DOWN, "80", "70", 1),
    ]

    assert retracement_metrics(state) is None
    assert extension_metrics(state) is None
    assert build_hypotheses(state, config, NOW + timedelta(minutes=30)) == []


def test_gap_invalidates_current_derived_state_but_preserves_confirmed_history() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        elliott_enabled=True,
        recovery_bars=2,
    )
    prices = [100.0, 94.0, 110.0, 100.0, 120.0, 108.0, 130.0]
    history: list[Candle] = []
    for index, price in enumerate(prices):
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
        module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))

    before = module.snapshot_state()["timeframes"]["1m"]
    assert before["confirmed_pivots"]
    assert before["wave_legs"]

    gap = Candle(
        NOW + timedelta(minutes=len(history)),
        80,
        81,
        79,
        80,
        100,
        "BTCUSDT",
        "1m",
    )
    history.append(gap)
    module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, len(history) - 1, data_gap=True))
    after = module.snapshot_state()["timeframes"]["1m"]

    assert after["confirmed_pivots"] == before["confirmed_pivots"]
    assert after["wave_legs"] == before["wave_legs"]
    assert after["structure"] is None
    assert after["hypotheses"] == []


def _candle(index: int, price: float) -> Candle:
    return Candle(
        NOW + timedelta(minutes=index),
        price,
        price + 1,
        price - 1,
        price,
        100,
        "BTCUSDT",
        "1m",
    )


def test_recovery_bars_rebuild_detector_state_but_suppress_publication() -> None:
    kwargs = dict(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        recovery_bars=3,
    )
    gapped = WaveAnalysisModule(**kwargs)
    history: list[Candle] = []
    for index, price in enumerate((100.0, 106.0, 94.0, 110.0)):
        item = _candle(index, price)
        history.append(item)
        gapped.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))

    gap_index = len(history)
    gap = _candle(gap_index, 80.0)
    history.append(gap)
    gapped.on_bar(market_view(tuple(history), {"1m": tuple(history)}, gap_index, data_gap=True))
    assert gapped.snapshot_state()["timeframes"]["1m"]["segment_id"] == 1

    decisions = []
    recovery = [_candle(gap_index + offset, price) for offset, price in enumerate((100.0, 90.0, 110.0), start=1)]
    for item in recovery:
        history.append(item)
        decisions.append(gapped.on_bar(market_view(tuple(history), {"1m": tuple(history)}, len(history) - 1)))

    assert decisions[0].features == ()
    assert decisions[1].features == ()
    assert "wave_analysis.recovery_warmup" in decisions[0].skips
    assert "wave_analysis.recovery_warmup" in decisions[1].skips
    assert decisions[2].features
    assert decisions[2].diagnostics["change_reasons"]["1m"] == (
        "confirmed",
        "provisional",
        "recovery_complete",
        "wave",
    )

    state = gapped.snapshot_state()["timeframes"]["1m"]
    assert state["segment_id"] == 1
    active_segment = state["segment_id"]
    active_pivots = [p for p in state["confirmed_pivots"] if p["metadata"]["segment_id"] == active_segment]
    assert [(p["kind"], p["price"], p["pivot_time"]) for p in active_pivots] == [
        ("high", "101", recovery[0].timestamp.isoformat()),
        ("low", "89", recovery[1].timestamp.isoformat()),
    ]


def test_fractal_detector_does_not_use_pre_gap_last_pivot() -> None:
    from wave_analysis.models import BarRecord
    from wave_analysis.pivots import append_bar, update_fractal_pivots

    config = WaveAnalysisConfig(
        timeframes=("1m",),
        primary_timeframe="1m",
        left_bars=1,
        right_bars=1,
        minimum_leg_bars=1,
    )
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT", segment_id=1)
    state.confirmed_pivots = [pivot(100, PivotKind.HIGH, "1000", 0)]
    rows = (
        ("95", "100", "90"),
        ("100", "110", "92"),
        ("100", "105", "93"),
    )
    confirmed = ()
    for index, (close, high, low) in enumerate(rows):
        timestamp = NOW + timedelta(minutes=200 + index)
        bar = BarRecord(
            timestamp=timestamp,
            close_time=timestamp + timedelta(minutes=1),
            open=Decimal(close),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
            volume=Decimal("100"),
        )
        append_bar(state, bar, config)
        confirmed, _ = update_fractal_pivots(state, config, bar)

    assert len(confirmed) == 1
    assert confirmed[0].kind is PivotKind.HIGH
    assert confirmed[0].metadata["segment_id"] == 1


def test_recovery_publication_does_not_expose_stale_pre_gap_current_wave() -> None:
    from wave_analysis.contracts import CURRENT_WAVE_FEATURE, MTF_CONTEXT_FEATURE, WAVE_LEGS_FEATURE

    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        recovery_bars=1,
    )
    history: list[Candle] = []
    for index, price in enumerate((100.0, 90.0, 110.0, 95.0, 120.0)):
        item = _candle(index + 300, price)
        history.append(item)
        module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))
    assert module.snapshot_state()["timeframes"]["1m"]["wave_legs"]

    gap = _candle(305, 80.0)
    history.append(gap)
    module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, len(history) - 1, data_gap=True))
    recovered = _candle(306, 100.0)
    history.append(recovered)
    decision = module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, len(history) - 1))

    by_key = {feature.key: feature.value for feature in decision.features}
    assert by_key[CURRENT_WAVE_FEATURE]["last_confirmed_wave"] is None
    assert by_key[WAVE_LEGS_FEATURE]["count"] > 0
    assert by_key[WAVE_LEGS_FEATURE]["active_segment_count"] == 0
    assert by_key[MTF_CONTEXT_FEATURE]["local_wave_direction"] is None
