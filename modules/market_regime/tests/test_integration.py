from __future__ import annotations

from pathlib import Path

from modular_core import FeatureStore, ModuleRegistry, StrategyModule
from market_regime import (
    CURRENT_FEATURE_KEY,
    MODULE_VERSION,
    MarketRegimeModule,
)

from conftest import candle, feed, view_for


def test_real_strategy_module_contract(module: MarketRegimeModule) -> None:
    assert isinstance(module, StrategyModule)
    assert module.manifest is not None
    assert module.manifest.version == MODULE_VERSION == "1.2.0"
    assert module.manifest.state_schema_version == "2"
    assert module.manifest.api_version == "1.0"


def test_registry_plans_module(module: MarketRegimeModule) -> None:
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    assert plan.modules == (module,)
    assert plan.produced_by[CURRENT_FEATURE_KEY] == module.name
    assert plan.required_timeframes == ("1h",)


def test_manifest_parameters_match_resolved_config(module: MarketRegimeModule) -> None:
    assert set(module.parameters) == set(module.manifest.parameters_schema)
    assert module.manifest.warmup_bars["1h"] == module.config.minimum_observations


def test_feature_store_same_bar_visibility(module: MarketRegimeModule, start) -> None:
    decision = feed(module, [candle(start, i, 100 + i) for i in range(16)])[-1]
    store = FeatureStore()
    for feature in decision.features:
        store.publish(feature)
    snapshot = store.snapshot(decision.features[0].created_at)
    value = snapshot.get(CURRENT_FEATURE_KEY, symbol="BTCUSDT", timeframe="1h")
    assert value["regime"] == "trend_up"


def test_feature_store_gap_invalidation(module: MarketRegimeModule, start) -> None:
    decision = feed(module, [candle(start, i, 100 + i) for i in range(16)])[-1]
    store = FeatureStore()
    for feature in decision.features:
        store.publish(feature)
    assert len(store.invalidate_on_gap(symbol="BTCUSDT")) == 3


def test_parallel_instances_isolated(small_kwargs, start) -> None:
    btc = MarketRegimeModule(**small_kwargs)
    eth = MarketRegimeModule(**small_kwargs)
    btc_decision = feed(btc, [candle(start, i, 100 + i, symbol="BTCUSDT") for i in range(16)])[-1]
    eth_decision = feed(eth, [candle(start, i, 200 + i, symbol="ETHUSDT") for i in range(16)])[-1]
    assert {feature.symbol for feature in btc_decision.features} == {"BTCUSDT"}
    assert {feature.symbol for feature in eth_decision.features} == {"ETHUSDT"}


def test_base_timeframe_can_differ(module: MarketRegimeModule, start) -> None:
    final = None
    for i in range(16):
        target = candle(start, i, 100 + i, timeframe="1h")
        base = candle(start, 12 * (i + 1), 100 + i, timeframe="5m", minutes=5)
        final = module.on_bar(view_for(base, target=target))
    assert final is not None and final.features


def test_entry_point_declared_in_project_metadata() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    assert 'market_regime = "market_regime:MarketRegimeModule"' in pyproject.read_text()


def test_no_hidden_execution_outputs(module: MarketRegimeModule, start) -> None:
    for decision in feed(module, [candle(start, i, 100 + i) for i in range(20)]):
        assert not decision.orders
        assert not decision.cancellations
        assert not decision.position_updates
        assert not decision.closes
        assert not decision.signals
        assert not decision.setups
