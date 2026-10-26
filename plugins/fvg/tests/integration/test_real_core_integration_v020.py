from __future__ import annotations

from importlib.metadata import entry_points

from modular_core import (
    FeatureDelete,
    FeatureStore,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)

from conftest import bullish_triplet, view_for
from modular_strategy_fvg import ACTIVE_ZONE_KEY, FvgModule


class Consumer(StrategyModule):
    name = "consumer"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        required_features=(ACTIVE_ZONE_KEY,),
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_real_core_registry_import_path_and_dependency_plan() -> None:
    registry = ModuleRegistry()
    module = registry.load_import_path("modular_strategy_fvg:FvgModule", {"timeframes": ["15m"]})
    plan = registry.validate_and_plan((Consumer(), module), base_timeframe="5m")
    assert [item.name for item in plan.manifests] == ["fvg", "consumer"]


def test_entry_point_is_discoverable() -> None:
    selected = {item.name: item for item in entry_points().select(group="modular_strategy.modules")}
    assert "fvg" in selected
    assert selected["fvg"].load() is FvgModule


def test_feature_store_accepts_and_exactly_deletes_active_zone() -> None:
    decision = FvgModule(timeframes=("15m",)).on_bar(view_for(bullish_triplet()))
    store = FeatureStore()
    active = next(feature for feature in decision.features if feature.key == ACTIVE_ZONE_KEY)
    stored = store.publish(active)
    assert stored.feature_id == active.feature_id
    removed = store.delete(
        FeatureDelete(
            key=ACTIVE_ZONE_KEY,
            source_module="fvg",
            feature_id=active.feature_id,
            symbol="BTCUSDT",
            timeframe="15m",
            reason="test",
        )
    )
    assert removed == (stored,)


def test_same_bar_feature_is_causally_visible_to_downstream_store() -> None:
    view = view_for(bullish_triplet())
    decision = FvgModule(timeframes=("15m",)).on_bar(view)
    store = FeatureStore()
    for feature in decision.features:
        store.publish(feature)
    snapshot = store.snapshot(view.current.close_time)
    assert snapshot.latest(ACTIVE_ZONE_KEY, symbol="BTCUSDT", timeframe="15m", source_module="fvg") is not None


def test_distribution_declares_pep561_marker() -> None:
    from importlib.resources import files

    assert files("modular_strategy_fvg").joinpath("py.typed").is_file()
