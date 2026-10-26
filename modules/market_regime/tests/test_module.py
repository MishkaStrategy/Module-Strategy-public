from __future__ import annotations

import json
from datetime import timedelta

import pytest

from market_regime import (
    CURRENT_FEATURE_KEY,
    TREND_FEATURE_KEY,
    VOLATILITY_FEATURE_KEY,
    MarketRegimeDataError,
    MarketRegimeModule,
    SkipReason,
)

from conftest import candle, feature_trace, feed, view_for


def test_warmup_and_exact_first_publication(module: MarketRegimeModule, start) -> None:
    bars = [candle(start, i, 100 + i) for i in range(module.config.minimum_observations)]
    decisions = feed(module, bars)
    assert not decisions[-2].features
    assert decisions[-1].features
    assert decisions[-2].skips == (SkipReason.MODULE_NOT_READY.value,)


def test_exact_three_features_and_no_actions(module: MarketRegimeModule, start) -> None:
    decision = feed(module, [candle(start, i, 100 + i) for i in range(16)])[-1]
    assert {feature.key for feature in decision.features} == {
        CURRENT_FEATURE_KEY,
        TREND_FEATURE_KEY,
        VOLATILITY_FEATURE_KEY,
    }
    assert not decision.orders
    assert not decision.signals
    assert not decision.setups
    assert not decision.cancellations
    assert not decision.position_updates
    assert not decision.closes


def test_feature_timestamps_and_lifetime(module: MarketRegimeModule, start) -> None:
    bar = candle(start, 15, 115)
    decision = feed(module, [candle(start, i, 100 + i) for i in range(15)] + [bar])[-1]
    for feature in decision.features:
        assert feature.created_at == bar.close_time
        assert feature.valid_until == bar.close_time + timedelta(hours=1)
        assert feature.timeframe == "1h"
        assert feature.invalidate_on_gap is True


def test_feature_payload_deeply_immutable(module: MarketRegimeModule, start) -> None:
    feature = feed(module, [candle(start, i, 100 + i) for i in range(16)])[-1].features[0]
    with pytest.raises(TypeError):
        feature.value["regime"] = "bad"
    with pytest.raises(TypeError):
        feature.value["metrics"]["atr"] = -1


def test_feature_ids_deterministic(small_kwargs, start) -> None:
    bars = [candle(start, i, 100 + i) for i in range(20)]
    a = feature_trace(feed(MarketRegimeModule(**small_kwargs), bars))
    b = feature_trace(feed(MarketRegimeModule(**small_kwargs), bars))
    assert a == b


def test_future_data_invariance(small_kwargs, start) -> None:
    prefix = [candle(start, i, 100 + i) for i in range(30)]
    future = [candle(start, 30 + i, 1_000_000 if i % 2 else 1) for i in range(10)]
    prefix_trace = feature_trace(feed(MarketRegimeModule(**small_kwargs), prefix))
    extended_trace = feature_trace(feed(MarketRegimeModule(**small_kwargs), prefix + future))
    assert extended_trace[: len(prefix)] == prefix_trace


def test_empty_view_is_safe(module: MarketRegimeModule) -> None:
    from modular_core import MarketView

    view = MarketView(
        base_bars=(),
        timeframe_bars={},
        index=0,
        cash=1,
        equity=1,
        position=None,
        pending_orders=(),
    )
    assert module.on_bar(view).skips == (SkipReason.EMPTY_VIEW.value,)


def test_no_target_is_safe_and_binds_symbol(module: MarketRegimeModule, start) -> None:
    base = candle(start, 0, 100, timeframe="5m", minutes=5)
    decision = module.on_bar(view_for(base))
    state = module.snapshot_state()
    assert decision.skips == (SkipReason.NO_TARGET_BAR.value,)
    assert state["bound_symbol"] == "BTCUSDT"
    assert state["bound_base_timeframe"] == "5m"


def test_incomplete_target_not_used(module: MarketRegimeModule, start) -> None:
    target = candle(start, 0, 100, timeframe="1h")
    base = candle(start, 0, 100, timeframe="5m", minutes=5)
    decision = module.on_bar(view_for(base, target=target))
    assert decision.skips == (SkipReason.INCOMPLETE_BAR.value,)
    assert module.snapshot_state()["processed_bars"] == 0


def test_alias_timeframe_is_canonicalized(module: MarketRegimeModule, start) -> None:
    bars = [candle(start, i, 100 + i, timeframe="1H") for i in range(16)]
    decision = feed(module, bars)[-1]
    assert decision.features
    assert {feature.timeframe for feature in decision.features} == {"1h"}


def test_conflicting_alias_target_histories_rejected(module: MarketRegimeModule, start) -> None:
    base = candle(start, 12, 112, timeframe="5m", minutes=5)
    one = candle(start, 0, 100, timeframe="1h")
    other = candle(start, 0, 101, timeframe="1H")
    with pytest.raises(MarketRegimeDataError, match="conflicting target histories"):
        module.on_bar(view_for(base, target=one, aliases={"1H": (other,)}))


def test_exact_duplicate_base_is_idempotent(module: MarketRegimeModule, start) -> None:
    bar = candle(start, 0, 100)
    first = module.on_bar(view_for(bar))
    before = dict(module.snapshot_state())
    second = module.on_bar(view_for(bar))
    after = dict(module.snapshot_state())
    assert first.skips
    assert second.skips == (SkipReason.DUPLICATE_BAR.value,)
    assert after["processed_bars"] == before["processed_bars"]
    assert after["duplicate_events"] == before["duplicate_events"] + 1


def test_conflicting_duplicate_base_rejected(module: MarketRegimeModule, start) -> None:
    module.on_bar(view_for(candle(start, 0, 100)))
    with pytest.raises(MarketRegimeDataError, match="conflicting duplicate base"):
        module.on_bar(view_for(candle(start, 0, 101)))


def test_out_of_order_base_rejected(module: MarketRegimeModule, start) -> None:
    module.on_bar(view_for(candle(start, 1, 101)))
    with pytest.raises(MarketRegimeDataError, match="out-of-order base"):
        module.on_bar(view_for(candle(start, 0, 100)))


def test_duplicate_gap_does_not_reset(module: MarketRegimeModule, start) -> None:
    bar = candle(start, 0, 100)
    module.on_bar(view_for(bar))
    before = dict(module.snapshot_state())
    decision = module.on_bar(view_for(bar, data_gap=True))
    after = dict(module.snapshot_state())
    assert decision.skips == (SkipReason.DUPLICATE_BAR.value,)
    assert after["data_gap_resets"] == before["data_gap_resets"]
    assert after["processed_bars"] == before["processed_bars"]


def test_out_of_order_gap_does_not_reset(module: MarketRegimeModule, start) -> None:
    module.on_bar(view_for(candle(start, 1, 101)))
    before = dict(module.snapshot_state())
    with pytest.raises(MarketRegimeDataError, match="out-of-order base"):
        module.on_bar(view_for(candle(start, 0, 100), data_gap=True))
    assert module.snapshot_state() == before


def test_new_gap_resets_and_can_rebind_base_timeframe(module: MarketRegimeModule, start) -> None:
    module.on_bar(view_for(candle(start, 0, 100, timeframe="5m", minutes=5)))
    changed = candle(start, 1, 101, timeframe="15m", minutes=15)
    decision = module.on_bar(view_for(changed, data_gap=True))
    state = module.snapshot_state()
    assert decision.skips == (SkipReason.DATA_GAP_RESET.value,)
    assert state["bound_base_timeframe"] == "15m"
    assert state["processed_bars"] == 0
    assert state["data_gap_resets"] == 1


def test_base_timeframe_change_without_gap_rejected(module: MarketRegimeModule, start) -> None:
    module.on_bar(view_for(candle(start, 0, 100, timeframe="5m", minutes=5)))
    with pytest.raises(MarketRegimeDataError, match="bound to base timeframe"):
        module.on_bar(view_for(candle(start, 1, 101, timeframe="15m", minutes=15)))


def test_symbol_change_rejected_even_across_gap(module: MarketRegimeModule, start) -> None:
    module.on_bar(view_for(candle(start, 0, 100, symbol="BTCUSDT")))
    with pytest.raises(MarketRegimeDataError, match="one MarketRegimeModule instance"):
        module.on_bar(view_for(candle(start, 1, 100, symbol="ETHUSDT"), data_gap=True))


def test_repeated_target_on_new_base_is_noop(module: MarketRegimeModule, start) -> None:
    target = candle(start, 0, 100)
    base0 = candle(start, 12, 100, timeframe="5m", minutes=5)
    module.on_bar(view_for(base0, target=target))
    before = dict(module.snapshot_state())
    base1 = candle(start, 13, 100, timeframe="5m", minutes=5)
    decision = module.on_bar(view_for(base1, target=target))
    after = dict(module.snapshot_state())
    assert decision.skips == (SkipReason.STALE_TARGET.value,)
    assert decision.diagnostics["reason"] == SkipReason.STALE_TARGET.value
    assert after["processed_bars"] == before["processed_bars"]
    assert after["last_open_time"] == before["last_open_time"]


def test_target_timeframe_gap_resets(module: MarketRegimeModule, start) -> None:
    for i in range(5):
        module.on_bar(view_for(candle(start, i, 100 + i)))
    gap = candle(start, 7, 107)
    decision = module.on_bar(view_for(gap))
    state = module.snapshot_state()
    assert decision.skips == (SkipReason.TIMEFRAME_GAP_RESET.value,)
    assert state["processed_bars"] == 1
    assert state["timeframe_gap_resets"] == 1


def test_diagnostics_toggle_does_not_change_trace_or_state(small_kwargs, start) -> None:
    bars = [candle(start, i, 100 + i * 0.5) for i in range(30)]
    verbose = MarketRegimeModule(**small_kwargs, publish_diagnostics=True)
    quiet = MarketRegimeModule(**small_kwargs, publish_diagnostics=False)
    verbose_trace = feature_trace(feed(verbose, bars))
    quiet_trace = feature_trace(feed(quiet, bars))
    assert verbose_trace == quiet_trace
    assert verbose.snapshot_state() == quiet.snapshot_state()
    assert any(
        decision.diagnostics for decision in feed(MarketRegimeModule(**small_kwargs), bars[:1])
    )


def test_snapshot_is_json_serializable(module: MarketRegimeModule, start) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    json.dumps(module.snapshot_state(), sort_keys=True)
