from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest
from conftest import feature, make_position, make_view, standard_features
from modular_core import PositionSide

from modular_strategy_position_scaling.canonical import deep_thaw
from modular_strategy_position_scaling.enums import ReasonCode
from modular_strategy_position_scaling.models import PositionAdjustmentPlan
from modular_strategy_position_scaling.module import (
    FEATURE_APPROVED,
    FEATURE_PLAN,
    FEATURE_REJECTED,
    FEATURE_RISK_DECISION,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)


def _module(*, side: str = "long", mode: str = "averaging_down") -> PositionScalingPlannerModule:
    is_average = mode == "averaging_down"
    return PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=is_average,
        allow_pyramiding=mode == "pyramiding",
        allow_scale_out=mode == "scale_out",
        rules=[
            {
                "stage": 1,
                "mode": mode,
                "action": "reduce" if mode == "scale_out" else "add",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.25",
                "expires_bars": 5,
            }
        ],
        require_setup_validity=False,
        require_market_regime=False,
        require_liquidation_distance=False,
        require_margin_usage=False,
    )


def _plan(module: PositionScalingPlannerModule, *, side: PositionSide = PositionSide.LONG) -> Any:
    decision = module.on_bar(
        make_view(
            close=98 if side is PositionSide.LONG else 102,
            position=make_position(
                side=side,
                stop_loss=95 if side is PositionSide.LONG else 105,
            ),
            features=standard_features(),
        )
    )
    return next(item for item in decision.features if item.key == FEATURE_PLAN)


def _risk(plan: Mapping[str, Any], *, quantity: str = "0.25") -> Any:
    return feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan["risk_request_id"],
            "symbol": plan["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": quantity,
            "raw_quantity": quantity,
            "risk_fraction": "0.001",
            "risk_budget": "10",
            "actual_risk_amount": "1",
            "actual_risk_fraction": "0.0001",
            "notional": "25",
            "required_leverage": "0.01",
            "binding_constraints": [],
            "reject_reason": None,
            "message": "",
            "setup_id": plan["setup_id"],
            "diagnostics": {},
        },
        source="risk_sizing",
    )


def test_short_scale_in_overfill_is_symmetric_and_fails_closed() -> None:
    planner = _module()
    plan = _plan(planner, side=PositionSide.SHORT)
    approved = next(
        item
        for item in PositionScalingApprovalModule()
        .on_bar(
            make_view(
                close=102,
                position=make_position(side=PositionSide.SHORT, stop_loss=105),
                features=(plan, _risk(plan.value)),
            )
        )
        .features
        if item.key == FEATURE_APPROVED
    )
    result = planner.on_bar(
        make_view(
            index=1,
            close=102,
            position=make_position(
                side=PositionSide.SHORT,
                quantity=1.3,
                entry_price=100.5,
                stop_loss=105,
                entry_fill_count=2,
            ),
            features=(*standard_features(1), approved),
        )
    )
    assert result.skips == (ReasonCode.ADJUSTMENT_OVERFILL.value,)
    assert planner.snapshot_state()["lifecycle"] == "recovery_required"


def test_scale_out_beyond_approved_reduction_is_overfill() -> None:
    planner = _module(mode="scale_out")
    plan = _plan(planner)
    approved = next(
        item
        for item in PositionScalingApprovalModule()
        .on_bar(make_view(close=98, position=make_position(), features=(plan,)))
        .features
        if item.key == FEATURE_APPROVED
    )
    result = planner.on_bar(
        make_view(
            index=1,
            position=make_position(quantity=0.70, entry_price=100, entry_fill_count=1),
            features=(*standard_features(1), approved),
        )
    )
    assert result.skips == (ReasonCode.ADJUSTMENT_OVERFILL.value,)
    assert planner.snapshot_state()["scale_out_count"] == 0


def test_partial_approval_short_side_reconciles_exact_quantity() -> None:
    planner = _module()
    plan = _plan(planner, side=PositionSide.SHORT)
    approval = PositionScalingApprovalModule().on_bar(
        make_view(
            close=102,
            position=make_position(side=PositionSide.SHORT, stop_loss=105),
            features=(plan, _risk(plan.value, quantity="0.1")),
        )
    )
    approved = next(item for item in approval.features if item.key == FEATURE_APPROVED)
    planner.on_bar(
        make_view(
            index=1,
            close=102,
            position=make_position(
                side=PositionSide.SHORT,
                quantity=1.1,
                entry_price=100.1818181818,
                stop_loss=105,
                entry_fill_count=2,
            ),
            features=(approved,),
        )
    )
    state = planner.snapshot_state()
    assert state["completed_stages"] == [1]
    assert state["pending_plan"] is None


def test_conflicting_approval_and_rejection_feedback_fails_closed() -> None:
    planner = _module()
    plan = _plan(planner)
    approval_module = PositionScalingApprovalModule()
    approved_result = approval_module.on_bar(
        make_view(position=make_position(), features=(plan, _risk(plan.value)))
    )
    approved = next(item for item in approved_result.features if item.key == FEATURE_APPROVED)
    rejected_value = deep_thaw(approved.value)
    rejected_value.update(
        reason_code=ReasonCode.RISK_REJECTED.value,
        message="conflicting terminal feedback",
    )
    rejected = feature(
        FEATURE_REJECTED,
        rejected_value,
        index=1,
        source="position_scaling_approval",
        feature_id="conflicting-rejection",
    )
    result = planner.on_bar(
        make_view(
            index=1,
            position=make_position(),
            features=(*standard_features(1), approved, rejected),
        )
    )
    assert result.skips == (ReasonCode.CONFLICT.value,)
    assert planner.snapshot_state()["lifecycle"] == "recovery_required"


def test_planner_and_approval_emit_no_direct_execution_actions() -> None:
    planner = _module()
    plan_result = planner.on_bar(
        make_view(close=98, position=make_position(pnl=-20), features=standard_features())
    )
    assert not plan_result.orders
    assert not plan_result.cancellations
    assert not plan_result.position_updates
    assert not plan_result.closes

    plan = next(item for item in plan_result.features if item.key == FEATURE_PLAN)
    approval_result = PositionScalingApprovalModule().on_bar(
        make_view(position=make_position(), features=(plan, _risk(plan.value)))
    )
    assert not approval_result.orders
    assert not approval_result.cancellations
    assert not approval_result.position_updates
    assert not approval_result.closes
    approved = next(item.value for item in approval_result.features if item.key == FEATURE_APPROVED)
    assert Decimal(str(approved["approved_quantity"])) > 0


def test_hardening_manifests_match_public_contracts() -> None:
    from modular_strategy_position_scaling import __version__
    from modular_strategy_position_scaling.module import (
        FEATURE_RISK_REQUEST,
        FEATURE_STATUS,
    )

    planner = _module()
    approval = PositionScalingApprovalModule()
    assert __version__ == "0.2.2"
    assert planner.manifest.version == approval.manifest.version == "0.2.2"
    assert planner.manifest.api_version == approval.manifest.api_version == "1.0"
    assert planner.manifest.state_schema_version == "4"
    assert approval.manifest.state_schema_version == "4"
    assert {FEATURE_PLAN, FEATURE_RISK_REQUEST, FEATURE_STATUS}.issubset(
        planner.manifest.produced_features
    )
    assert {FEATURE_APPROVED, FEATURE_REJECTED}.issubset(planner.manifest.optional_features)
    assert FEATURE_PLAN in approval.manifest.required_features
    assert FEATURE_RISK_DECISION in approval.manifest.required_features


def test_feedback_dependencies_remain_optional_without_registry_cycle() -> None:
    from modular_core import (
        MarketView,
        ModuleDecision,
        ModuleManifest,
        ModuleRegistry,
        StrategyModule,
    )

    from modular_strategy_position_scaling.module import FEATURE_RISK_REQUEST

    class RiskContractStub(StrategyModule):
        name = "risk_contract_stub"
        manifest = ModuleManifest(
            name=name,
            required_features=(FEATURE_RISK_REQUEST,),
            produced_features=(FEATURE_RISK_DECISION,),
            skip_until_ready=False,
        )
        parameters: Mapping[str, object] = MappingProxyType({})

        def on_bar(self, view: MarketView) -> ModuleDecision:
            return ModuleDecision()

    planner = _module()
    approval = PositionScalingApprovalModule()
    dependency = ModuleRegistry().validate_and_plan(
        (approval, planner, RiskContractStub()), base_timeframe="1m"
    )
    assert tuple(item.name for item in dependency.modules) == (
        "position_scaling_planner",
        "risk_contract_stub",
        "position_scaling_approval",
    )


def test_public_dto_payload_is_deeply_immutable() -> None:
    plan_feature = _plan(_module())
    plan = PositionAdjustmentPlan.from_mapping(plan_feature.value)
    with pytest.raises(TypeError):
        plan.metadata["mutate"] = True  # type: ignore[index]
    assert isinstance(plan.source_feature_ids, tuple)
    assert plan.to_mapping()["schema_version"] == "1"


def test_operational_partial_approval_checkpoint_replay_continuation() -> None:
    planner = _module()
    plan = _plan(planner)
    approval_decision = PositionScalingApprovalModule().on_bar(
        make_view(position=make_position(), features=(plan, _risk(plan.value, quantity="0.1")))
    )
    approved = next(item for item in approval_decision.features if item.key == FEATURE_APPROVED)

    first = planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=(*standard_features(1), approved),
        )
    )
    assert first.skips == (ReasonCode.PENDING_ADJUSTMENT.value,)
    checkpoint = planner.snapshot_state()
    assert checkpoint["pending_approved_quantity"] == "0.1"

    restored = _module()
    restored.restore_state(checkpoint)
    partial = restored.on_bar(
        make_view(
            index=2,
            close=98,
            position=make_position(quantity=1.05, entry_price=99.9047619048),
            features=(*standard_features(2), approved),
        )
    )
    assert partial.skips == (ReasonCode.PARTIAL_FILL.value,)
    partial_checkpoint = restored.snapshot_state()

    restarted = _module()
    restarted.restore_state(partial_checkpoint)
    restarted.on_bar(
        make_view(
            index=3,
            close=98,
            position=make_position(quantity=1.1, entry_price=99.8181818182),
            features=standard_features(3),
        )
    )
    final_state = restarted.snapshot_state()
    assert final_state["completed_stages"] == [1]
    assert final_state["pending_plan"] is None
    assert final_state["scale_in_count"] == 1

    out_of_order = restarted.on_bar(
        make_view(index=2, close=98, position=make_position(quantity=1.1))
    )
    assert out_of_order.skips == (ReasonCode.OUT_OF_ORDER.value,)


def test_operational_instances_do_not_share_feedback_or_state() -> None:
    first = _module()
    second = _module()
    plan = _plan(first)
    approved = next(
        item
        for item in PositionScalingApprovalModule()
        .on_bar(make_view(position=make_position(), features=(plan, _risk(plan.value))))
        .features
        if item.key == FEATURE_APPROVED
    )
    first.on_bar(
        make_view(
            index=1,
            position=make_position(),
            features=(*standard_features(1), approved),
        )
    )
    assert first.snapshot_state()["pending_approved_quantity"] == "0.25"
    assert second.snapshot_state()["position_id"] is None
    assert second.snapshot_state()["pending_approved_quantity"] is None
