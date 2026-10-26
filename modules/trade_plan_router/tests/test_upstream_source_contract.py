from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from modular_core import OrderType, Side
from modular_modules.entry_model.enums import EntryKind
from modular_modules.entry_model.plan import EntryPlan, deterministic_plan_id
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    ReasonCode as ScalingReasonCode,
    ScalingMode,
)
from modular_strategy_position_scaling.models import ApprovedAdjustment, PositionAdjustmentPlan
from modular_strategy_protection_target.models import ProtectionPlan
from strategy_modules.risk_sizing.decision import RiskDecision
from strategy_modules.risk_sizing.enums import RiskDecisionStatus

from modular_strategy_trade_plan_router import DecisionNormalizer, ScalingDirection, TradeSide

from conftest import NOW


def _released_entry_plan() -> EntryPlan:
    identity = {
        "schema_version": "2",
        "module_version": "0.2.0",
        "setup_id": "setup-source",
        "setup_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_kind": "limit_explicit",
        "order_type": "limit",
        "created_at": NOW.isoformat(),
        "setup_confirmed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_bars": 12,
        "price": "100",
        "trigger_price": None,
        "limit_price": "100",
        "post_only": False,
        "rationale": "source-contract",
        "metadata": {"strategy_id": "strategy-source", "market": "usdt_perpetual"},
    }
    return EntryPlan(
        plan_id=deterministic_plan_id(identity),
        module_version="0.2.0",
        setup_id="setup-source",
        setup_fingerprint="a" * 64,
        config_fingerprint="b" * 64,
        symbol="BTCUSDT",
        side=Side.BUY,
        entry_kind=EntryKind.LIMIT_EXPLICIT,
        order_type=OrderType.LIMIT,
        created_at=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        rationale="source-contract",
        metadata={"strategy_id": "strategy-source", "market": "usdt_perpetual"},
    )


def test_released_entry_source_serialization_normalizes_without_fixture_drift():
    plan = _released_entry_plan()
    normalized = DecisionNormalizer().normalize_entry(
        plan.to_feature_value(),
        feature_key="entry_model.plan",
        feature_schema_version="2",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id=plan.plan_id,
        source_module="entry_model",
        symbol="BTCUSDT",
        portfolio_id="portfolio-source",
    )
    assert normalized.candidate_id == plan.plan_id
    assert normalized.strategy_id == "strategy-source"
    assert normalized.side is TradeSide.LONG
    assert normalized.limit_price == Decimal("100")


def test_released_protection_source_serialization_normalizes_without_fixture_drift():
    entry = _released_entry_plan()
    plan = ProtectionPlan(
        protection_plan_id="protection-source",
        entry_plan_id=entry.plan_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        timeframe="5m",
        side=Side.BUY,
        entry_price=Decimal("100"),
        invalidation_price=Decimal("94"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        risk_distance=Decimal("5"),
        reward_distance=Decimal("10"),
        stop_distance_bps=Decimal("500"),
        reward_r=Decimal("2"),
        invalidation_mode="explicit",
        target_mode="r_multiple",
        created_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        source_entry_feature_id=entry.plan_id,
        source_invalidation_feature_id="invalidation-source",
        source_target_feature_id="target-source",
    )
    normalized = DecisionNormalizer().normalize_protection(
        plan.as_mapping(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id=plan.protection_plan_id,
        symbol="BTCUSDT",
    )
    assert normalized.decision_id == plan.protection_plan_id
    assert normalized.entry_candidate_id == entry.plan_id
    assert normalized.stop_loss == Decimal("95")


def test_released_risk_source_serialization_normalizes_without_fixture_drift():
    entry = _released_entry_plan()
    decision = RiskDecision(
        request_id="risk-source",
        symbol="BTCUSDT",
        status=RiskDecisionStatus.APPROVED,
        quantity=Decimal("10"),
        raw_quantity=Decimal("10.1"),
        risk_fraction=Decimal("0.005"),
        risk_budget=Decimal("50"),
        actual_risk_amount=Decimal("50"),
        actual_risk_fraction=Decimal("0.005"),
        notional=Decimal("1000"),
        required_leverage=Decimal("1"),
        setup_id=entry.setup_id,
        diagnostics={
            "entry_plan_id": entry.plan_id,
            "protection_plan_id": "protection-source",
        },
    )
    normalized = DecisionNormalizer().normalize_sizing(
        decision.to_mapping(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="risk-feature-source",
        symbol="BTCUSDT",
    )
    assert normalized.approved is True
    assert normalized.entry_candidate_id == entry.plan_id
    assert normalized.protection_decision_id == "protection-source"


def test_released_scaling_source_serialization_preserves_decrease_direction():
    plan = PositionAdjustmentPlan(
        adjustment_id="adjustment-source",
        position_id="position-source",
        setup_id="setup-source",
        symbol="BTCUSDT",
        side="long",
        action=AdjustmentAction.REDUCE,
        direction=AdjustmentDirection.DECREASE,
        scaling_mode=ScalingMode.SCALE_OUT,
        stage=1,
        decision_time=NOW,
        decision_index=10,
        desired_entry_kind="limit",
        desired_price=Decimal("100"),
        trigger_price=Decimal("101"),
        desired_quantity=Decimal("2"),
        maximum_quantity=Decimal("2"),
        risk_budget=Decimal("50"),
        notional_budget=Decimal("1000"),
        current_average_price=Decimal("100"),
        expected_average_price=Decimal("100"),
        current_quantity=Decimal("10"),
        expected_total_quantity=Decimal("8"),
        current_total_risk=Decimal("50"),
        projected_total_risk=Decimal("40"),
        maximum_total_risk=Decimal("50"),
        stop_price=Decimal("95"),
        invalidation_price=Decimal("94"),
        expires_at=NOW + timedelta(minutes=5),
        expires_index=11,
        reason_code=ScalingReasonCode.REDUCTION_APPROVED,
        risk_request_id=None,
        metadata={"source": "contract-test"},
        source_feature_ids=("position-source-feature",),
        config_fingerprint="c" * 64,
    )
    approved = ApprovedAdjustment(
        adjustment_id=plan.adjustment_id,
        position_id=plan.position_id,
        symbol=plan.symbol,
        side=plan.side,
        action=plan.action,
        direction=plan.direction,
        approved_quantity=Decimal("2"),
        desired_price=plan.desired_price,
        risk_request_id=None,
        reason_code=ScalingReasonCode.REDUCTION_APPROVED,
        approved_at=NOW,
        plan=plan.to_mapping(),
    )
    normalized = DecisionNormalizer().normalize_scaling(
        approved.to_mapping(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="scaling-feature-source",
        symbol="BTCUSDT",
    )
    assert normalized.direction is ScalingDirection.DECREASE
    assert normalized.approved_quantity == Decimal("2")
    assert normalized.position_id == "position-source"
    assert normalized.order_type == "limit"
    assert normalized.trigger_price == Decimal("101")
