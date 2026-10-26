from __future__ import annotations

import copy
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest
from modular_core import Candle, Feature, FeatureView, MarketView

from wave_analysis import (
    CONFIRMED_PIVOTS_FEATURE,
    HYPOTHESES_FEATURE,
    MTF_CONTEXT_FEATURE,
    PROVISIONAL_PIVOT_FEATURE,
    PivotKind,
    WaveAnalysisConfigurationError,
    WaveAnalysisDataError,
    WaveAnalysisModule,
    WaveAnalysisStateError,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def bar(
    index: int,
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    volume: float = 100.0,
    timeframe: str = "1m",
    symbol: str = "BTCUSDT",
    minute_step: int = 1,
) -> Candle:
    open_value = close if open_ is None else open_
    high_value = max(open_value, close) + 0.5 if high is None else high
    low_value = min(open_value, close) - 0.5 if low is None else low
    return Candle(
        START + timedelta(minutes=index * minute_step),
        open_value,
        high_value,
        low_value,
        close,
        volume,
        symbol,
        timeframe,
    )


def view(history: list[Candle], *, timeframe_bars=None, gap: bool = False, features=None) -> MarketView:
    return MarketView(
        tuple(history),
        timeframe_bars or {history[-1].timeframe: tuple(history)},
        len(history) - 1,
        cash=0.0,
        equity=0.0,
        position=None,
        pending_orders=(),
        data_gap=gap,
        features=features,
    )


def feed(module: WaveAnalysisModule, candles: Iterable[Candle]):
    history: list[Candle] = []
    decisions = []
    for candle in candles:
        history.append(candle)
        decisions.append(module.on_bar(view(history)))
    return decisions


def tf_state(module: WaveAnalysisModule, timeframe: str = "1m") -> dict:
    return dict(module.snapshot_state()["timeframes"][timeframe])


def confirmed(module: WaveAnalysisModule, timeframe: str = "1m") -> list[dict]:
    return list(tf_state(module, timeframe)["confirmed_pivots"])


def waves(module: WaveAnalysisModule, timeframe: str = "1m") -> list[dict]:
    return list(tf_state(module, timeframe)["wave_legs"])


def fractal_series() -> list[Candle]:
    return [
        bar(0, close=100, high=101, low=99),
        bar(1, close=102, high=103, low=100),
        bar(2, close=108, high=110, low=106),
        bar(3, close=104, high=105, low=103),
        bar(4, close=103, high=104, low=102),
        bar(5, close=99, high=101, low=97),
        bar(6, close=95, high=98, low=94),
        bar(7, close=99, high=100, low=97),
        bar(8, close=101, high=102, low=99),
    ]


def zigzag_series() -> list[Candle]:
    prices = [100, 95, 105, 110, 103, 120, 112, 130, 121, 140, 128, 150, 135]
    return [bar(i, close=p, high=p + 1, low=p - 1, volume=100 + i * 10) for i, p in enumerate(prices)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeframes": (), "primary_timeframe": "1m"},
        {"timeframes": ("1m",), "primary_timeframe": "5m"},
        {"timeframes": ("1m",), "primary_timeframe": "1m", "reversal_threshold": "0"},
        {"timeframes": ("1m",), "primary_timeframe": "1m", "maximum_retained_pivots": 10, "maximum_retained_waves": 10},
    ],
)
def test_config_rejects_invalid_values(kwargs) -> None:
    with pytest.raises(WaveAnalysisConfigurationError):
        WaveAnalysisModule(**kwargs)


def test_fractal_pivot_delayed_confirmation() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=2, right_bars=2)
    candles = fractal_series()[:5]
    feed(module, candles[:4])
    assert confirmed(module) == []
    decision = module.on_bar(view(candles))
    pivot = confirmed(module)[0]
    assert pivot["kind"] == PivotKind.HIGH.value
    assert pivot["pivot_time"] == candles[2].timestamp.isoformat()
    assert pivot["confirmed_at"] == candles[4].close_time.isoformat()
    feature = next(item for item in decision.features if item.key == CONFIRMED_PIVOTS_FEATURE)
    assert feature.created_at == candles[4].close_time


def test_last_bar_not_confirmed() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=1, right_bars=2)
    feed(module, [bar(0, close=100), bar(1, close=102), bar(2, close=108, high=120, low=107)])
    assert confirmed(module) == []
    assert tf_state(module)["provisional"]["status"] == "provisional"


def test_future_data_invariance() -> None:
    candles = fractal_series()
    prefix = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=2, right_bars=2)
    feed(prefix, candles[:5])
    expected = confirmed(prefix)
    cutoff = candles[4].close_time.isoformat()
    full = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=2, right_bars=2)
    feed(full, candles)
    assert [p for p in confirmed(full) if p["confirmed_at"] <= cutoff] == expected


def test_duplicate_idempotent_digest() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    candle = bar(0, close=100)
    module.on_bar(view([candle]))
    before = module.state_digest
    result = module.on_bar(view([candle]))
    assert result.skips == ("wave_analysis.duplicate_bar",)
    assert module.state_digest == before


def test_conflicting_duplicate_and_out_of_order_fail_closed() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    first = bar(2, close=100)
    module.on_bar(view([first]))
    before = confirmed(module)
    with pytest.raises(WaveAnalysisDataError):
        module.on_bar(view([bar(2, close=101, high=102, low=99)]))
    assert confirmed(module) == before
    with pytest.raises(WaveAnalysisDataError):
        module.on_bar(view([bar(1, close=99)]))


def test_percentage_zigzag_and_wave_legs() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",), primary_timeframe="1m", pivot_method="percentage_zigzag", reversal_threshold="0.05"
    )
    feed(module, zigzag_series()[:5])
    assert [item["kind"] for item in confirmed(module)] == ["high", "low", "high"]
    assert [item["direction"] for item in waves(module)] == ["down", "up"]


@pytest.mark.parametrize("method", ["atr_zigzag", "hybrid_adaptive"])
def test_atr_detectors_use_completed_atr(method: str) -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method=method,
        atr_period=2,
        atr_multiplier="1",
        reversal_threshold="0.02",
    )
    feed(module, zigzag_series()[:2])
    assert tf_state(module)["atr"] is not None
    assert all(p["confirmed_at"] <= zigzag_series()[1].close_time.isoformat() for p in confirmed(module))


@pytest.mark.parametrize("policy,expected", [("earliest", ["high"]), ("reject", []), ("latest", [])])
def test_equal_high_policy(policy: str, expected: list[str]) -> None:
    candles = [
        bar(0, close=100, high=100, low=95),
        bar(1, close=105, high=110, low=100),
        bar(2, close=104, high=110, low=101),
    ]
    module = WaveAnalysisModule(
        timeframes=("1m",), primary_timeframe="1m", left_bars=1, right_bars=1, tie_policy=policy
    )
    feed(module, candles)
    assert [item["kind"] for item in confirmed(module)] == expected


def test_flat_market_reject_ties() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",), primary_timeframe="1m", left_bars=1, right_bars=1, tie_policy="reject"
    )
    feed(module, [bar(i, close=100, high=101, low=99) for i in range(12)])
    assert confirmed(module) == [] and waves(module) == []


def test_gap_preserves_confirmed_and_prevents_cross_gap_wave() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        recovery_bars=2,
    )
    history = zigzag_series()[:5]
    feed(module, history)
    old_pivots = copy.deepcopy(confirmed(module))
    old_waves = len(waves(module))
    gap_bar = bar(5, close=80, high=81, low=79)
    module.on_bar(view([*history, gap_bar], gap=True))
    state = tf_state(module)
    assert state["confirmed_pivots"] == old_pivots
    assert state["provisional"] is None
    for item in [
        bar(6, close=82, high=83, low=81),
        bar(7, close=90, high=91, low=89),
        bar(8, close=83, high=84, low=82),
    ]:
        history.append(item)
        module.on_bar(view(history))
    assert all(w["start_time"] > gap_bar.timestamp.isoformat() for w in waves(module)[old_waves:])


def test_checkpoint_restore_identical() -> None:
    candles = zigzag_series()
    kwargs = dict(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        elliott_enabled=True,
    )
    uninterrupted = WaveAnalysisModule(**kwargs)
    feed(uninterrupted, candles)
    first = WaveAnalysisModule(**kwargs)
    history = []
    for item in candles[:6]:
        history.append(item)
        first.on_bar(view(history))
    restored = WaveAnalysisModule(**kwargs)
    restored.restore_state(first.snapshot_state())
    for item in candles[6:]:
        history.append(item)
        restored.on_bar(view(history))
    assert restored.state_digest == uninterrupted.state_digest
    assert confirmed(restored) == confirmed(uninterrupted)


def test_restore_rejects_mismatch_and_corruption() -> None:
    source = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    feed(source, fractal_series()[:3])
    snapshot = dict(source.snapshot_state())
    with pytest.raises(WaveAnalysisStateError):
        WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", minimum_leg_bars=2).restore_state(snapshot)
    bad = dict(snapshot)
    bad["unknown"] = 1
    with pytest.raises(WaveAnalysisStateError):
        source.restore_state(bad)


def test_elliott_disabled_and_bounded() -> None:
    off = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.04",
        elliott_enabled=False,
    )
    decisions = feed(off, zigzag_series())
    assert tf_state(off)["hypotheses"] == []
    assert all(f.value["items"] == () for d in decisions for f in d.features if f.key == HYPOTHESES_FEATURE)
    on = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.04",
        elliott_enabled=True,
        maximum_hypotheses=1,
        hypothesis_min_confidence="0",
    )
    feed(on, zigzag_series())
    assert len(tf_state(on)["hypotheses"]) <= 1


def test_analytical_only_and_namespaced_features() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=1, right_bars=1)
    decisions = feed(module, fractal_series())
    features = [f for d in decisions for f in d.features]
    assert features and all(f.key.startswith("wave_analysis.") for f in features)
    assert any(f.key == PROVISIONAL_PIVOT_FEATURE for f in features)
    assert all(not d.orders and not d.signals and not d.setups for d in decisions)


def test_bounded_state_long_stream() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.01",
        maximum_retained_bars=16,
        maximum_retained_pivots=8,
        maximum_retained_waves=7,
    )
    feed(module, [bar(i, close=100 + (10 if i % 2 else -10), high=112, low=88) for i in range(100)])
    state = tf_state(module)
    assert len(state["bars"]) <= 16
    assert len(state["confirmed_pivots"]) <= 8
    assert len(state["wave_legs"]) <= 7


def test_multi_timeframe_completed_gate_and_context() -> None:
    module = WaveAnalysisModule(timeframes=("5m", "15m"), primary_timeframe="5m", left_bars=1, right_bars=1)
    htf0 = bar(0, close=100, timeframe="15m", minute_step=15)
    history = []
    decisions = []
    for i, price in enumerate((100, 110, 100)):
        base = bar(i, close=price, timeframe="5m", minute_step=5)
        history.append(base)
        decisions.append(module.on_bar(view(history, timeframe_bars={"5m": tuple(history), "15m": (htf0,)})))
    assert tf_state(module, "15m")["processed_bars"] == 1
    assert any(f.key == MTF_CONTEXT_FEATURE for decision in decisions for f in decision.features)


def test_same_history_same_digest_and_ids() -> None:
    candles = fractal_series()
    a = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=2, right_bars=2)
    b = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=2, right_bars=2)
    feed(a, candles)
    feed(b, candles)
    assert a.state_digest == b.state_digest
    assert confirmed(a) == confirmed(b)


def test_optional_regime_feature_is_read_only_context() -> None:
    regime = Feature(
        "market_regime.current", "market_regime", START, {"regime": "trend"}, symbol="BTCUSDT", timeframe="1m"
    )
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=1, right_bars=1)
    candles = fractal_series()[:3]
    history = []
    decision = None
    for candle in candles:
        history.append(candle)
        decision = module.on_bar(view(history, features=FeatureView([regime], candle.close_time)))
    assert decision is not None
    assert any(f.metadata["optional_market_regime"]["regime"] == "trend" for f in decision.features)
