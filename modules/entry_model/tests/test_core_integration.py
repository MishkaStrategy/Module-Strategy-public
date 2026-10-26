from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

import pytest
from modular_core import (
    BacktestEngine,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleDependencyError,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
    capture_module_states,
    restore_module_states,
)

from modular_modules.entry_model import (
    ENTRY_PLAN_FEATURE_KEY,
    SETUP_FEATURE_KEY,
    EntryModelModule,
    EntryPlan,
)

from .support import bar_at, payload


class CanonicalSetupProducer(StrategyModule):
    name = "canonical_setup_source"
    manifest = ModuleManifest(
        name=name,
        version="1.0.0-test",
        api_version="1.0",
        produced_features=(SETUP_FEATURE_KEY,),
        parameters_schema={"emit_index": {"type": "integer", "minimum": 0}},
        skip_until_ready=False,
        state_schema_version="1",
    )

    def __init__(self, *, emit_index: int = 0) -> None:
        self.emit_index = emit_index
        self.parameters: Mapping[str, object] = {"emit_index": emit_index}

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index != self.emit_index:
            return ModuleDecision()
        current = view.current
        feature = Feature(
            key=SETUP_FEATURE_KEY,
            source_module=self.name,
            created_at=current.close_time,
            value=payload(
                setup_id=f"setup-{view.index}",
                side="buy",
                confirmed_at=current.close_time,
                confirmation_price="100",
                extreme_price="90",
                entry_kind="limit_retracement",
            ),
            symbol=current.symbol,
            timeframe=current.timeframe,
            valid_until=current.close_time,
            schema_version="1",
            feature_id=f"canonical-setup-{view.index}",
        )
        return ModuleDecision(features=(feature,))


class PlanConsumer(StrategyModule):
    name = "plan_consumer"
    manifest = ModuleManifest(
        name=name,
        version="1.0.0-test",
        api_version="1.0",
        required_features=(ENTRY_PLAN_FEATURE_KEY,),
        produced_features=("consumer.receipt",),
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        assert view.features is not None
        plan_feature = view.features.latest(ENTRY_PLAN_FEATURE_KEY, symbol=view.current.symbol)
        if plan_feature is None or plan_feature.created_at != view.current.close_time:
            return ModuleDecision()
        plan = EntryPlan.from_feature_value(plan_feature.value)
        return ModuleDecision(
            features=(
                Feature(
                    key="consumer.receipt",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value={"plan_id": plan.plan_id},
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    valid_until=view.current.close_time,
                    feature_id=f"receipt-{plan.plan_id}",
                ),
            )
        )


def candles(count: int) -> tuple:
    return tuple(bar_at(index) for index in range(count))


def published_features(result: object, key: str) -> list[Feature]:
    return [
        row["feature"]
        for row in result.journal.features  # type: ignore[attr-defined]
        if row["action"] == "published" and row["feature"].key == key
    ]


def test_manifest_contract_and_dependency_plan() -> None:
    entry = EntryModelModule()
    source = CanonicalSetupProducer()
    consumer = PlanConsumer()
    registry = ModuleRegistry()
    plan = registry.validate_and_plan((consumer, entry, source), base_timeframe="5m")
    assert [module.name for module in plan.modules] == [source.name, entry.name, consumer.name]
    assert plan.produced_by[SETUP_FEATURE_KEY] == source.name
    assert plan.produced_by[ENTRY_PLAN_FEATURE_KEY] == entry.name
    assert entry.manifest.version == "0.2.0"
    assert entry.manifest.api_version == "1.0"
    assert entry.manifest.state_schema_version == "2"
    assert entry.manifest.required_features == (SETUP_FEATURE_KEY,)
    assert ENTRY_PLAN_FEATURE_KEY in entry.manifest.produced_features
    assert entry.manifest.required_timeframes == ()


def test_dependency_planner_rejects_missing_upstream() -> None:
    with pytest.raises(ModuleDependencyError, match="missing module dependencies"):
        ModuleRegistry().validate_and_plan((EntryModelModule(),), base_timeframe="5m")


def test_real_engine_same_bar_pipeline_and_downstream_consumption() -> None:
    source = CanonicalSetupProducer(emit_index=0)
    entry = EntryModelModule(ttl_bars=2)
    consumer = PlanConsumer()
    result = BacktestEngine((consumer, entry, source)).run(candles(2))
    setup_features = published_features(result, SETUP_FEATURE_KEY)
    plan_features = published_features(result, ENTRY_PLAN_FEATURE_KEY)
    receipts = published_features(result, "consumer.receipt")
    assert len(setup_features) == 1
    assert len(plan_features) == 1
    assert len(receipts) == 1
    plan = EntryPlan.from_feature_value(plan_features[0].value)
    assert plan.setup_id == "setup-0"
    assert plan.created_at == bar_at(0).close_time
    assert receipts[0].value["plan_id"] == plan.plan_id
    assert result.modules == (source.name, entry.name, consumer.name)
    assert result.journal.errors == []


def test_no_lookahead_truncated_run_and_future_invariance() -> None:
    short = BacktestEngine((EntryModelModule(), CanonicalSetupProducer(emit_index=1))).run(
        candles(1)
    )
    assert published_features(short, ENTRY_PLAN_FEATURE_KEY) == []

    exact = BacktestEngine((EntryModelModule(), CanonicalSetupProducer(emit_index=1))).run(
        candles(2)
    )
    extended = BacktestEngine((EntryModelModule(), CanonicalSetupProducer(emit_index=1))).run(
        candles(4)
    )
    exact_plan = EntryPlan.from_feature_value(
        published_features(exact, ENTRY_PLAN_FEATURE_KEY)[0].value
    )
    extended_plan = EntryPlan.from_feature_value(
        published_features(extended, ENTRY_PLAN_FEATURE_KEY)[0].value
    )
    assert exact_plan == extended_plan
    assert exact_plan.created_at == bar_at(1).close_time


def test_engine_checkpoint_contains_versioned_entry_state() -> None:
    source = CanonicalSetupProducer()
    entry = EntryModelModule()
    result = BacktestEngine((entry, source)).run(candles(2))
    states = {state.module: state for state in result.checkpoint.module_states}
    state = states[entry.name]
    assert state.module_version == "0.2.0"
    assert state.schema_version == "2"
    assert state.payload["state_schema_version"] == "2"
    assert len(state.payload["processed"]) == 1


def test_core_capture_and_restore_helpers() -> None:
    module = EntryModelModule()
    source = CanonicalSetupProducer()
    result = BacktestEngine((module, source)).run(candles(1))
    states = capture_module_states((module,))
    restored = EntryModelModule()
    restore_module_states((restored,), states)
    assert restored.snapshot_state() == module.snapshot_state()
    assert result.checkpoint.module_states


def test_core_restore_rejects_version_or_schema_mismatch() -> None:
    module = EntryModelModule()
    states = capture_module_states((module,))
    from modular_core import ModuleState

    bad_version = ModuleState(
        module=module.name,
        module_version="9.9.9",
        schema_version="2",
        payload=states[0].payload,
    )
    with pytest.raises(ValueError, match="module version mismatch"):
        restore_module_states((EntryModelModule(),), (bad_version,))
    bad_schema = ModuleState(
        module=module.name,
        module_version="0.2.0",
        schema_version="1",
        payload=states[0].payload,
    )
    with pytest.raises(ValueError, match="state schema mismatch"):
        restore_module_states((EntryModelModule(),), (bad_schema,))


def test_entry_point_discovery_public_import_and_config_loading() -> None:
    registry = ModuleRegistry()
    discovered = registry.discover_entry_points()
    assert "entry_model" in discovered
    module = registry.load_named(
        "entry_model",
        {
            "ttl_bars": 2,
            "retracement_fraction": "0.5",
            "allowed_entry_kinds": ["limit_retracement", "market"],
        },
    )
    assert isinstance(module, EntryModelModule)
    assert module.config.ttl_bars == 2
    assert module.config.retracement_fraction == Decimal("0.5")

    loaded = registry.load_config(
        {
            "modules": [
                {
                    "plugin": "entry_model",
                    "parameters": {
                        "default_entry_kind": "market",
                        "allowed_entry_kinds": ["market"],
                    },
                }
            ]
        }
    )
    assert isinstance(loaded[0], EntryModelModule)
    assert loaded[0].config.default_entry_kind.value == "market"


def test_import_path_loading() -> None:
    module = ModuleRegistry().load_import_path(
        "modular_modules.entry_model:EntryModelModule",
        {"ttl_bars": 3},
    )
    assert isinstance(module, EntryModelModule)
    assert module.config.ttl_bars == 3


def test_feature_lifetime_expires_in_core_store() -> None:
    result = BacktestEngine(
        (EntryModelModule(ttl_bars=1), CanonicalSetupProducer(emit_index=0))
    ).run(candles(3))
    plan_rows = [
        row for row in result.journal.features if row["feature"].key == ENTRY_PLAN_FEATURE_KEY
    ]
    assert [row["action"] for row in plan_rows] == ["published", "expired"]
    plan = EntryPlan.from_feature_value(plan_rows[0]["feature"].value)
    assert plan.expires_at == bar_at(0).close_time + timedelta(minutes=5)
