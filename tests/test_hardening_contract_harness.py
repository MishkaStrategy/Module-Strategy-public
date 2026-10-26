from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import inspect

import pytest

from modular_core import (
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)
from modular_core.run_manifest import build_run_manifest
from modular_core import Candle, EngineConfig

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FeatureStage(StrategyModule):
    def __init__(self, name: str, requires: tuple[str, ...], produces: str) -> None:
        self.name = name
        self.output = produces
        self.manifest = ModuleManifest(
            name=name,
            required_features=requires,
            produced_features=(produces,),
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(
            features=(Feature(self.output, self.name, view.current.close_time, {"ok": True}),)
        )


def test_executor_chain_dependency_contract_orders_all_four_stages() -> None:
    entry = FeatureStage("entry_model", ("setup.confirmed",), "entry_model.plan")
    protection = FeatureStage("protection_target", ("entry_model.plan",), "protection_target.plan")
    risk = FeatureStage("risk_sizing", ("protection_target.plan",), "risk.decision")
    exits = FeatureStage("position_exit", ("risk.decision",), "position_exit.plan")
    setup = FeatureStage("setup_source", (), "setup.confirmed")
    plan = ModuleRegistry().validate_and_plan(
        (exits, risk, protection, entry, setup), base_timeframe="5m"
    )
    assert tuple(module.name for module in plan.modules) == (
        "setup_source",
        "entry_model",
        "protection_target",
        "risk_sizing",
        "position_exit",
    )


def test_public_contract_field_surface_remains_backward_compatible() -> None:
    assert {field.name for field in fields(ModuleManifest)} >= {
        "name",
        "version",
        "api_version",
        "required_timeframes",
        "required_features",
        "optional_features",
        "produced_features",
        "warmup_bars",
        "parameters_schema",
        "skip_until_ready",
        "state_schema_version",
    }
    assert {field.name for field in fields(MarketView)} >= {
        "base_bars",
        "timeframe_bars",
        "index",
        "cash",
        "equity",
        "position",
        "pending_orders",
        "features",
        "readiness",
        "sessions",
    }
    assert {field.name for field in fields(ModuleDecision)} >= {
        "setups",
        "signals",
        "features",
        "feature_deletes",
        "orders",
        "cancellations",
        "position_updates",
        "closes",
        "skips",
        "diagnostics",
    }
    assert tuple(inspect.signature(StrategyModule.on_bar).parameters) == ("self", "view")


def test_run_manifest_rejects_empty_or_naive_identity_inputs() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_run_manifest(
            kernel_version="0.4.1", created_at=NOW, bars=(), config=EngineConfig(), manifests=()
        )
    bar = Candle(NOW, 100, 101, 99, 100, 1, "X", "5m")
    with pytest.raises(ValueError, match="timezone-aware"):
        build_run_manifest(
            kernel_version="0.4.1",
            created_at=datetime(2026, 1, 1),
            bars=(bar,),
            config=EngineConfig(),
            manifests=(),
        )
