from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from conftest import feature, make_position, make_view, standard_features
from modular_core import PositionSide

from modular_strategy_position_scaling import (
    FEATURE_PLAN,
    FEATURE_REJECTED,
    FEATURE_RISK_DECISION,
    FEATURE_RISK_REQUEST,
    PositionAdjustmentPlan,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)
from modular_strategy_position_scaling.enums import ReasonCode


def averaging_module(*, expires_bars: int = 5, **extra: object) -> PositionScalingPlannerModule:
    parameters: dict[str, object] = {
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
                "expires_bars": expires_bars,
                "cooldown_bars": 1,
            }
        ],
    }
    parameters.update(extra)
    return PositionScalingPlannerModule(**parameters)


def plan_and_risk(module: PositionScalingPlannerModule, *, index: int = 0) -> tuple[object, object]:
    decision = module.on_bar(
        make_view(
            index=index,
            close=98,
            position=make_position(pnl=-20),
            features=standard_features(index),
        )
    )
    by_key = {item.key: item for item in decision.features}
    return by_key[FEATURE_PLAN], by_key[FEATURE_RISK_REQUEST]


def approved_risk(plan: object, *, index: int = 0, quantity: str = "0.25") -> object:
    value = plan.value  # type: ignore[attr-defined]
    return feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": value["risk_request_id"],
            "symbol": value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": quantity,
            "setup_id": value["setup_id"],
        },
        index=index,
        source="risk_sizing",
    )


def approval_feedback(plan: object, *, quantity: str = "0.25") -> object:
    risk = approved_risk(plan, quantity=quantity)
    decision = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    )
    return next(item for item in decision.features if item.key == "position_scaling.approved")


def test_plan_ttl_uses_completed_bar_duration() -> None:
    plan_feature, _ = plan_and_risk(averaging_module(expires_bars=2))
    plan = PositionAdjustmentPlan.from_mapping(plan_feature.value)  # type: ignore[attr-defined]
    assert plan.expires_at - plan.decision_time == timedelta(minutes=2)
    assert plan.expires_index - plan.decision_index == 2


def test_stop_scaling_zero_quantity_roundtrips() -> None:
    module = PositionScalingPlannerModule(
        enabled=True,
        require_setup_validity=False,
        require_liquidation_distance=False,
        require_margin_usage=False,
        rules=[
            {
                "stage": 1,
                "mode": "scale_out",
                "action": "stop_scaling",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": 1,
                "expires_bars": 0,
            }
        ],
    )
    decision = module.on_bar(make_view(position=make_position()))
    raw = next(item.value for item in decision.features if item.key == FEATURE_PLAN)
    plan = PositionAdjustmentPlan.from_mapping(raw)
    assert plan.desired_quantity == Decimal("0")
    assert PositionAdjustmentPlan.from_mapping(plan.to_mapping()) == plan


def test_partial_fill_does_not_complete_stage_until_target_reached() -> None:
    module = averaging_module()
    plan, _ = plan_and_risk(module)
    approved = approval_feedback(plan)
    partial = module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=1.1, entry_price=99.8, entry_fill_count=2),
            features=(*standard_features(1), approved),
        )
    )
    state = module.snapshot_state()
    assert ReasonCode.PARTIAL_FILL.value in partial.skips
    assert state["lifecycle"] == "partially_filled"
    assert state["completed_stages"] == []
    assert state["pending_plan"] is not None

    module.on_bar(
        make_view(
            index=2,
            close=98,
            position=make_position(quantity=1.25, entry_price=99.6, entry_fill_count=3),
            features=standard_features(2),
        )
    )
    state = module.snapshot_state()
    assert state["completed_stages"] == [1]
    assert state["scale_in_count"] == 1
    assert state["pending_plan"] is None


def test_partial_fill_survives_checkpoint_restore() -> None:
    module = averaging_module()
    plan, _ = plan_and_risk(module)
    approved = approval_feedback(plan)
    module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=1.1, entry_price=99.8, entry_fill_count=2),
            features=(*standard_features(1), approved),
        )
    )
    snapshot = module.snapshot_state()
    restored = averaging_module()
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    restored.on_bar(
        make_view(
            index=2,
            close=98,
            position=make_position(quantity=1.25, entry_price=99.6, entry_fill_count=3),
            features=standard_features(2),
        )
    )
    assert restored.snapshot_state()["completed_stages"] == [1]


def test_opposite_position_change_while_pending_requires_recovery() -> None:
    module = averaging_module()
    plan_and_risk(module)
    result = module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=0.9, entry_price=100),
            features=standard_features(1),
        )
    )
    assert ReasonCode.POSITION_SNAPSHOT_MISMATCH.value in result.skips
    assert module.snapshot_state()["lifecycle"] == "recovery_required"


def test_stop_weakening_fails_closed_for_long_and_short() -> None:
    long_module = averaging_module()
    plan_and_risk(long_module)
    long_result = long_module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(stop_loss=94),
            features=standard_features(1),
        )
    )
    assert ReasonCode.STOP_WEAKENING_FORBIDDEN.value in long_result.skips

    short_module = averaging_module()
    short_module.on_bar(
        make_view(
            close=102,
            position=make_position(side=PositionSide.SHORT, stop_loss=105),
            features=standard_features(),
        )
    )
    short_result = short_module.on_bar(
        make_view(
            index=1,
            close=102,
            position=make_position(side=PositionSide.SHORT, stop_loss=106),
            features=standard_features(1),
        )
    )
    assert ReasonCode.STOP_WEAKENING_FORBIDDEN.value in short_result.skips


def test_missing_instrument_rules_can_retry_without_duplicate_poisoning() -> None:
    module = averaging_module()
    missing = tuple(item for item in standard_features() if item.key != "instrument.rules")
    first = module.on_bar(make_view(close=98, position=make_position(), features=missing))
    assert ReasonCode.INVALID_RULE.value in first.skips
    second = module.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    assert FEATURE_PLAN in {item.key for item in second.features}


def test_pending_scale_in_is_invalidated_by_gap_and_exit_priority() -> None:
    gap_module = averaging_module()
    plan_and_risk(gap_module)
    gap = gap_module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=standard_features(1),
            data_gap=True,
        )
    )
    assert ReasonCode.DATA_GAP.value in gap.skips
    assert gap_module.snapshot_state()["pending_plan"] is None

    exit_module = averaging_module()
    plan_and_risk(exit_module)
    exit_result = exit_module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=standard_features(1),
            exit_pending=True,
        )
    )
    assert ReasonCode.EXIT_PENDING.value in exit_result.skips
    assert exit_module.snapshot_state()["pending_plan"] is None


def test_approval_rejects_changed_position_snapshot() -> None:
    planner = averaging_module(expires_bars=0)
    plan, _ = plan_and_risk(planner)
    risk = approved_risk(plan)
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(
            close=98,
            position=make_position(quantity=1.1, entry_price=99.8),
            features=(plan, risk),
        )
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.POSITION_SNAPSHOT_MISMATCH.value


def test_approval_rejects_increase_on_gap_but_allows_reduction() -> None:
    planner = averaging_module(expires_bars=0)
    plan, _ = plan_and_risk(planner)
    risk = approved_risk(plan)
    result = PositionScalingApprovalModule().on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=(plan, risk),
            data_gap=True,
        )
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.DATA_GAP.value


def test_custom_feature_trigger_is_declared_and_consumed() -> None:
    module = PositionScalingPlannerModule(
        enabled=True,
        allow_fixed_dca=True,
        require_setup_validity=False,
        require_liquidation_distance=False,
        require_margin_usage=False,
        rules=[
            {
                "stage": 1,
                "mode": "fixed_dca",
                "action": "add",
                "trigger_kind": "feature_true",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "feature_key": "custom.confirmation",
                "expires_bars": 0,
            }
        ],
        instrument_rules={"quantity_step": "0.001", "contract_size": "1"},
    )
    assert "custom.confirmation" in module.manifest.optional_features
    decision = module.on_bar(
        make_view(
            position=make_position(),
            features=(feature("custom.confirmation", True),),
        )
    )
    assert FEATURE_PLAN in {item.key for item in decision.features}


def test_risk_request_uses_explicit_available_margin() -> None:
    module = averaging_module()
    features = (
        *(item for item in standard_features() if item.key != "position_scaling.context"),
        feature(
            "position_scaling.context",
            {
                "liquidation_distance_fraction": "0.2",
                "margin_usage_fraction": "0.1",
                "available_margin": "1234.5",
            },
        ),
    )
    decision = module.on_bar(
        make_view(close=98, position=make_position(), features=features, cash=9999)
    )
    request = next(item.value for item in decision.features if item.key == FEATURE_RISK_REQUEST)
    assert request["available_margin"] == "1234.5"


def test_schema_one_planner_state_migrates_to_schema_two() -> None:
    module = averaging_module()
    snapshot = dict(module.snapshot_state())
    snapshot["schema_version"] = "1"
    snapshot.pop("state_fingerprint", None)
    snapshot.pop("last_adjustment_price", None)
    restored = averaging_module()
    restored.restore_state(snapshot)
    assert restored.snapshot_state()["schema_version"] == "4"


def test_approval_manifest_requires_risk_decision_for_dependency_order() -> None:
    assert FEATURE_RISK_DECISION in PositionScalingApprovalModule.manifest.required_features
