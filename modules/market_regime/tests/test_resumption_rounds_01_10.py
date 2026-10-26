from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta, timezone
from decimal import Decimal
from math import isfinite

import pytest

from market_regime import MarketRegimeConfig, MarketRegimeModule
from market_regime.enums import Regime, TrendDirection
from market_regime.indicators import classify_regime

from conftest import candle, feature_trace, feed, view_for


@pytest.mark.parametrize("raw", ["0.1", "0.10", "1e-1", Decimal("0.1000")])
def test_round_01_equivalent_decimal_inputs_share_semantic_identity(raw: object) -> None:
    baseline = MarketRegimeConfig(trend_spread_threshold="0.1")
    candidate = MarketRegimeConfig(trend_spread_threshold=raw)
    assert candidate.signature == baseline.signature
    assert candidate.semantic_parameters() == baseline.semantic_parameters()


def test_round_02_classifier_symmetry_holds_across_threshold_grid() -> None:
    config = MarketRegimeConfig()
    magnitudes = (Decimal("0"), Decimal("0.0001"), Decimal("0.001"), Decimal("0.01"))
    efficiencies = (Decimal("0"), Decimal("0.22"), Decimal("0.35"), Decimal("1"))
    for spread in magnitudes:
        for slope in magnitudes:
            for efficiency in efficiencies:
                positive = classify_regime(spread, slope, efficiency, config)
                negative = classify_regime(-spread, -slope, efficiency, config)
                if positive[1] is Regime.TREND_UP:
                    assert negative == (TrendDirection.BEARISH, Regime.TREND_DOWN)
                elif negative[1] is Regime.TREND_DOWN:
                    assert positive == (TrendDirection.BULLISH, Regime.TREND_UP)
                else:
                    assert positive[1] is negative[1]
                    assert positive[0] is negative[0] is TrendDirection.NEUTRAL


def test_round_03_equivalent_utc_instants_produce_identical_trace(small_kwargs, start) -> None:
    shifted_start = start.astimezone(timezone(timedelta(hours=7)))
    utc_bars = [candle(start, index, 100 + index) for index in range(20)]
    shifted_bars = [candle(shifted_start, index, 100 + index) for index in range(20)]
    utc_trace = feature_trace(feed(MarketRegimeModule(**small_kwargs), utc_bars))
    shifted_trace = feature_trace(feed(MarketRegimeModule(**small_kwargs), shifted_bars))
    assert shifted_trace == utc_trace


def test_round_04_target_is_available_exactly_at_completed_close(start) -> None:
    module = MarketRegimeModule(
        timeframe="1h",
        fast_ema_period=2,
        slow_ema_period=4,
        atr_period=3,
        volatility_baseline_period=5,
        efficiency_period=3,
        slope_lookback=2,
        regime_confirmation_bars=1,
        volatility_confirmation_bars=1,
    )
    target = candle(start, 0, 100, timeframe="1h")
    decision_base = candle(start, 11, 100, timeframe="5m", minutes=5)
    decision = module.on_bar(view_for(decision_base, target=target))
    assert decision.skips
    assert module.snapshot_state()["processed_bars"] == 1
    assert module.snapshot_state()["last_open_time"] == target.timestamp.isoformat()


def test_round_05_equivalent_alias_histories_accept_same_target(start) -> None:
    module = MarketRegimeModule(
        timeframe="1h",
        fast_ema_period=2,
        slow_ema_period=4,
        atr_period=3,
        volatility_baseline_period=5,
        efficiency_period=3,
        slope_lookback=2,
        regime_confirmation_bars=1,
        volatility_confirmation_bars=1,
    )
    target = candle(start, 0, 100, timeframe="1h")
    base = candle(start, 11, 100, timeframe="5m", minutes=5)
    decision = module.on_bar(view_for(base, target=target, aliases={" 1H ": (target,)}))
    assert decision.skips
    assert module.snapshot_state()["processed_bars"] == 1


def test_round_06_unrelated_invalid_timeframe_history_cannot_shadow_target(start) -> None:
    module = MarketRegimeModule(
        timeframe="1h",
        fast_ema_period=2,
        slow_ema_period=4,
        atr_period=3,
        volatility_baseline_period=5,
        efficiency_period=3,
        slope_lookback=2,
        regime_confirmation_bars=1,
        volatility_confirmation_bars=1,
    )
    target = candle(start, 0, 100, timeframe="1h")
    unrelated = candle(start, 0, 999, timeframe="5m", minutes=5)
    base = candle(start, 11, 100, timeframe="5m", minutes=5)
    decision = module.on_bar(
        view_for(base, target=target, aliases={"not-a-timeframe": (unrelated,)})
    )
    assert decision.skips
    assert module.snapshot_state()["last_open_time"] == target.timestamp.isoformat()


def test_round_07_published_feature_payloads_are_json_scalars_and_finite(
    small_kwargs, start
) -> None:
    decision = feed(
        MarketRegimeModule(**small_kwargs),
        [candle(start, index, 100 + index * 0.5) for index in range(20)],
    )[-1]
    assert decision.features

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            for nested in value.values():
                walk(nested)
        elif isinstance(value, float):
            assert isfinite(value)
        else:
            assert value is None or isinstance(value, (str, int, bool))

    for feature in decision.features:
        walk(feature.value)


def test_round_08_feature_identity_survives_restart_and_diagnostics_toggle(
    small_kwargs, start
) -> None:
    bars = [candle(start, index, 100 + index * 0.25) for index in range(30)]
    uninterrupted = feature_trace(
        feed(MarketRegimeModule(**small_kwargs, publish_diagnostics=True), bars)
    )
    prefix_module = MarketRegimeModule(**small_kwargs, publish_diagnostics=True)
    prefix = feed(prefix_module, bars[:13])
    restored = MarketRegimeModule(**small_kwargs, publish_diagnostics=False)
    restored.restore_state(prefix_module.snapshot_state())
    suffix = feed(restored, bars[13:])
    assert feature_trace(prefix + suffix) == uninterrupted


def test_round_09_unknown_constructor_fields_fail_closed() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        MarketRegimeModule(unknown_parameter=1)


def test_round_10_all_decimal_configuration_fields_have_canonical_identity() -> None:
    baseline = MarketRegimeModule()
    equivalent = MarketRegimeModule(
        trend_spread_threshold="1e-3",
        trend_slope_threshold="15e-5",
        trend_efficiency_threshold="0.3500",
        range_efficiency_threshold="22e-2",
        low_volatility_ratio="0.750",
        high_volatility_ratio="135e-2",
    )
    assert equivalent.config.signature == baseline.config.signature
    assert equivalent.config.semantic_parameters() == baseline.config.semantic_parameters()
    assert equivalent.parameters == baseline.parameters
