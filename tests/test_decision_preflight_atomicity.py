from datetime import datetime, timezone

import pytest

from modular_core import (
    Candle,
    EngineConfig,
    Feature,
    ModuleDecision,
    ModuleManifest,
    Side,
    Signal,
    StrategyModule,
)
from modular_core.audit import Journal
from modular_core.execution import BrokerSimulator
from modular_core.features import FeatureStore
from modular_core.setups import SetupRegistry

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR = Candle(NOW, 100, 101, 99, 100, 1, "X", "5m")


class Module(StrategyModule):
    name = "module"
    manifest = ModuleManifest(name=name, produced_features=("module.value",))

    def on_bar(self, view):  # type: ignore[no-untyped-def]
        return ModuleDecision()


def _state():
    journal = Journal(run_id="run-test")
    return (
        Module(),
        Journal(run_id="run-test"),
        FeatureStore(),
        SetupRegistry(),
        BrokerSimulator(EngineConfig(), journal),
    )


def test_late_signal_contract_error_does_not_publish_earlier_feature() -> None:
    module, journal, features, setups, broker = _state()
    decision = ModuleDecision(
        features=(Feature("module.value", module.name, BAR.close_time, 1),),
        signals=(Signal("wrong", Side.BUY, "late error"),),
    )
    engine = __import__("modular_core.engine", fromlist=["BacktestEngine"]).BacktestEngine(module)
    with pytest.raises(ValueError, match="signal.module"):
        engine._apply_decision(
            decision=decision,
            module=module,
            manifest=module.manifest,
            bar=BAR,
            index=0,
            broker=broker,
            journal=journal,
            features=features,
            setups=setups,
        )
    assert features.count == 0
    assert setups.all() == ()
    assert journal.features == []


def test_preview_copies_do_not_mutate_live_stores() -> None:
    features = FeatureStore()
    features.publish(Feature("module.value", "module", NOW, 1))
    clone = features.copy()
    clone.publish(Feature("module.value", "module", NOW, 2))
    assert features.count == 1
    assert clone.count == 2

    setups = SetupRegistry()
    assert setups.copy().all() == ()
