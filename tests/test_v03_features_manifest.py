import pytest

from modular_core import (
    BacktestEngine,
    Feature,
    MarketView,
    ModuleConfigurationError,
    ModuleDecision,
    ModuleDependencyError,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)


class Consumer(StrategyModule):
    name = "consumer"
    manifest = ModuleManifest(
        name=name,
        required_features=("regime.current",),
    )

    def __init__(self):
        self.values = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.values.append(view.features.get("regime.current"))
        return ModuleDecision()


class Producer(StrategyModule):
    name = "producer"
    manifest = ModuleManifest(
        name=name,
        produced_features=("regime.current",),
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(
            features=(
                Feature(
                    key="regime.current",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value="trending",
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                ),
            )
        )


def test_manifest_dependency_orders_modules_and_feature_visible_same_bar(bars_factory):
    consumer = Consumer()
    result = BacktestEngine((consumer, Producer())).run(bars_factory([100, 101]))
    assert result.modules == ("producer", "consumer")
    assert consumer.values == ["trending", "trending"]
    assert len(result.journal.features) == 2


def test_missing_feature_dependency_rejected_before_run(bars_factory):
    with pytest.raises(ModuleDependencyError, match="requires regime.current"):
        BacktestEngine(Consumer()).run(bars_factory([100]))


class Configured(StrategyModule):
    name = "configured"
    manifest = ModuleManifest(
        name=name,
        parameters_schema={
            "period": {
                "type": "integer",
                "minimum": 2,
                "maximum": 100,
                "required": True,
            }
        },
    )

    def __init__(self, period=1):
        self.parameters = {"period": period}

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_manifest_parameters_are_validated(bars_factory):
    with pytest.raises(ModuleConfigurationError, match="below minimum"):
        BacktestEngine(Configured(period=1)).run(bars_factory([100]))


def test_registry_loads_plugin_without_kernel_edit():
    registry = ModuleRegistry()
    registry.register("configured", Configured)
    module = registry.load_named("configured", {"period": 20})
    assert module.parameters == {"period": 20}
