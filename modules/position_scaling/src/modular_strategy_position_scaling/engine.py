from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .canonical import stable_id
from .config import PositionScalingConfig
from .decimal_utils import ZERO, canonical_decimal, positive_decimal, weighted_average
from .enums import (
    AdjustmentAction,
    AdjustmentDirection,
    QuantityKind,
    ReasonCode,
    ScalingMode,
    TriggerKind,
)
from .models import PositionAdjustmentPlan, ScalingRule


@dataclass(frozen=True, slots=True)
class ScalingContext:
    position_id: str
    setup_id: str | None
    symbol: str
    side: str
    quantity: Decimal
    average_price: Decimal
    initial_quantity: Decimal
    initial_entry_price: Decimal
    initial_stop_price: Decimal
    stop_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    cash: Decimal
    index: int
    attached_index: int
    decision_time: datetime
    bar_seconds: int
    last_adjustment_index: int | None
    last_adjustment_price: Decimal | None
    scale_in_count: int
    scale_out_count: int
    data_gap: bool
    exit_pending: bool
    pending_order_count: int
    setup_valid: bool | None
    market_regime: str | None
    atr: Decimal | None
    liquidation_distance_fraction: Decimal | None
    margin_usage_fraction: Decimal | None
    available_margin: Decimal | None
    source_feature_ids: tuple[str, ...]
    features: Mapping[str, Any]

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def current_r_multiple(self) -> Decimal:
        unit_risk = abs(self.initial_entry_price - self.initial_stop_price)
        if unit_risk <= ZERO:
            return ZERO
        favorable = (
            self.current_price - self.initial_entry_price
            if self.is_long
            else self.initial_entry_price - self.current_price
        )
        return favorable / unit_risk

    @property
    def drawdown_fraction(self) -> Decimal:
        if self.equity <= ZERO or self.unrealized_pnl >= ZERO:
            return ZERO
        return abs(self.unrealized_pnl) / self.equity

    @property
    def bars_held(self) -> int:
        return max(0, self.index - self.attached_index)


@dataclass(frozen=True, slots=True)
class PlanResult:
    plan: PositionAdjustmentPlan | None
    reason: ReasonCode
    message: str = ""


class ScalingPlanner:
    def __init__(self, config: PositionScalingConfig) -> None:
        self.config = config

    def evaluate_rule(self, context: ScalingContext, rule: ScalingRule) -> PlanResult:
        quantity, direction = self._desired_quantity(context, rule)
        reason = self._precheck(context, rule, direction)
        if reason is not None:
            return PlanResult(None, reason)
        if not self._triggered(context, rule):
            return PlanResult(None, ReasonCode.NO_TRIGGER)
        if rule.action is not AdjustmentAction.STOP_SCALING and (
            quantity <= ZERO or direction is AdjustmentDirection.NONE
        ):
            return PlanResult(None, ReasonCode.INVALID_QUANTITY)
        desired_price = rule.explicit_price or context.current_price
        if desired_price <= ZERO:
            return PlanResult(None, ReasonCode.INVALID_GEOMETRY)
        if direction is AdjustmentDirection.INCREASE:
            expected_quantity = context.quantity + quantity
            expected_average = weighted_average(
                context.average_price,
                context.quantity,
                desired_price,
                quantity,
            )
        elif direction is AdjustmentDirection.DECREASE:
            if quantity >= context.quantity:
                quantity = context.quantity - self.config.minimum_residual_quantity
            if quantity <= ZERO:
                return PlanResult(None, ReasonCode.INVALID_QUANTITY)
            expected_quantity = context.quantity - quantity
            if (
                expected_quantity <= ZERO
                or expected_quantity < self.config.minimum_residual_quantity
            ):
                return PlanResult(None, ReasonCode.INVALID_QUANTITY)
            expected_average = context.average_price
        else:
            expected_quantity = context.quantity
            expected_average = context.average_price

        try:
            contract_size = self._contract_size(context)
        except ValueError:
            return PlanResult(None, ReasonCode.INVALID_RULE)
        current_risk = (
            self._risk_amount(
                context.is_long, context.average_price, context.stop_price, context.quantity
            )
            * contract_size
        )
        projected_risk = (
            self._risk_amount(
                context.is_long, expected_average, context.stop_price, expected_quantity
            )
            * contract_size
        )
        maximum_risk = context.equity * self.config.max_total_position_risk_fraction
        additional_risk = max(ZERO, projected_risk - current_risk)
        notional = expected_quantity * expected_average * contract_size
        maximum_quantity = quantity
        safety = self._projected_safety(
            context,
            direction=direction,
            quantity=quantity,
            expected_quantity=expected_quantity,
            expected_average=expected_average,
            desired_price=desired_price,
            current_risk=current_risk,
            projected_risk=projected_risk,
            maximum_risk=maximum_risk,
            additional_risk=additional_risk,
            expected_notional=notional,
        )
        if safety is not None:
            return PlanResult(None, safety)

        adjustment_id = stable_id(
            "adjustment",
            {
                "position_id": context.position_id,
                "setup_id": context.setup_id,
                "symbol": context.symbol,
                "stage": rule.stage,
                "index": context.index,
                "action": rule.action,
                "direction": direction,
                "mode": rule.mode,
                "price": desired_price,
                "quantity": quantity,
                "config": self.config.fingerprint,
            },
        )
        risk_request_id = (
            f"riskreq-{adjustment_id.removeprefix('adjustment-')}"
            if direction is AdjustmentDirection.INCREASE
            else None
        )
        expires_index = context.index + rule.expires_bars
        expires_at = context.decision_time + timedelta(
            seconds=max(0, context.bar_seconds * rule.expires_bars)
        )
        plan = PositionAdjustmentPlan(
            adjustment_id=adjustment_id,
            position_id=context.position_id,
            setup_id=context.setup_id,
            symbol=context.symbol,
            side=context.side,
            action=rule.action,
            direction=direction,
            scaling_mode=rule.mode,
            stage=rule.stage,
            decision_time=context.decision_time,
            decision_index=context.index,
            desired_entry_kind=rule.desired_entry_kind,
            desired_price=desired_price,
            trigger_price=rule.explicit_price or context.current_price,
            desired_quantity=quantity,
            maximum_quantity=maximum_quantity,
            risk_budget=context.equity * self.config.max_additional_risk_fraction,
            notional_budget=(
                self.config.max_total_notional
                if self.config.max_total_notional is not None
                else context.equity * self.config.max_total_leverage
            ),
            current_average_price=context.average_price,
            expected_average_price=expected_average,
            current_quantity=context.quantity,
            expected_total_quantity=expected_quantity,
            current_total_risk=current_risk,
            projected_total_risk=projected_risk,
            maximum_total_risk=maximum_risk,
            stop_price=context.stop_price,
            invalidation_price=context.initial_stop_price,
            expires_at=expires_at,
            expires_index=expires_index,
            reason_code=(
                ReasonCode.STOP_SCALING_REQUESTED
                if rule.action is AdjustmentAction.STOP_SCALING
                else ReasonCode.RISK_PENDING
                if direction is AdjustmentDirection.INCREASE
                else ReasonCode.PLAN_CREATED
            ),
            risk_request_id=risk_request_id,
            metadata={
                "rule": rule.to_mapping(),
                "current_r_multiple": str(context.current_r_multiple),
                "bars_held": context.bars_held,
                "drawdown_fraction": str(context.drawdown_fraction),
                "cooldown_bars": rule.cooldown_bars,
            },
            source_feature_ids=context.source_feature_ids,
            config_fingerprint=self.config.fingerprint,
        )
        return PlanResult(plan, plan.reason_code)

    def build_risk_request(
        self,
        context: ScalingContext,
        plan: PositionAdjustmentPlan,
        instrument_rules: Mapping[str, Any],
        portfolio_snapshot: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if plan.direction is not AdjustmentDirection.INCREASE or plan.risk_request_id is None:
            raise ValueError("risk request is valid only for increase plans")
        portfolio = dict(portfolio_snapshot or {})
        contract_size = positive_decimal(
            instrument_rules.get("contract_size", 1),
            "instrument.contract_size",
        )
        context_contract_size = self._contract_size(context)
        if context_contract_size != contract_size:
            raise ValueError("instrument contract_size disagrees with planning context")
        calculated_current_risk = (
            self._risk_amount(
                context.is_long,
                context.average_price,
                context.stop_price,
                context.quantity,
            )
            * contract_size
        )
        calculated_projected_risk = (
            self._risk_amount(
                context.is_long,
                plan.expected_average_price,
                plan.stop_price,
                plan.expected_total_quantity,
            )
            * contract_size
        )
        if (
            calculated_current_risk != plan.current_total_risk
            or calculated_projected_risk != plan.projected_total_risk
        ):
            raise ValueError("plan risk geometry disagrees with instrument contract_size")
        current_notional = context.quantity * context.current_price * contract_size
        projected_notional = plan.expected_total_quantity * plan.desired_price * contract_size
        portfolio.setdefault("gross_notional", str(current_notional))
        portfolio.setdefault("symbol_notional", str(current_notional))
        portfolio.setdefault("open_risk_amount", str(plan.current_total_risk))
        portfolio.setdefault("open_positions", 1)
        position_adjustment = {
            "schema_version": "1",
            "adjustment_id": plan.adjustment_id,
            "position_id": plan.position_id,
            "kind": "position_increase",
            "opens_new_position": False,
            "current_quantity": canonical_decimal(plan.current_quantity),
            "requested_quantity": canonical_decimal(plan.desired_quantity),
            "maximum_quantity": canonical_decimal(plan.maximum_quantity),
            "projected_quantity": canonical_decimal(plan.expected_total_quantity),
            "current_average_price": canonical_decimal(plan.current_average_price),
            "projected_average_price": canonical_decimal(plan.expected_average_price),
            "valuation_price": canonical_decimal(context.current_price),
            "current_total_risk": canonical_decimal(plan.current_total_risk),
            "projected_total_risk": canonical_decimal(plan.projected_total_risk),
            "current_notional": canonical_decimal(current_notional),
            "projected_notional": canonical_decimal(projected_notional),
        }
        return {
            "schema_version": "1",
            "request_id": plan.risk_request_id,
            "symbol": plan.symbol,
            "side": "buy" if context.is_long else "sell",
            "entry_price": str(plan.desired_price),
            "stop_loss": str(plan.stop_price),
            "equity": str(context.equity),
            "instrument": dict(instrument_rules),
            "portfolio": portfolio,
            "requested_risk_fraction": str(self.config.max_additional_risk_fraction),
            "available_margin": str(
                max(
                    ZERO,
                    context.available_margin
                    if context.available_margin is not None
                    else context.cash,
                )
            ),
            "setup_id": plan.setup_id,
            "position_adjustment": position_adjustment,
            "metadata": {
                "adjustment_id": plan.adjustment_id,
                "position_id": plan.position_id,
                "adjustment_kind": "position_increase",
                "opens_new_position": False,
                "current_quantity": canonical_decimal(plan.current_quantity),
                "desired_quantity": canonical_decimal(plan.desired_quantity),
                "maximum_quantity": canonical_decimal(plan.maximum_quantity),
                "requested_delta_quantity": canonical_decimal(plan.desired_quantity),
                "maximum_delta_quantity": canonical_decimal(plan.maximum_quantity),
                "projected_total_quantity": canonical_decimal(plan.expected_total_quantity),
                "current_average_price": canonical_decimal(plan.current_average_price),
                "projected_average_price": canonical_decimal(plan.expected_average_price),
                "valuation_price": canonical_decimal(context.current_price),
                "current_total_risk": canonical_decimal(plan.current_total_risk),
                "projected_total_risk": canonical_decimal(plan.projected_total_risk),
                "current_notional": canonical_decimal(current_notional),
                "projected_notional": canonical_decimal(projected_notional),
            },
        }

    def _contract_size(self, context: ScalingContext) -> Decimal:
        feature_rules = context.features.get(self.config.instrument_rules_feature)
        if isinstance(feature_rules, Mapping):
            return positive_decimal(
                feature_rules.get("contract_size", 1),
                "instrument.contract_size",
            )
        configured_rules = self.config.instrument_rules
        if isinstance(configured_rules, Mapping):
            return positive_decimal(
                configured_rules.get("contract_size", 1),
                "instrument.contract_size",
            )
        return Decimal("1")

    def _precheck(
        self,
        context: ScalingContext,
        rule: ScalingRule,
        direction: AdjustmentDirection,
    ) -> ReasonCode | None:
        increasing = direction is AdjustmentDirection.INCREASE
        decreasing = direction is AdjustmentDirection.DECREASE
        if context.stop_price <= ZERO:
            return ReasonCode.STOP_INVALID
        if context.is_long:
            if context.stop_price < context.initial_stop_price:
                return ReasonCode.STOP_WEAKENING_FORBIDDEN
        elif context.stop_price > context.initial_stop_price:
            return ReasonCode.STOP_WEAKENING_FORBIDDEN
        if rule.mode is ScalingMode.AVERAGING_DOWN and context.current_r_multiple >= ZERO:
            return ReasonCode.NO_TRIGGER
        if rule.mode is ScalingMode.PYRAMIDING and context.current_r_multiple <= ZERO:
            return ReasonCode.NO_TRIGGER
        if increasing and context.data_gap and self.config.suspend_additions_on_gap:
            return ReasonCode.DATA_GAP
        if increasing and context.exit_pending and self.config.block_additions_when_exit_pending:
            return ReasonCode.EXIT_PENDING
        if increasing and context.pending_order_count > 0:
            return ReasonCode.CONFLICTING_PENDING_ORDER
        if increasing and context.scale_in_count >= self.config.max_scale_in_count:
            return ReasonCode.MAX_SCALE_IN_COUNT
        if decreasing and context.scale_out_count >= self.config.max_scale_out_count:
            return ReasonCode.MAX_SCALE_OUT_COUNT
        if increasing and self.config.require_setup_validity and context.setup_valid is not True:
            return ReasonCode.SETUP_INVALID
        if increasing and self.config.require_market_regime:
            if (
                context.market_regime is None
                or context.market_regime not in self.config.allowed_market_regimes
            ):
                return ReasonCode.MARKET_REGIME_BLOCKED
        if increasing and context.bars_held > self.config.max_holding_bars_for_new_additions:
            return ReasonCode.MAX_HOLDING_TIME
        if increasing and self.config.require_liquidation_distance:
            if context.liquidation_distance_fraction is None:
                return ReasonCode.LIQUIDATION_DISTANCE_UNKNOWN
            if (
                context.liquidation_distance_fraction
                < self.config.minimum_liquidation_distance_fraction
            ):
                return ReasonCode.LIQUIDATION_DISTANCE_TOO_SMALL
        if increasing and self.config.require_margin_usage:
            if context.margin_usage_fraction is None:
                return ReasonCode.MARGIN_USAGE_UNKNOWN
            if context.margin_usage_fraction > self.config.maximum_margin_usage_fraction:
                return ReasonCode.MARGIN_USAGE_EXCEEDED
        if increasing and context.drawdown_fraction > self.config.max_position_drawdown_fraction:
            return ReasonCode.MAX_POSITION_DRAWDOWN
        if increasing and context.last_adjustment_index is not None:
            if (
                context.index - context.last_adjustment_index
                < self.config.min_bars_between_additions
            ):
                return ReasonCode.MIN_BARS
        return None

    def _triggered(self, context: ScalingContext, rule: ScalingRule) -> bool:
        value = rule.trigger_value
        adverse = (
            context.average_price - context.current_price
            if context.is_long
            else context.current_price - context.average_price
        )
        favorable = -adverse
        if rule.trigger_kind is TriggerKind.ALWAYS:
            return True
        if rule.trigger_kind is TriggerKind.ADVERSE_PERCENT:
            return adverse / context.average_price >= value
        if rule.trigger_kind is TriggerKind.FAVORABLE_PERCENT:
            return favorable / context.average_price >= value
        if rule.trigger_kind is TriggerKind.ADVERSE_ABSOLUTE:
            return adverse >= value
        if rule.trigger_kind is TriggerKind.FAVORABLE_ABSOLUTE:
            return favorable >= value
        if rule.trigger_kind is TriggerKind.R_MULTIPLE:
            return context.current_r_multiple >= value
        if rule.trigger_kind is TriggerKind.BARS:
            return context.bars_held >= int(value)
        if rule.trigger_kind is TriggerKind.ATR_MULTIPLE:
            return context.atr is not None and adverse >= context.atr * value
        if rule.trigger_kind is TriggerKind.PRICE_LADDER:
            if rule.explicit_price is None:
                return False
            if rule.mode is ScalingMode.PYRAMIDING:
                return (
                    context.current_price >= rule.explicit_price
                    if context.is_long
                    else context.current_price <= rule.explicit_price
                )
            return (
                context.current_price <= rule.explicit_price
                if context.is_long
                else context.current_price >= rule.explicit_price
            )
        if rule.trigger_kind is TriggerKind.FEATURE_TRUE:
            if rule.feature_key is None:
                return False
            return context.features.get(rule.feature_key) is True
        return False

    def _desired_quantity(
        self,
        context: ScalingContext,
        rule: ScalingRule,
    ) -> tuple[Decimal, AdjustmentDirection]:
        kind = rule.quantity_kind
        value = rule.quantity_value
        if kind is QuantityKind.FIXED:
            quantity = value
        elif kind in {QuantityKind.INITIAL_FRACTION, QuantityKind.WEIGHT}:
            quantity = context.initial_quantity * value
        elif kind is QuantityKind.CURRENT_FRACTION:
            quantity = context.quantity * value
        elif kind is QuantityKind.EQUITY_FRACTION:
            target = context.equity * value / context.current_price
            quantity = abs(target - context.quantity)
        elif kind is QuantityKind.TARGET_QUANTITY:
            target = value
            quantity = abs(target - context.quantity)
        elif kind is QuantityKind.TARGET_NOTIONAL:
            target = value / context.current_price
            quantity = abs(target - context.quantity)
        else:
            return ZERO, AdjustmentDirection.NONE

        if rule.action is AdjustmentAction.ADD:
            return quantity, AdjustmentDirection.INCREASE
        if rule.action is AdjustmentAction.REDUCE:
            return quantity, AdjustmentDirection.DECREASE
        if rule.action is AdjustmentAction.STOP_SCALING:
            return ZERO, AdjustmentDirection.NONE
        if rule.action is AdjustmentAction.REBALANCE:
            if kind is QuantityKind.EQUITY_FRACTION:
                target = context.equity * value / context.current_price
            elif kind is QuantityKind.TARGET_NOTIONAL:
                target = value / context.current_price
            elif kind is QuantityKind.TARGET_QUANTITY:
                target = value
            else:
                return ZERO, AdjustmentDirection.NONE
            if target > context.quantity:
                return target - context.quantity, AdjustmentDirection.INCREASE
            if target < context.quantity:
                return context.quantity - target, AdjustmentDirection.DECREASE
        return ZERO, AdjustmentDirection.NONE

    @staticmethod
    def _risk_amount(
        is_long: bool,
        average_price: Decimal,
        stop_price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        unit_risk = (
            max(ZERO, average_price - stop_price)
            if is_long
            else max(ZERO, stop_price - average_price)
        )
        return unit_risk * quantity

    def _projected_safety(
        self,
        context: ScalingContext,
        *,
        direction: AdjustmentDirection,
        quantity: Decimal,
        expected_quantity: Decimal,
        expected_average: Decimal,
        desired_price: Decimal,
        current_risk: Decimal,
        projected_risk: Decimal,
        maximum_risk: Decimal,
        additional_risk: Decimal,
        expected_notional: Decimal,
    ) -> ReasonCode | None:
        if direction is not AdjustmentDirection.INCREASE:
            return None
        if context.is_long and context.stop_price >= desired_price:
            return ReasonCode.INVALID_GEOMETRY
        if not context.is_long and context.stop_price <= desired_price:
            return ReasonCode.INVALID_GEOMETRY
        config = self.config
        if config.max_total_quantity is not None and expected_quantity > config.max_total_quantity:
            return ReasonCode.MAX_TOTAL_QUANTITY
        if config.max_total_notional is not None and expected_notional > config.max_total_notional:
            return ReasonCode.MAX_TOTAL_NOTIONAL
        if context.equity <= ZERO or expected_notional / context.equity > config.max_total_leverage:
            return ReasonCode.MAX_TOTAL_LEVERAGE
        if projected_risk > maximum_risk:
            return ReasonCode.MAX_TOTAL_POSITION_RISK
        if additional_risk > context.equity * config.max_additional_risk_fraction:
            return ReasonCode.MAX_ADDITIONAL_RISK
        deterioration = (
            max(ZERO, expected_average - context.average_price) / context.average_price
            if context.is_long
            else max(ZERO, context.average_price - expected_average) / context.average_price
        )
        if deterioration > config.maximum_average_price_deterioration_fraction:
            return ReasonCode.AVERAGE_PRICE_DETERIORATION
        if context.scale_in_count > 0 or context.last_adjustment_index is not None:
            distance_anchor = (
                context.last_adjustment_price
                if context.last_adjustment_price is not None
                else context.average_price
            )
            min_distance = distance_anchor * config.min_distance_between_additions_fraction
            if abs(desired_price - distance_anchor) < min_distance:
                return ReasonCode.MIN_DISTANCE
        if quantity <= ZERO or current_risk < ZERO:
            return ReasonCode.INVALID_QUANTITY
        return None
