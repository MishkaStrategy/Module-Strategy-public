import pytest

from modular_core import (
    BacktestEngine,
    MarketView,
    ModuleConfigurationError,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)


class Parameterized(StrategyModule):
    name = "parameterized"
    manifest = ModuleManifest(
        name=name,
        parameters_schema={"period": {"type": "integer", "minimum": 1}},
    )

    def __init__(self, period: int) -> None:
        self.parameters = {"period": period}

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_run_id_changes_when_module_parameters_change(bars_factory) -> None:
    bars = bars_factory([100, 101])
    first = BacktestEngine(Parameterized(10)).run(bars).run_manifest
    second = BacktestEngine(Parameterized(20)).run(bars).run_manifest
    assert first.module_hash != second.module_hash
    assert first.run_id != second.run_id
    assert first.module_parameters[0]["period"] == 10


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": 1, "modules": []},
        {"modules": [{"class": "x:y", "plugin": "z"}]},
        {"modules": [{"class": "x:y", "enabled": "yes"}]},
        {"modules": [{"class": "x:y", "parameters": []}]},
        {"modules": [{"class": "x:y", "mystery": 1}]},
    ],
)
def test_module_config_rejects_ambiguous_or_unknown_fields(payload) -> None:
    with pytest.raises(ModuleConfigurationError):
        ModuleRegistry().load_config(payload)
