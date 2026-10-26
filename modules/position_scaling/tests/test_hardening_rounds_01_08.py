from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from modular_strategy_position_scaling import (
    AdjustmentAction,
    PositionScalingConfig,
    QuantityKind,
    ScalingContext,
    ScalingMode,
    ScalingPlanner,
    ScalingRule,
    TriggerKind,
)
from modular_strategy_position_scaling.decimal_utils import weighted_average


def test_round_01_weighted_average_precision_is_operand_order_independent() -> None:
    low_precision_price = Decimal("100")
    low_precision_quantity = Decimal("1")
    high_precision_price = Decimal("98.12345678901234567890123456789012345678901234567890123456789")
    high_precision_quantity = Decimal(
        "0.12345678901234567890123456789012345678901234567890123456789"
    )
    forward = weighted_average(
        low_precision_price,
        low_precision_quantity,
        high_precision_price,
        high_precision_quantity,
    )
    reverse = weighted_average(
        high_precision_price,
        high_precision_quantity,
        low_precision_price,
        low_precision_quantity,
    )
    assert forward == reverse
    assert (
        min(low_precision_price, high_precision_price)
        < forward
        < max(low_precision_price, high_precision_price)
    )


def _round_context(*, side: str, current_price: str, stop: str) -> ScalingContext:
    return ScalingContext(
        position_id=f"round-{side}",
        setup_id="setup",
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal(stop),
        stop_price=Decimal(stop),
        current_price=Decimal(current_price),
        unrealized_pnl=Decimal("-1"),
        equity=Decimal("100000"),
        cash=Decimal("90000"),
        index=10,
        attached_index=0,
        decision_time=datetime(2026, 1, 1, tzinfo=UTC),
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
        liquidation_distance_fraction=Decimal("0.2"),
        margin_usage_fraction=Decimal("0.1"),
        available_margin=Decimal("90000"),
        source_feature_ids=(),
        features={},
    )


def test_round_02_buy_sell_projected_risk_is_exactly_symmetric() -> None:
    rule = ScalingRule(
        stage=1,
        mode=ScalingMode.FIXED_DCA,
        action=AdjustmentAction.ADD,
        trigger_kind=TriggerKind.ALWAYS,
        trigger_value=Decimal("0"),
        quantity_kind=QuantityKind.FIXED,
        quantity_value=Decimal("0.3333333333333333333333333333333333"),
        expires_bars=0,
    )
    config = PositionScalingConfig(
        enabled=True,
        allow_fixed_dca=True,
        max_total_position_risk_fraction=Decimal("0.5"),
        max_additional_risk_fraction=Decimal("0.5"),
        rules=(rule,),
    )
    planner = ScalingPlanner(config)
    long_plan = planner.evaluate_rule(
        _round_context(side="long", current_price="98", stop="90"), rule
    ).plan
    short_plan = planner.evaluate_rule(
        _round_context(side="short", current_price="102", stop="110"), rule
    ).plan
    assert long_plan is not None and short_plan is not None
    assert long_plan.current_total_risk == short_plan.current_total_risk == Decimal("10")
    assert long_plan.projected_total_risk == short_plan.projected_total_risk
    assert long_plan.expected_total_quantity == short_plan.expected_total_quantity
    assert long_plan.expected_average_price + short_plan.expected_average_price == Decimal("200")


def test_round_03_zero_ttl_feedback_is_visible_only_on_immediate_next_bar() -> None:
    from conftest import feature, make_position, make_view, standard_features
    from modular_core import FeatureView

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

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
                "quantity_value": "0.1",
                "expires_bars": 0,
            }
        ],
    )
    planned = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    )
    approved = next(item for item in approval.features if item.key == FEATURE_APPROVED)
    next_bar = make_view(index=1, close=98, position=make_position())
    later_bar = make_view(index=2, close=98, position=make_position())
    assert (
        FeatureView((approved,), next_bar.current.close_time).latest(FEATURE_APPROVED) is approved
    )
    assert FeatureView((approved,), later_bar.current.close_time).latest(FEATURE_APPROVED) is None


def test_round_04_gap_and_exit_override_fresh_risk_approval() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
        ReasonCode,
    )

    for reason, view_flags in (
        (ReasonCode.DATA_GAP, {"data_gap": True}),
        (ReasonCode.EXIT_PENDING, {"exit_pending": True}),
    ):
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
                    "quantity_value": "0.1",
                    "expires_bars": 5,
                }
            ],
        )
        planned = planner.on_bar(
            make_view(close=98, position=make_position(), features=standard_features())
        )
        plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
        risk = feature(
            FEATURE_RISK_DECISION,
            {
                "schema_version": "1",
                "request_id": plan.value["risk_request_id"],
                "symbol": plan.value["symbol"],
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
        result = planner.on_bar(
            make_view(
                index=1,
                close=98,
                position=make_position(),
                features=(*standard_features(1), approved),
                **view_flags,
            )
        )
        assert result.skips == (reason.value,)
        state = planner.snapshot_state()
        assert state["pending_plan"] is None
        assert state["lifecycle"] == "invalidated"


def test_round_05_approved_feedback_parser_rejects_contract_drift() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        ApprovedAdjustment,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )
    from modular_strategy_position_scaling.canonical import deep_thaw

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
                "quantity_value": "0.1",
                "expires_bars": 1,
            }
        ],
    )
    planned = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    decision = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    )
    approved = next(item.value for item in decision.features if item.key == FEATURE_APPROVED)
    assert ApprovedAdjustment.from_mapping(approved).to_mapping() == deep_thaw(approved)

    malformed = deep_thaw(approved)
    malformed["direction"] = "decrease"
    try:
        ApprovedAdjustment.from_mapping(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("action/direction drift must be rejected")

    malformed = deep_thaw(approved)
    malformed["approved_quantity"] = "999"
    try:
        ApprovedAdjustment.from_mapping(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("quantity above the embedded plan must be rejected")


def test_round_06_planner_uses_typed_feedback_contracts() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
        RejectedAdjustment,
    )
    from modular_strategy_position_scaling.canonical import deep_thaw

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
                "quantity_value": "0.1",
                "expires_bars": 3,
            }
        ],
    )
    planned = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approval_decision = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    )
    approved = next(item for item in approval_decision.features if item.key == FEATURE_APPROVED)
    malformed = deep_thaw(approved.value)
    malformed["plan"]["desired_price"] = "97"
    malformed_feature = type(approved)(
        key=approved.key,
        source_module=approved.source_module,
        created_at=approved.created_at,
        value=malformed,
        symbol=approved.symbol,
        timeframe=approved.timeframe,
        valid_until=approved.valid_until,
        schema_version=approved.schema_version,
        feature_id=f"{approved.feature_id}-malformed",
        invalidate_on_gap=approved.invalidate_on_gap,
        metadata=approved.metadata,
    )
    invalid = planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=(*standard_features(1), malformed_feature),
        )
    )
    assert invalid.skips == ("approval_feedback_invalid",)
    assert planner.snapshot_state()["lifecycle"] == "recovery_required"

    rejected_value = {
        "schema_version": "1",
        "adjustment_id": plan.value["adjustment_id"],
        "position_id": plan.value["position_id"],
        "symbol": plan.value["symbol"],
        "reason_code": "risk_rejected",
        "message": "risk rejected",
        "rejected_at": "2026-01-01T00:02:00+00:00",
        "risk_request_id": plan.value["risk_request_id"],
    }
    parsed = RejectedAdjustment.from_mapping(rejected_value)
    assert parsed.to_mapping() == rejected_value
    rejected_value["unexpected"] = True
    try:
        RejectedAdjustment.from_mapping(rejected_value)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown rejected feedback fields must be rejected")


def test_round_07_output_constructor_matches_public_parser() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        ApprovedAdjustment,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )
    from modular_strategy_position_scaling.canonical import deep_thaw

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
                "quantity_value": "0.1",
            }
        ],
    )
    planned = planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approved_feature = next(
        item
        for item in PositionScalingApprovalModule()
        .on_bar(make_view(close=98, position=make_position(), features=(plan, risk)))
        .features
        if item.key == FEATURE_APPROVED
    )
    payload = deep_thaw(approved_feature.value)
    assert ApprovedAdjustment.from_mapping(payload).to_mapping() == payload
    for field, replacement in (
        ("reason_code", "reduction_approved"),
        ("risk_request_id", "wrong"),
    ):
        malformed = deep_thaw(payload)
        malformed[field] = replacement
        try:
            ApprovedAdjustment.from_mapping(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{field} drift must fail at construction")


def test_round_08_terminal_feature_ids_are_deterministic() -> None:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_REJECTED,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

    def planned_feature() -> object:
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
                    "quantity_value": "0.1",
                }
            ],
        )
        return next(
            item
            for item in planner.on_bar(
                make_view(close=98, position=make_position(), features=standard_features())
            ).features
            if item.key == FEATURE_PLAN
        )

    plan = planned_feature()
    approved_risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approved_outputs = [
        next(
            item
            for item in PositionScalingApprovalModule()
            .on_bar(make_view(close=98, position=make_position(), features=(plan, approved_risk)))
            .features
            if item.key == FEATURE_APPROVED
        )
        for _ in range(2)
    ]
    assert approved_outputs[0].feature_id == approved_outputs[1].feature_id
    assert approved_outputs[0].value == approved_outputs[1].value

    rejected_risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "rejected",
            "approved": False,
            "quantity": "0",
            "setup_id": plan.value["setup_id"],
            "message": "capacity exhausted",
        },
        source="risk_sizing",
        feature_id="risk-rejected",
    )
    rejected_outputs = [
        next(
            item
            for item in PositionScalingApprovalModule()
            .on_bar(make_view(close=98, position=make_position(), features=(plan, rejected_risk)))
            .features
            if item.key == FEATURE_REJECTED
        )
        for _ in range(2)
    ]
    assert rejected_outputs[0].feature_id == rejected_outputs[1].feature_id
    assert rejected_outputs[0].value == rejected_outputs[1].value
