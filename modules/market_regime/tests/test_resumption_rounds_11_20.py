from __future__ import annotations

import copy

import pytest
from modular_core import FeatureStore, ModuleRegistry

from market_regime import MarketRegimeDataError, MarketRegimeModule, MarketRegimeStateError

from conftest import candle, feature_trace, feed, view_for


def test_round_11_hysteresis_candidate_progression_is_explicit(start) -> None:
    module = MarketRegimeModule(
        timeframe="1h",
        fast_ema_period=2,
        slow_ema_period=4,
        atr_period=3,
        volatility_baseline_period=5,
        efficiency_period=3,
        slope_lookback=2,
        regime_confirmation_bars=3,
        volatility_confirmation_bars=3,
    )
    ready = module.config.indicator_ready_observations
    feed(module, [candle(start, index, 100 + index) for index in range(ready)])
    first = module.snapshot_state()
    assert first["candidate_regime_count"] == 1
    assert first["candidate_volatility_count"] == 1
    second_decision = module.on_bar(view_for(candle(start, ready, 100 + ready)))
    second = module.snapshot_state()
    assert not second_decision.features
    assert second["candidate_regime_count"] == 2
    final_decision = module.on_bar(view_for(candle(start, ready + 1, 101 + ready)))
    final = module.snapshot_state()
    assert final_decision.features
    assert final["candidate_regime_count"] == 0
    assert final["candidate_volatility_count"] == 0


def test_round_12_gap_reset_clears_runtime_but_preserves_health_counters(
    small_kwargs, start
) -> None:
    module = MarketRegimeModule(**small_kwargs)
    bars = [candle(start, index, 100 + index) for index in range(20)]
    feed(module, bars)
    module.on_bar(view_for(bars[-1]))
    before = module.snapshot_state()
    assert before["publications"] > 0
    assert before["duplicate_events"] == 1
    decision = module.on_bar(view_for(candle(start, 21, 121), data_gap=True))
    after = module.snapshot_state()
    assert decision.skips
    assert after["processed_bars"] == 0
    assert after["candidate_regime_count"] == 0
    assert after["candidate_volatility_count"] == 0
    assert after["publications"] == before["publications"]
    assert after["duplicate_events"] == before["duplicate_events"]
    assert after["data_gap_resets"] == before["data_gap_resets"] + 1


@pytest.mark.parametrize(
    "field", ["last_base_open_time", "last_base_fingerprint", "last_open_time", "last_fingerprint"]
)
def test_round_13_partial_checkpoint_identity_is_rejected_atomically(
    small_kwargs, start, field: str
) -> None:
    module = MarketRegimeModule(**small_kwargs)
    feed(module, [candle(start, index, 100 + index) for index in range(12)])
    before = dict(module.snapshot_state())
    corrupted = dict(before)
    corrupted[field] = None
    with pytest.raises(MarketRegimeStateError):
        module.restore_state(corrupted)
    assert module.snapshot_state() == before


def test_round_14_restore_detaches_from_caller_owned_checkpoint_lists(small_kwargs, start) -> None:
    source = MarketRegimeModule(**small_kwargs)
    feed(source, [candle(start, index, 100 + index) for index in range(12)])
    payload = copy.deepcopy(dict(source.snapshot_state()))
    restored = MarketRegimeModule(**small_kwargs)
    restored.restore_state(payload)
    before = copy.deepcopy(dict(restored.snapshot_state()))
    for key in ("tr_seed", "closes", "movements", "slow_emas", "atr_pct_window"):
        payload[key].append("999")
    assert restored.snapshot_state() == before


def test_round_15_conflicting_target_rejection_is_transactional(small_kwargs, start) -> None:
    module = MarketRegimeModule(**small_kwargs)
    target = candle(start, 0, 100, timeframe="1h")
    first_base = candle(start, 11, 100, timeframe="5m", minutes=5)
    module.on_bar(view_for(first_base, target=target))
    before = copy.deepcopy(dict(module.snapshot_state()))
    next_base = candle(start, 12, 100, timeframe="5m", minutes=5)
    conflicting = candle(start, 0, 101, timeframe="1h")
    with pytest.raises(MarketRegimeDataError, match="conflicting duplicate target"):
        module.on_bar(view_for(next_base, target=conflicting))
    assert module.snapshot_state() == before
    corrected = module.on_bar(view_for(next_base, target=target))
    assert corrected.skips
    assert module.snapshot_state()["last_base_open_time"] == next_base.timestamp.isoformat()


def test_round_16_out_of_order_target_rejection_is_transactional(small_kwargs, start) -> None:
    module = MarketRegimeModule(**small_kwargs)
    current_target = candle(start, 1, 101, timeframe="1h")
    first_base = candle(start, 23, 101, timeframe="5m", minutes=5)
    module.on_bar(view_for(first_base, target=current_target))
    before = copy.deepcopy(dict(module.snapshot_state()))
    next_base = candle(start, 24, 101, timeframe="5m", minutes=5)
    older_target = candle(start, 0, 100, timeframe="1h")
    with pytest.raises(MarketRegimeDataError, match="out-of-order 1h bar"):
        module.on_bar(view_for(next_base, target=older_target))
    assert module.snapshot_state() == before
    corrected = module.on_bar(view_for(next_base, target=current_target))
    assert corrected.skips
    assert module.snapshot_state()["last_base_open_time"] == next_base.timestamp.isoformat()


def test_round_17_core_registry_loads_public_import_path() -> None:
    loaded = ModuleRegistry().load_import_path(
        "market_regime:MarketRegimeModule", {"timeframe": "4h", "publish_diagnostics": False}
    )
    assert isinstance(loaded, MarketRegimeModule)
    assert loaded.manifest.required_timeframes == ("4h",)
    assert loaded.parameters["publish_diagnostics"] is False


def test_round_18_core_config_loader_and_planner_resolve_exact_contract() -> None:
    registry = ModuleRegistry()
    modules = registry.load_config(
        {
            "modules": [
                {
                    "class": "market_regime:MarketRegimeModule",
                    "parameters": {"timeframe": "4h", "publish_diagnostics": False},
                },
                {"class": "market_regime:MarketRegimeModule", "enabled": False},
            ]
        }
    )
    plan = registry.validate_and_plan(modules, base_timeframe="5m")
    assert len(plan.modules) == 1
    assert plan.required_timeframes == ("4h",)
    assert set(plan.produced_by) == {
        "market_regime.current",
        "market_regime.trend",
        "market_regime.volatility",
    }


def test_round_19_downstream_feature_store_reads_all_owned_keys_same_bar(
    small_kwargs, start
) -> None:
    decision = feed(
        MarketRegimeModule(**small_kwargs),
        [candle(start, index, 100 + index) for index in range(20)],
    )[-1]
    store = FeatureStore()
    for feature in decision.features:
        store.publish(feature)
    snapshot = store.snapshot(decision.features[0].created_at)
    values = {
        key: snapshot.get(key, symbol="BTCUSDT", timeframe="1h")
        for key in ("market_regime.current", "market_regime.trend", "market_regime.volatility")
    }
    assert all(values.values())
    assert values["market_regime.current"]["trend"] == values["market_regime.trend"]["direction"]
    assert (
        values["market_regime.current"]["volatility"] == values["market_regime.volatility"]["level"]
    )


def test_round_20_interleaved_instances_and_single_restart_remain_isolated(
    small_kwargs, start
) -> None:
    btc_bars = [candle(start, index, 100 + index, symbol="BTCUSDT") for index in range(24)]
    eth_bars = [candle(start, index, 200 + index, symbol="ETHUSDT") for index in range(24)]
    expected_btc = feature_trace(feed(MarketRegimeModule(**small_kwargs), btc_bars))
    expected_eth = feature_trace(feed(MarketRegimeModule(**small_kwargs), eth_bars))
    btc = MarketRegimeModule(**small_kwargs)
    eth = MarketRegimeModule(**small_kwargs)
    btc_decisions = []
    eth_decisions = []
    for index, (btc_bar, eth_bar) in enumerate(zip(btc_bars, eth_bars, strict=True)):
        btc_decisions.append(btc.on_bar(view_for(btc_bar)))
        eth_decisions.append(eth.on_bar(view_for(eth_bar)))
        if index == 10:
            restored = MarketRegimeModule(**small_kwargs)
            restored.restore_state(btc.snapshot_state())
            btc = restored
    assert feature_trace(btc_decisions) == expected_btc
    assert feature_trace(eth_decisions) == expected_eth
