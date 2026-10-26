from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from conftest import make_position, make_view, standard_features
from modular_core import (
    Feature,
    FeatureStore,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)

from modular_strategy_position_scaling import (
    FEATURE_APPROVED,
    FEATURE_PLAN,
    FEATURE_RISK_DECISION,
    FEATURE_RISK_REQUEST,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)


class RiskStubModule(StrategyModule):
    name = "risk_stub"
    manifest = ModuleManifest(
        name=name,
        required_features=(FEATURE_RISK_REQUEST,),
        produced_features=(FEATURE_RISK_DECISION,),
        skip_until_ready=False,
    )
    parameters: Mapping[str, object] = MappingProxyType({})

    def on_bar(self, view: MarketView) -> ModuleDecision:
        assert view.features is not None
        request = view.features.latest(FEATURE_RISK_REQUEST, symbol=view.current.symbol)
        if request is None:
            return ModuleDecision(skips=("risk_request_missing",))
        value = request.value
        assert isinstance(value, Mapping)
        decision: dict[str, Any] = {
            "schema_version": "1",
            "request_id": value["request_id"],
            "symbol": value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": value["metadata"]["desired_quantity"],
            "raw_quantity": value["metadata"]["desired_quantity"],
            "risk_fraction": value["requested_risk_fraction"],
            "risk_budget": "25",
            "actual_risk_amount": "1",
            "actual_risk_fraction": "0.0001",
            "notional": "24.5",
            "required_leverage": "0.01",
            "binding_constraints": [],
            "reject_reason": None,
            "message": "",
            "setup_id": value["setup_id"],
            "diagnostics": {},
        }
        return ModuleDecision(
            features=(
                Feature(
                    key=FEATURE_RISK_DECISION,
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value=decision,
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    valid_until=view.current.close_time,
                    feature_id=f"risk-{view.index}",
                    invalidate_on_gap=False,
                ),
            )
        )


def _pipeline_view(base: MarketView, store: FeatureStore) -> MarketView:
    return MarketView(
        base_bars=base.base_bars,
        timeframe_bars=base.timeframe_bars,
        index=base.index,
        cash=base.cash,
        equity=base.equity,
        position=base.position,
        pending_orders=base.pending_orders,
        exit_pending=base.exit_pending,
        data_gap=base.data_gap,
        features=store.snapshot(base.current.close_time),
    )


def test_dependency_planner_orders_planner_risk_approval() -> None:
    planner = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.25",
                "expires_bars": 0,
            }
        ],
    )
    risk = RiskStubModule()
    approval = PositionScalingApprovalModule()
    plan = ModuleRegistry().validate_and_plan(
        (planner, risk, approval),
        base_timeframe="1m",
    )
    assert [item.name for item in plan.modules] == [
        "position_scaling_planner",
        "risk_stub",
        "position_scaling_approval",
    ]


def test_full_closed_bar_pipeline_and_restart_determinism() -> None:
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
                "quantity_value": "0.25",
                "expires_bars": 0,
            }
        ],
    }
    base = make_view(
        close=98,
        position=make_position(),
        features=standard_features(),
    )

    def execute(
        planner: PositionScalingPlannerModule, approval: PositionScalingApprovalModule
    ) -> tuple[list[str], Mapping[str, object], Mapping[str, object]]:
        store = FeatureStore()
        for item in standard_features():
            store.publish(item)
        modules: tuple[StrategyModule, ...] = (planner, RiskStubModule(), approval)
        keys: list[str] = []
        for module in modules:
            decision = module.on_bar(_pipeline_view(base, store))
            for produced in decision.features:
                store.publish(produced)
                keys.append(produced.key)
        return keys, planner.snapshot_state(), approval.snapshot_state()

    first_planner = PositionScalingPlannerModule(**parameters)
    first_approval = PositionScalingApprovalModule()
    first_keys, planner_state, approval_state = execute(first_planner, first_approval)
    assert FEATURE_PLAN in first_keys
    assert FEATURE_RISK_REQUEST in first_keys
    assert FEATURE_RISK_DECISION in first_keys
    assert FEATURE_APPROVED in first_keys

    restored_planner = PositionScalingPlannerModule(**parameters)
    restored_planner.restore_state(planner_state)
    restored_approval = PositionScalingApprovalModule()
    restored_approval.restore_state(approval_state)
    assert restored_planner.snapshot_state() == planner_state
    assert restored_approval.snapshot_state() == approval_state


def test_future_feature_is_not_visible_to_current_bar() -> None:
    store = FeatureStore()
    future = standard_features(1)[0]
    store.publish(future)
    current = make_view(position=make_position())
    assert store.snapshot(current.current.close_time).latest("setup.validity") is None
