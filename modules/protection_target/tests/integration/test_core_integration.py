from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from modular_core import (
    Candle,
    Feature,
    FeatureStore,
    FeatureView,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)

from modular_strategy_protection_target import ProtectionTargetModule


class StubEntryModule(StrategyModule):
    name = "entry_model"
    parameters = MappingProxyType({})
    manifest = ModuleManifest(
        name=name,
        version="1.0.0",
        api_version="1.0",
        produced_features=("entry_model.entry_plan",),
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        bar = view.current
        return ModuleDecision(
            features=(
                Feature(
                    key="entry_model.entry_plan",
                    source_module=self.name,
                    created_at=bar.close_time,
                    value={
                        "schema_version": "1",
                        "entry_plan_id": f"entry-{view.index}",
                        "setup_id": f"setup-{view.index}",
                        "symbol": bar.symbol,
                        "timeframe": bar.timeframe,
                        "side": "buy" if view.index % 2 == 0 else "sell",
                        "entry_price": "100",
                        "invalidation_price": "99" if view.index % 2 == 0 else "101",
                        "created_at": bar.close_time.isoformat(),
                    },
                    symbol=bar.symbol,
                    timeframe=bar.timeframe,
                    valid_until=bar.close_time,
                    feature_id=f"entry-{view.index}:feature",
                ),
            )
        )


class StubRiskModule(StrategyModule):
    name = "risk_sizing"
    parameters = MappingProxyType({})
    manifest = ModuleManifest(
        name=name,
        version="1.0.0",
        api_version="1.0",
        required_features=("protection_target.plan",),
        produced_features=("risk_sizing.decision",),
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        assert view.features is not None
        plan = view.features.latest(
            "protection_target.plan",
            symbol=view.current.symbol,
            timeframe=view.current.timeframe,
        )
        if plan is None:
            return ModuleDecision(skips=("risk_sizing.no_protection_plan",))
        return ModuleDecision(
            features=(
                Feature(
                    key="risk_sizing.decision",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value={
                        "protection_plan_id": plan.value["protection_plan_id"],
                        "accepted": True,
                    },
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    valid_until=view.current.close_time,
                    feature_id=f"risk:{plan.value['protection_plan_id']}",
                ),
            )
        )


def bar(index: int = 0) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def market_view(item: Candle, store: FeatureStore, index: int) -> MarketView:
    return MarketView(
        base_bars=(item,),
        timeframe_bars={},
        index=index,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        features=store.snapshot(item.close_time),
    )


def test_dependency_planner_orders_full_local_chain() -> None:
    modules = (StubRiskModule(), ProtectionTargetModule(), StubEntryModule())
    planned = ModuleRegistry().validate_and_plan(modules, base_timeframe="5m")
    assert [module.name for module in planned.modules] == [
        "entry_model",
        "protection_target",
        "risk_sizing",
    ]
    assert planned.required_timeframes == ()


def test_real_feature_store_pipeline() -> None:
    item = bar()
    store = FeatureStore()
    entry = StubEntryModule()
    protection = ProtectionTargetModule()
    risk = StubRiskModule()
    first = market_view(item, store, 0)
    for feature in entry.on_bar(first).features:
        store.publish(feature)
    second = market_view(item, store, 0)
    protection_decision = protection.on_bar(second)
    for feature in protection_decision.features:
        store.publish(feature)
    third = market_view(item, store, 0)
    risk_decision = risk.on_bar(third)
    assert protection_decision.features[0].key == "protection_target.plan"
    assert risk_decision.features[0].key == "risk_sizing.decision"


def test_registry_import_path_loading() -> None:
    module = ModuleRegistry().load_import_path(
        "modular_strategy_protection_target:ProtectionTargetModule",
        {"reward_r": 4.0, "max_diagnostic_results": 10},
    )
    assert isinstance(module, ProtectionTargetModule)
    assert module.config.reward_r == 4


def test_checkpoint_restart_pipeline_continuation() -> None:
    source = ProtectionTargetModule()
    first_bar = bar(0)
    first_store = FeatureStore()
    entry = StubEntryModule()
    for feature in entry.on_bar(market_view(first_bar, first_store, 0)).features:
        first_store.publish(feature)
    source.on_start()
    first_decision = source.on_bar(market_view(first_bar, first_store, 0))
    source.on_finish()
    assert first_decision.features
    restored = ProtectionTargetModule()
    restored.restore_state(source.snapshot_state())
    second_bar = bar(1)
    second_store = FeatureStore()
    for feature in entry.on_bar(market_view(second_bar, second_store, 1)).features:
        second_store.publish(feature)
    restored.on_start()
    second_decision = restored.on_bar(market_view(second_bar, second_store, 1))
    restored.on_finish()
    assert second_decision.features[0].value["entry_plan_id"] == "entry-1"


def test_alternative_producer_is_substitutable() -> None:
    item = bar()
    feature = Feature(
        key="entry_model.entry_plan",
        source_module="alternative_entry",
        created_at=item.close_time,
        value={
            "schema_version": "1",
            "entry_plan_id": "alternative-entry",
            "setup_id": "alternative-setup",
            "symbol": item.symbol,
            "timeframe": item.timeframe,
            "side": "buy",
            "entry_price": "100",
            "invalidation_price": "99",
            "created_at": item.close_time.isoformat(),
        },
        symbol=item.symbol,
        timeframe=item.timeframe,
        valid_until=item.close_time,
        feature_id="alternative-entry:feature",
    )
    view = MarketView(
        base_bars=(item,),
        timeframe_bars={},
        index=0,
        cash=1,
        equity=1,
        position=None,
        pending_orders=(),
        features=FeatureView((feature,), item.close_time),
    )
    decision = ProtectionTargetModule().on_bar(view)
    assert decision.features[0].value["entry_plan_id"] == "alternative-entry"


def test_future_data_invariance() -> None:
    item = bar()
    entry = (
        StubEntryModule()
        .on_bar(
            MarketView(
                base_bars=(item,),
                timeframe_bars={},
                index=0,
                cash=1,
                equity=1,
                position=None,
                pending_orders=(),
                features=FeatureView((), item.close_time),
            )
        )
        .features[0]
    )
    future = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=item.close_time + timedelta(minutes=5),
        value=entry.value,
        symbol=item.symbol,
        timeframe=item.timeframe,
        feature_id="future",
    )
    base = MarketView(
        base_bars=(item,),
        timeframe_bars={},
        index=0,
        cash=1,
        equity=1,
        position=None,
        pending_orders=(),
        features=FeatureView((entry,), item.close_time),
    )
    with_future = MarketView(
        base_bars=(item,),
        timeframe_bars={},
        index=0,
        cash=1,
        equity=1,
        position=None,
        pending_orders=(),
        features=FeatureView((entry, future), item.close_time),
    )
    first = ProtectionTargetModule().on_bar(base).features[0].value
    second = ProtectionTargetModule().on_bar(with_future).features[0].value
    assert first == second


def test_multi_instance_symbol_isolation() -> None:
    btc = bar()
    eth = Candle(
        timestamp=btc.timestamp,
        open=10,
        high=11,
        low=9,
        close=10,
        volume=1,
        symbol="ETHUSDT",
        timeframe="5m",
    )
    modules = (ProtectionTargetModule(), ProtectionTargetModule())
    stores = (FeatureStore(), FeatureStore())
    entries = StubEntryModule()
    for item, module, store in zip((btc, eth), modules, stores, strict=True):
        for feature in entries.on_bar(market_view(item, store, 0)).features:
            value = dict(feature.value)
            value["entry_price"] = str(item.close)
            value["invalidation_price"] = str(item.close - 1)
            store.publish(
                Feature(
                    key=feature.key,
                    source_module=feature.source_module,
                    created_at=feature.created_at,
                    value=value,
                    symbol=item.symbol,
                    timeframe=item.timeframe,
                    valid_until=feature.valid_until,
                    feature_id=f"{item.symbol}:entry",
                )
            )
        assert module.on_bar(market_view(item, store, 0)).features
    assert modules[0].snapshot_state()["bound_symbol"] == "BTCUSDT"
    assert modules[1].snapshot_state()["bound_symbol"] == "ETHUSDT"


def test_dependency_planner_rejects_missing_entry_producer() -> None:
    from modular_core import ModuleDependencyError

    with pytest.raises(ModuleDependencyError, match="missing module dependencies"):
        ModuleRegistry().validate_and_plan((ProtectionTargetModule(),), base_timeframe="5m")


def test_dependency_planner_rejects_entry_protection_cycle() -> None:
    from modular_core import ModuleDependencyError

    class CyclicEntry(StrategyModule):
        name = "entry_model"
        parameters = MappingProxyType({})
        manifest = ModuleManifest(
            name=name,
            version="1.0.0",
            api_version="1.0",
            required_features=("protection_target.plan",),
            produced_features=("entry_model.entry_plan",),
        )

        def on_bar(self, view: MarketView) -> ModuleDecision:
            return ModuleDecision()

    with pytest.raises(ModuleDependencyError, match="cyclic module dependencies"):
        ModuleRegistry().validate_and_plan(
            (ProtectionTargetModule(), CyclicEntry()), base_timeframe="5m"
        )
