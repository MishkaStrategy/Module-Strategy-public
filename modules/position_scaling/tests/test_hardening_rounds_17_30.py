from __future__ import annotations


def test_round_17_source_uses_only_public_core_imports() -> None:
    import ast
    from pathlib import Path

    source_root = Path("src/modular_strategy_position_scaling")
    imported_modules: set[str] = set()
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("modular_core")
            ):
                imported_modules.add(node.module)
            if isinstance(node, ast.Import):
                imported_modules.update(
                    item.name for item in node.names if item.name.startswith("modular_core")
                )
    assert imported_modules == {"modular_core"}


def test_round_18_real_core_checkpoint_helpers_restore_schema_four() -> None:
    from conftest import make_position, make_view, standard_features
    from modular_core import capture_module_states, restore_module_states

    from modular_strategy_position_scaling import (
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

    parameters = {
        "enabled": True,
        "allow_averaging_down": True,
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    }
    modules = (PositionScalingPlannerModule(**parameters), PositionScalingApprovalModule())
    modules[0].on_bar(make_view(close=98, position=make_position(), features=standard_features()))
    states = capture_module_states(modules)
    assert {state.schema_version for state in states} == {"4"}

    restored = (PositionScalingPlannerModule(**parameters), PositionScalingApprovalModule())
    restore_module_states(restored, states)
    assert capture_module_states(restored) == states


def test_round_19_risk_request_notional_respects_contract_size() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from modular_strategy_position_scaling import PositionScalingConfig
    from modular_strategy_position_scaling.engine import ScalingContext, ScalingPlanner

    config = PositionScalingConfig.from_mapping(
        {
            "enabled": True,
            "allow_averaging_down": True,
            "max_total_position_risk_fraction": "0.01",
            "max_additional_risk_fraction": "0.01",
            "rules": [
                {
                    "stage": 1,
                    "mode": "averaging_down",
                    "action": "add",
                    "trigger_kind": "always",
                    "trigger_value": 0,
                    "quantity_kind": "fixed",
                    "quantity_value": "0.1",
                }
            ],
        }
    )
    planner = ScalingPlanner(config)
    context = ScalingContext(
        position_id="position-1",
        setup_id="setup-1",
        symbol="BTCUSDT",
        side="long",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("95"),
        stop_price=Decimal("95"),
        current_price=Decimal("98"),
        unrealized_pnl=Decimal("-2"),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
        index=1,
        attached_index=0,
        decision_time=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        bar_seconds=60,
        last_adjustment_index=None,
        last_adjustment_price=None,
        scale_in_count=0,
        scale_out_count=0,
        data_gap=False,
        exit_pending=False,
        pending_order_count=0,
        setup_valid=True,
        market_regime="trend",
        atr=Decimal("2"),
        liquidation_distance_fraction=Decimal("0.5"),
        margin_usage_fraction=Decimal("0.1"),
        available_margin=Decimal("9000"),
        source_feature_ids=("feature-1",),
        features={"instrument.rules": {"contract_size": "10"}},
    )
    result = planner.evaluate_rule(context, config.rules[0])
    assert result.plan is not None
    request = planner.build_risk_request(
        context,
        result.plan,
        {"quantity_step": "0.001", "contract_size": "10"},
    )
    assert request["portfolio"]["gross_notional"] == "980"
    assert request["portfolio"]["symbol_notional"] == "980"


def test_round_20_risk_request_publishes_full_adjustment_geometry() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from modular_strategy_position_scaling import PositionScalingConfig
    from modular_strategy_position_scaling.engine import ScalingContext, ScalingPlanner

    config = PositionScalingConfig.from_mapping(
        {
            "enabled": True,
            "allow_averaging_down": True,
            "max_total_position_risk_fraction": "0.01",
            "max_additional_risk_fraction": "0.01",
            "rules": [
                {
                    "stage": 1,
                    "mode": "averaging_down",
                    "action": "add",
                    "trigger_kind": "always",
                    "trigger_value": 0,
                    "quantity_kind": "fixed",
                    "quantity_value": "0.25",
                }
            ],
        }
    )
    context = ScalingContext(
        position_id="position-1",
        setup_id="setup-1",
        symbol="BTCUSDT",
        side="long",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("95"),
        stop_price=Decimal("95"),
        current_price=Decimal("98"),
        unrealized_pnl=Decimal("-2"),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
        index=1,
        attached_index=0,
        decision_time=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        bar_seconds=60,
        last_adjustment_index=None,
        last_adjustment_price=None,
        scale_in_count=0,
        scale_out_count=0,
        data_gap=False,
        exit_pending=False,
        pending_order_count=0,
        setup_valid=True,
        market_regime="trend",
        atr=Decimal("2"),
        liquidation_distance_fraction=Decimal("0.5"),
        margin_usage_fraction=Decimal("0.1"),
        available_margin=Decimal("9000"),
        source_feature_ids=("feature-1",),
        features={"instrument.rules": {"contract_size": "10"}},
    )
    planner = ScalingPlanner(config)
    result = planner.evaluate_rule(context, config.rules[0])
    assert result.plan is not None
    request = planner.build_risk_request(
        context, result.plan, {"quantity_step": "0.001", "contract_size": "10"}
    )
    metadata = request["metadata"]
    assert metadata == {
        "adjustment_id": result.plan.adjustment_id,
        "position_id": "position-1",
        "adjustment_kind": "position_increase",
        "opens_new_position": False,
        "current_quantity": "1",
        "desired_quantity": "0.25",
        "maximum_quantity": "0.25",
        "requested_delta_quantity": "0.25",
        "maximum_delta_quantity": "0.25",
        "projected_total_quantity": "1.25",
        "current_average_price": "100",
        "projected_average_price": "99.6",
        "valuation_price": "98",
        "current_total_risk": "50",
        "projected_total_risk": "57.5",
        "current_notional": "980",
        "projected_notional": "1225",
    }


def test_round_21_risk_request_benchmark_runner_is_deterministic() -> None:
    import runpy
    from pathlib import Path

    namespace = runpy.run_path(str(Path("benchmarks/benchmark.py")))
    runner_factory = namespace["risk_request_runner"]
    first = runner_factory()
    second = runner_factory()
    assert first() == second()
    assert first().startswith("riskreq-")


def test_round_22_risk_request_identity_reuses_adjustment_digest() -> None:
    import runpy
    from pathlib import Path

    from modular_strategy_position_scaling.engine import ScalingPlanner

    namespace = runpy.run_path(str(Path("benchmarks/benchmark.py")))
    cfg = namespace["config"]()
    result = ScalingPlanner(cfg).evaluate_rule(namespace["context"](), cfg.rules[0])
    assert result.plan is not None
    assert result.plan.risk_request_id == result.plan.adjustment_id.replace(
        "adjustment-", "riskreq-", 1
    )


def test_round_23_bounded_state_load_keeps_configured_caps() -> None:
    import runpy
    from pathlib import Path

    namespace = runpy.run_path(str(Path("benchmarks/benchmark.py")))
    result = namespace["bounded_state_load"](5_000)
    assert result["processed_event_count"] == 2_048
    assert result["history_count"] == 50
    assert result["throughput_events_per_second"] > 0


def test_round_24_restart_load_matches_uninterrupted_digest() -> None:
    import runpy
    from pathlib import Path

    namespace = runpy.run_path(str(Path("benchmarks/benchmark.py")))
    first = namespace["restart_state_load"](10_000, 5)
    second = namespace["restart_state_load"](10_000, 5)
    assert first["snapshot_digest"] == second["snapshot_digest"]
    assert first["processed_event_count"] == 2_048
    assert first["history_count"] == 100
    assert first["restore_seconds_maximum"] >= first["restore_seconds_median"]


def test_round_25_planner_observability_counters_survive_restore() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

    parameters = {
        "enabled": True,
        "allow_averaging_down": True,
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    }
    planner = PositionScalingPlannerModule(**parameters)
    planned = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": "BTCUSDT",
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approved_decision = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    )
    approved = next(item for item in approved_decision.features if item.key == FEATURE_APPROVED)
    planner.on_bar(make_view(index=1, close=98, position=make_position(), features=(approved,)))
    snapshot = planner.snapshot_state()
    counters = snapshot["counters"]
    assert counters["feedback_approved"] == 1
    assert counters["feedback_rejected"] == 0
    assert counters["feedback_invalid"] == 0
    assert counters["unapproved_changes"] == 0
    assert counters["overfills"] == 0
    assert counters["partial_fills"] == 0

    restored = PositionScalingPlannerModule(**parameters)
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    decision = restored.on_bar(make_view(index=1, close=98, position=make_position(), features=()))
    assert decision.diagnostics["restore_count"] == 1


def test_round_26_diagnostics_expose_bounded_lifecycle_evidence() -> None:
    from conftest import make_position, make_view, standard_features

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    planner = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    )
    decision = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    diagnostics = decision.diagnostics
    assert diagnostics["state_schema_version"] == "4"
    assert diagnostics["config_fingerprint"] == planner.config.fingerprint
    assert diagnostics["feedback_received"] is False
    assert diagnostics["pending_approved_quantity"] is None
    assert diagnostics["cooldown_until_index"] == -1
    assert diagnostics["processed_event_count"] <= planner.config.max_processed_events
    assert diagnostics["history_size"] <= planner.config.max_history


def test_round_27_package_and_manifest_versions_are_consistent() -> None:
    from pathlib import Path

    from modular_strategy_position_scaling import (
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
        __version__,
    )

    assert __version__ == "0.2.2"
    assert PositionScalingPlannerModule.manifest.version == __version__
    assert PositionScalingApprovalModule.manifest.version == __version__
    assert Path("VERSION").read_text(encoding="utf-8").strip() == __version__


def test_round_28_self_hosted_workflow_uses_verified_runner_contract() -> None:
    from pathlib import Path

    candidates = (
        Path(".github/workflows/position-scaling-hardening-ci.yml"),
        Path("../../.github/workflows/position-scaling-hardening-ci.yml"),
    )
    workflow = next((path for path in candidates if path.is_file()), None)
    assert workflow is not None
    text = workflow.read_text(encoding="utf-8")
    assert "runs-on: [self-hosted, fast]" in text
    assert "ubuntu-latest" not in text and "ubuntu-24.04" not in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 90" in text
    assert "uv python install" in text
    assert "pip-audit" in text and "cyclonedx-py" in text
    assert "modules/position_scaling/**" in text


def test_round_29_full_partial_approval_restart_reconcile_next_stage() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

    parameters = {
        "enabled": True,
        "allow_averaging_down": True,
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "cooldown_bars": 0,
                "expires_bars": 5,
            },
            {
                "stage": 2,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "cooldown_bars": 0,
                "expires_bars": 5,
            },
        ],
    }
    planner = PositionScalingPlannerModule(**parameters)
    planned = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    assert plan.value["stage"] == 1
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": "BTCUSDT",
            "status": "approved",
            "approved": True,
            "quantity": "0.05",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approved_decision = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    )
    approved = next(item for item in approved_decision.features if item.key == FEATURE_APPROVED)
    planner.on_bar(make_view(index=1, close=98, position=make_position(), features=(approved,)))
    checkpoint = planner.snapshot_state()
    assert checkpoint["pending_approved_quantity"] == "0.05"

    restored = PositionScalingPlannerModule(**parameters)
    restored.restore_state(checkpoint)
    restored.on_bar(
        make_view(
            index=2,
            close=98,
            position=make_position(
                quantity=1.05,
                entry_price=99.9047619047619,
                entry_fill_count=2,
            ),
            features=standard_features(2),
        )
    )
    reconciled = restored.snapshot_state()
    assert reconciled["completed_stages"] == [1]
    assert reconciled["scale_in_count"] == 1
    assert reconciled["pending_plan"] is None

    next_decision = restored.on_bar(
        make_view(
            index=3,
            close=97,
            position=make_position(
                quantity=1.05,
                entry_price=99.9047619047619,
                entry_fill_count=2,
            ),
            features=standard_features(3),
        )
    )
    next_plan = next(item for item in next_decision.features if item.key == FEATURE_PLAN)
    assert next_plan.value["stage"] == 2


def test_round_30_dependency_planner_orders_planner_risk_approval() -> None:
    from modular_core import ModuleDecision, ModuleManifest, ModuleRegistry, StrategyModule

    from modular_strategy_position_scaling import (
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

    class RiskContractStub(StrategyModule):
        name = "risk_sizing_contract_stub"
        manifest = ModuleManifest(
            name=name,
            version="1.1.0",
            api_version="1.0",
            required_features=("risk.request",),
            produced_features=("risk.decision",),
            state_schema_version="1",
            skip_until_ready=False,
        )

        def on_bar(self, view: object) -> ModuleDecision:
            del view
            return ModuleDecision()

    planner = PositionScalingPlannerModule()
    risk = RiskContractStub()
    approval = PositionScalingApprovalModule()
    result = ModuleRegistry().validate_and_plan((approval, risk, planner), base_timeframe="1m")
    assert tuple(module.name for module in result.modules) == (
        "position_scaling_planner",
        "risk_sizing_contract_stub",
        "position_scaling_approval",
    )
    assert result.produced_by["risk.request"] == planner.name
    assert result.produced_by["risk.decision"] == risk.name
    assert result.produced_by["position_scaling.approved"] == approval.name
