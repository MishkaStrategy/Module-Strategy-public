from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from modular_core import Feature, MarketView, ModuleDecision, ModuleManifest, StrategyModule

from .canonical import deep_thaw, fingerprint, stable_id
from .config import PositionScalingConfig
from .decimal_utils import (
    ZERO,
    canonical_decimal,
    decimal_from,
    nonnegative_decimal,
    weighted_average,
)
from .engine import ScalingContext, ScalingPlanner
from .enums import AdjustmentAction, AdjustmentDirection, ReasonCode, ScalingLifecycle
from .models import ApprovedAdjustment, PositionAdjustmentPlan, RejectedAdjustment
from .state import STATE_SCHEMA_VERSION, PlannerState

FEATURE_PLAN = "position_scaling.plan"
FEATURE_RISK_REQUEST = "risk.request"
FEATURE_RISK_DECISION = "risk.decision"
FEATURE_APPROVED = "position_scaling.approved"
FEATURE_REJECTED = "position_scaling.rejected"
FEATURE_STATUS = "position_scaling.status"
FEATURE_SCHEMA_VERSION = "1"
PARAMETER_SCHEMA: Mapping[str, Mapping[str, Any]] = {
    "enabled": {"type": "boolean", "default": False},
    "rules": {"type": "array", "default": ()},
    "allow_averaging_down": {"type": "boolean", "default": False},
    "allow_pyramiding": {"type": "boolean", "default": False},
    "allow_fixed_dca": {"type": "boolean", "default": False},
    "allow_weighted_dca": {"type": "boolean", "default": False},
    "allow_scale_out": {"type": "boolean", "default": True},
    "allow_rebalance": {"type": "boolean", "default": False},
    "allow_martingale": {"type": "boolean", "default": False},
    "max_scale_in_count": {"type": "integer", "minimum": 0, "default": 2},
    "max_scale_out_count": {"type": "integer", "minimum": 0, "default": 4},
    "max_total_quantity": {"default": None},
    "max_total_notional": {"default": None},
    "max_total_leverage": {"type": "number", "minimum": 0.000001, "default": 3.0},
    "max_total_position_risk_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.005,
    },
    "max_additional_risk_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.0025,
    },
    "max_position_drawdown_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.02,
    },
    "min_distance_between_additions_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.001,
    },
    "min_bars_between_additions": {"type": "integer", "minimum": 0, "default": 1},
    "max_holding_bars_for_new_additions": {
        "type": "integer",
        "minimum": 1,
        "default": 500,
    },
    "minimum_liquidation_distance_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.05,
    },
    "maximum_margin_usage_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.5,
    },
    "maximum_average_price_deterioration_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.05,
    },
    "minimum_residual_quantity": {"type": "number", "minimum": 0, "default": 0.0},
    "require_liquidation_distance": {"type": "boolean", "default": True},
    "require_margin_usage": {"type": "boolean", "default": True},
    "require_setup_validity": {"type": "boolean", "default": True},
    "require_market_regime": {"type": "boolean", "default": False},
    "allowed_market_regimes": {"type": "array", "default": ()},
    "setup_validity_feature": {"type": "string", "default": "setup.validity"},
    "market_regime_feature": {"type": "string", "default": "market_regime.state"},
    "atr_feature": {"type": "string", "default": "market.atr"},
    "instrument_rules_feature": {"type": "string", "default": "instrument.rules"},
    "portfolio_risk_feature": {"type": "string", "default": "portfolio.risk_snapshot"},
    "scaling_context_feature": {"type": "string", "default": "position_scaling.context"},
    "instrument_rules": {"default": None},
    "max_history": {"type": "integer", "minimum": 1, "default": 128},
    "max_processed_events": {"type": "integer", "minimum": 1, "default": 1024},
    "emit_status_feature": {"type": "boolean", "default": True},
    "suspend_additions_on_gap": {"type": "boolean", "default": True},
    "block_additions_when_exit_pending": {"type": "boolean", "default": True},
}


def _manifest_parameters(
    raw: Mapping[str, Any], config: PositionScalingConfig
) -> Mapping[str, object]:
    canonical = config.canonical_mapping()
    result: dict[str, object] = {}
    for key in raw:
        value = canonical[key]
        if isinstance(value, Decimal):
            result[key] = float(value)
        elif value is None:
            result[key] = None
        else:
            result[key] = deep_thaw(value)
    return MappingProxyType(result)


def _planner_manifest(config: PositionScalingConfig) -> ModuleManifest:
    optional = {
        config.setup_validity_feature,
        config.market_regime_feature,
        config.atr_feature,
        config.instrument_rules_feature,
        config.portfolio_risk_feature,
        config.scaling_context_feature,
        FEATURE_APPROVED,
        FEATURE_REJECTED,
    }
    optional.update(rule.feature_key for rule in config.rules if rule.feature_key is not None)
    return ModuleManifest(
        name=PositionScalingPlannerModule.name,
        version="0.2.2",
        api_version="1.0",
        optional_features=tuple(sorted(optional)),
        produced_features=(FEATURE_PLAN, FEATURE_RISK_REQUEST, FEATURE_STATUS),
        parameters_schema=PARAMETER_SCHEMA,
        description=(
            "Deterministic planning of position scale-in, scale-out and rebalance actions."
        ),
        state_schema_version=STATE_SCHEMA_VERSION,
        skip_until_ready=False,
    )


class PositionScalingPlannerModule(StrategyModule):
    name = "position_scaling_planner"
    manifest = ModuleManifest(
        name=name,
        version="0.2.2",
        api_version="1.0",
        optional_features=(
            "setup.validity",
            "market_regime.state",
            "market.atr",
            "instrument.rules",
            "portfolio.risk_snapshot",
            "position_scaling.context",
            FEATURE_APPROVED,
            FEATURE_REJECTED,
        ),
        produced_features=(FEATURE_PLAN, FEATURE_RISK_REQUEST, FEATURE_STATUS),
        parameters_schema=PARAMETER_SCHEMA,
        description="Deterministic planning of position scale-in, scale-out and rebalance actions.",
        state_schema_version=STATE_SCHEMA_VERSION,
        skip_until_ready=False,
    )

    def __init__(self, **parameters: Any) -> None:
        self.config = PositionScalingConfig.from_mapping(parameters)
        self.parameters = _manifest_parameters(parameters, self.config)
        self.manifest = _planner_manifest(self.config)
        self._planner = ScalingPlanner(self.config)
        self._state = PlannerState(config_fingerprint=self.config.fingerprint)
        self._state.counters = self._empty_counters()
        self._restore_count = 0

    @staticmethod
    def _empty_counters() -> dict[str, int]:
        return {
            "bars": 0,
            "attached": 0,
            "plans": 0,
            "risk_requests": 0,
            "reconciled": 0,
            "duplicates": 0,
            "conflicts": 0,
            "gaps": 0,
            "blocked": 0,
            "expired": 0,
            "external_changes": 0,
            "feedback_approved": 0,
            "feedback_rejected": 0,
            "feedback_invalid": 0,
            "unapproved_changes": 0,
            "overfills": 0,
            "partial_fills": 0,
        }

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self._state.counters["bars"] += 1
        current_index = view.index
        if current_index < self._state.last_seen_index:
            self._state.counters["blocked"] += 1
            return self._skip(ReasonCode.OUT_OF_ORDER)
        self._state.last_seen_index = current_index
        position = view.position
        if position is None or decimal_from(position.quantity, "position.quantity") <= ZERO:
            if self._state.position_id is not None:
                self._state.reset_position()
            return self._status_or_skip(view, ReasonCode.NO_OPEN_POSITION)
        feedback = self._ingest_feedback(view)
        if feedback is not None:
            return feedback
        attach = self._attach_or_reconcile(view)
        if attach is not None:
            return attach
        if self._state.lifecycle is ScalingLifecycle.RECOVERY_REQUIRED:
            return self._status_or_skip(view, ReasonCode.POSITION_SNAPSHOT_MISMATCH)
        if self._state.pending_plan is not None:
            direction = str(self._state.pending_plan.get("direction", ""))
            gap_invalid = (
                direction == AdjustmentDirection.INCREASE.value
                and view.data_gap
                and self.config.suspend_additions_on_gap
            )
            exit_invalid = (
                direction == AdjustmentDirection.INCREASE.value
                and view.exit_pending
                and self.config.block_additions_when_exit_pending
            )
            if gap_invalid or exit_invalid:
                reason = ReasonCode.DATA_GAP if gap_invalid else ReasonCode.EXIT_PENDING
                self._state.lifecycle = ScalingLifecycle.INVALIDATED
                self._state.add_history(
                    {
                        "event": "invalidated",
                        "reason": reason.value,
                        "adjustment_id": self._state.pending_plan.get("adjustment_id"),
                    },
                    self.config.max_history,
                )
                self._clear_pending()
                return self._status_or_skip(view, reason)
        if not self.config.enabled:
            return self._status_or_skip(view, ReasonCode.SCALING_DISABLED)
        if self._state.lifecycle is ScalingLifecycle.STOPPED:
            return self._status_or_skip(view, ReasonCode.STOP_SCALING_REQUESTED)
        if self._state.pending_plan is not None:
            expires_index = int(self._state.pending_plan.get("expires_index", -1))
            if view.index > expires_index:
                self._state.counters["expired"] += 1
                self._state.lifecycle = ScalingLifecycle.EXPIRED
                self._state.add_history(
                    {
                        "event": "expired",
                        "adjustment_id": self._state.pending_plan.get("adjustment_id"),
                    },
                    self.config.max_history,
                )
                self._clear_pending()
            else:
                return self._status_or_skip(view, ReasonCode.PENDING_ADJUSTMENT)
        if view.index < self._state.cooldown_until_index:
            return self._status_or_skip(view, ReasonCode.COOLDOWN)

        context = self._context(view)
        for rule in self.config.rules:
            if rule.stage in self._state.completed_stages:
                continue
            result = self._planner.evaluate_rule(context, rule)
            if result.plan is None:
                if result.reason not in {ReasonCode.NO_TRIGGER, ReasonCode.RULE_COMPLETED}:
                    self._state.counters["blocked"] += 1
                    return self._status_or_skip(view, result.reason)
                continue
            plan = result.plan
            plan_mapping = plan.to_mapping()
            plan_fingerprint = fingerprint(plan_mapping)
            event_id = stable_id("plan-event", {"plan": plan_mapping, "bar": view.index})
            if event_id in self._state.processed_event_set:
                self._state.counters["duplicates"] += 1
                return self._status_or_skip(view, ReasonCode.DUPLICATE)
            if (
                self._state.pending_fingerprint is not None
                and self._state.pending_fingerprint != plan_fingerprint
            ):
                self._state.counters["conflicts"] += 1
                self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
                return self._status_or_skip(view, ReasonCode.CONFLICT)
            request: Mapping[str, Any] | None = None
            if plan.direction is AdjustmentDirection.INCREASE:
                instrument = self._instrument_rules(view)
                if instrument is None:
                    self._state.lifecycle = ScalingLifecycle.REJECTED
                    return self._status_or_skip(view, ReasonCode.INVALID_RULE)
                portfolio = self._mapping_feature(view, self.config.portfolio_risk_feature)
                request = self._planner.build_risk_request(context, plan, instrument, portfolio)
            self._state.remember_event(event_id, self.config.max_processed_events)
            self._state.counters["plans"] += 1
            features = [self._feature(view, FEATURE_PLAN, plan_mapping, plan.adjustment_id)]
            if plan.action is AdjustmentAction.STOP_SCALING:
                self._clear_pending()
                self._state.completed_stages.add(plan.stage)
                self._state.lifecycle = ScalingLifecycle.STOPPED
                self._state.add_history(
                    {
                        "event": "stopped",
                        "adjustment_id": plan.adjustment_id,
                        "stage": plan.stage,
                    },
                    self.config.max_history,
                )
                features.extend(self._status_features(view, plan.reason_code))
                return ModuleDecision(
                    features=tuple(features),
                    diagnostics=self._diagnostics(
                        reason=plan.reason_code, adjustment_id=plan.adjustment_id
                    ),
                )
            self._state.pending_plan = plan_mapping
            self._state.pending_fingerprint = plan_fingerprint
            self._state.pending_approved_quantity = None
            self._state.pending_feedback_fingerprint = None
            self._state.lifecycle = (
                ScalingLifecycle.RISK_PENDING
                if plan.direction is AdjustmentDirection.INCREASE
                else ScalingLifecycle.PLAN_CREATED
            )
            if request is not None:
                features.append(
                    self._feature(
                        view,
                        FEATURE_RISK_REQUEST,
                        request,
                        plan.risk_request_id or plan.adjustment_id,
                    )
                )
                self._state.counters["risk_requests"] += 1
            features.extend(self._status_features(view, plan.reason_code))
            return ModuleDecision(
                features=tuple(features),
                diagnostics=self._diagnostics(
                    reason=plan.reason_code, adjustment_id=plan.adjustment_id
                ),
            )
        return self._status_or_skip(view, ReasonCode.NO_TRIGGER)

    def snapshot_state(self) -> Mapping[str, object]:
        return self._state.snapshot()

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not state:
            self._state = PlannerState(config_fingerprint=self.config.fingerprint)
            self._state.counters = self._empty_counters()
            self._restore_count += 1
            return
        restored = PlannerState.restore(
            state,
            config_fingerprint=self.config.fingerprint,
            max_history=self.config.max_history,
            max_processed_events=self.config.max_processed_events,
        )
        for key in self._empty_counters():
            restored.counters.setdefault(key, 0)
        self._state = restored
        self._restore_count += 1

    def _clear_pending(self) -> None:
        self._state.pending_plan = None
        self._state.pending_fingerprint = None
        self._state.pending_approved_quantity = None
        self._state.pending_feedback_fingerprint = None

    def _ingest_feedback(self, view: MarketView) -> ModuleDecision | None:
        if self._state.pending_plan is None or view.features is None:
            return None
        plan = PositionAdjustmentPlan.from_mapping(self._state.pending_plan)
        matching: list[tuple[str, Feature, str]] = []
        for key in (FEATURE_APPROVED, FEATURE_REJECTED):
            for feature in view.features.get_all(key, symbol=view.current.symbol):
                if not isinstance(feature.value, Mapping):
                    continue
                if str(feature.value.get("adjustment_id", "")) != plan.adjustment_id:
                    continue
                try:
                    value_fingerprint = fingerprint(feature.value)
                except (TypeError, ValueError):
                    self._state.counters["feedback_invalid"] += 1
                    self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
                    return self._status_or_skip(view, ReasonCode.APPROVAL_FEEDBACK_INVALID)
                matching.append((key, feature, value_fingerprint))
        if not matching:
            return None
        unique_fingerprints = {item[2] for item in matching}
        if len(unique_fingerprints) > 1:
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            self._state.counters["conflicts"] += 1
            return self._status_or_skip(view, ReasonCode.CONFLICT)
        key, feature, feedback_fingerprint = matching[-1]
        previous = self._state.pending_feedback_fingerprint
        if previous is not None:
            if previous != feedback_fingerprint:
                self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
                self._state.counters["conflicts"] += 1
                return self._status_or_skip(view, ReasonCode.CONFLICT)
            return None
        value = feature.value
        if feature.schema_version != FEATURE_SCHEMA_VERSION:
            self._state.counters["feedback_invalid"] += 1
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.APPROVAL_FEEDBACK_INVALID)
        try:
            if key == FEATURE_REJECTED:
                rejected = RejectedAdjustment.from_mapping(value)
                if (
                    rejected.adjustment_id != plan.adjustment_id
                    or rejected.position_id != plan.position_id
                    or rejected.symbol != plan.symbol
                    or rejected.risk_request_id != plan.risk_request_id
                ):
                    raise ValueError("rejected adjustment disagrees with pending plan")
                reason = rejected.reason_code
                self._state.counters["feedback_rejected"] += 1
                self._state.lifecycle = ScalingLifecycle.REJECTED
                self._state.add_history(
                    {
                        "event": "rejected",
                        "adjustment_id": plan.adjustment_id,
                        "reason": reason.value,
                    },
                    self.config.max_history,
                )
                cooldown = int(plan.metadata.get("cooldown_bars", 0))
                self._state.cooldown_until_index = view.index + cooldown
                self._clear_pending()
                return self._status_or_skip(view, reason)
            approved = ApprovedAdjustment.from_mapping(value)
            if approved.adjustment_id != plan.adjustment_id or approved.plan != plan.to_mapping():
                raise ValueError("approved adjustment disagrees with pending plan")
            approved_quantity = approved.approved_quantity
        except (KeyError, TypeError, ValueError):
            self._state.counters["feedback_invalid"] += 1
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.APPROVAL_FEEDBACK_INVALID)
        self._state.counters["feedback_approved"] += 1
        self._state.pending_approved_quantity = approved_quantity
        self._state.pending_feedback_fingerprint = feedback_fingerprint
        self._state.lifecycle = ScalingLifecycle.APPROVED
        self._state.add_history(
            {
                "event": "approved",
                "adjustment_id": plan.adjustment_id,
                "approved_quantity": str(approved_quantity),
            },
            self.config.max_history,
        )
        return None

    def _attach_or_reconcile(self, view: MarketView) -> ModuleDecision | None:
        position = view.position
        if position is None:
            return self._status_or_skip(view, ReasonCode.NO_OPEN_POSITION)
        quantity = decimal_from(position.quantity, "position.quantity")
        entry = decimal_from(position.entry_price, "position.entry_price")
        stop = (
            None
            if position.stop_loss is None
            else decimal_from(position.stop_loss, "position.stop_loss")
        )
        if stop is None or stop <= ZERO:
            if self._state.position_id is not None:
                self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.STOP_INVALID)
        if self._state.position_id is None:
            self._state.position_id = position.id
            self._state.setup_id = position.setup_id
            self._state.symbol = view.current.symbol
            self._state.side = position.side.value
            self._state.initial_quantity = quantity
            self._state.initial_entry_price = entry
            self._state.initial_stop_price = stop
            self._state.last_quantity = quantity
            self._state.last_entry_price = entry
            self._state.attached_index = view.index
            self._state.lifecycle = ScalingLifecycle.ELIGIBLE
            self._state.counters["attached"] += 1
            return None
        if (
            position.id != self._state.position_id
            or position.setup_id != self._state.setup_id
            or position.side.value != self._state.side
            or view.current.symbol != self._state.symbol
        ):
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.POSITION_CHANGED)
        is_long = position.side.value == "long"
        weakened = (
            stop < self._state.initial_stop_price
            if is_long
            else stop > self._state.initial_stop_price
        )
        if weakened:
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.STOP_WEAKENING_FORBIDDEN)

        quantity_changed = quantity != self._state.last_quantity
        entry_changed = entry != self._state.last_entry_price
        if self._state.pending_plan is None:
            if not quantity_changed and not entry_changed:
                return None
            self._state.counters["external_changes"] += 1
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            self._state.add_history(
                {
                    "event": "external_position_change",
                    "quantity": str(quantity),
                    "entry_price": str(entry),
                },
                self.config.max_history,
            )
            self._state.last_quantity = quantity
            self._state.last_entry_price = entry
            return self._status_or_skip(view, ReasonCode.EXTERNAL_POSITION_CHANGE)

        plan = PositionAdjustmentPlan.from_mapping(self._state.pending_plan)
        if quantity_changed or entry_changed:
            observed_direction = (
                AdjustmentDirection.INCREASE
                if quantity > plan.current_quantity
                else AdjustmentDirection.DECREASE
                if quantity < plan.current_quantity
                else AdjustmentDirection.NONE
            )
            if observed_direction is not plan.direction:
                self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
                return self._status_or_skip(view, ReasonCode.POSITION_SNAPSHOT_MISMATCH)
        if (quantity_changed or entry_changed) and self._state.pending_approved_quantity is None:
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            self._state.counters["external_changes"] += 1
            self._state.counters["unapproved_changes"] += 1
            self._state.add_history(
                {
                    "event": "unapproved_position_change",
                    "adjustment_id": plan.adjustment_id,
                    "quantity": str(quantity),
                    "entry_price": str(entry),
                },
                self.config.max_history,
            )
            self._state.last_quantity = quantity
            self._state.last_entry_price = entry
            return self._status_or_skip(view, ReasonCode.ADJUSTMENT_NOT_APPROVED)
        approved_quantity = self._state.pending_approved_quantity or plan.desired_quantity
        expected_total = (
            plan.current_quantity + approved_quantity
            if plan.direction is AdjustmentDirection.INCREASE
            else plan.current_quantity - approved_quantity
            if plan.direction is AdjustmentDirection.DECREASE
            else plan.current_quantity
        )
        if plan.direction is AdjustmentDirection.INCREASE:
            invalid_progress = quantity < self._state.last_quantity
            overfilled = quantity > expected_total
        elif plan.direction is AdjustmentDirection.DECREASE:
            invalid_progress = quantity > self._state.last_quantity
            overfilled = quantity < expected_total
        else:
            invalid_progress = quantity != plan.current_quantity
            overfilled = False
        if invalid_progress:
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.POSITION_SNAPSHOT_MISMATCH)
        if overfilled:
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            self._state.counters["external_changes"] += 1
            self._state.counters["overfills"] += 1
            self._state.add_history(
                {
                    "event": "overfill",
                    "adjustment_id": plan.adjustment_id,
                    "expected_total_quantity": str(expected_total),
                    "actual_quantity": str(quantity),
                },
                self.config.max_history,
            )
            self._state.last_quantity = quantity
            self._state.last_entry_price = entry
            return self._status_or_skip(view, ReasonCode.ADJUSTMENT_OVERFILL)
        if not quantity_changed and not entry_changed and quantity != expected_total:
            return None
        actual_direction = (
            AdjustmentDirection.INCREASE
            if quantity > plan.current_quantity
            else AdjustmentDirection.DECREASE
            if quantity < plan.current_quantity
            else AdjustmentDirection.NONE
        )
        if actual_direction is not plan.direction:
            self._state.lifecycle = ScalingLifecycle.RECOVERY_REQUIRED
            return self._status_or_skip(view, ReasonCode.POSITION_SNAPSHOT_MISMATCH)
        complete = quantity == expected_total
        self._state.last_quantity = quantity
        self._state.last_entry_price = entry
        self._state.last_adjustment_index = view.index
        self._state.last_adjustment_price = plan.desired_price
        if not complete:
            self._state.counters["partial_fills"] += 1
            self._state.lifecycle = ScalingLifecycle.PARTIALLY_FILLED
            self._state.add_history(
                {
                    "event": "partial_fill",
                    "adjustment_id": plan.adjustment_id,
                    "approved_quantity": str(approved_quantity),
                    "expected_total_quantity": str(expected_total),
                    "quantity": str(quantity),
                    "entry_price": str(entry),
                },
                self.config.max_history,
            )
            return self._status_or_skip(view, ReasonCode.PARTIAL_FILL)

        self._state.completed_stages.add(plan.stage)
        if plan.direction is AdjustmentDirection.INCREASE:
            self._state.scale_in_count += 1
        else:
            self._state.scale_out_count += 1
        cooldown = int(plan.metadata.get("cooldown_bars", 0))
        self._state.cooldown_until_index = view.index + cooldown
        self._state.counters["reconciled"] += 1
        self._state.lifecycle = ScalingLifecycle.RECONCILED
        self._state.add_history(
            {
                "event": "reconciled",
                "adjustment_id": plan.adjustment_id,
                "approved_quantity": str(approved_quantity),
                "quantity": str(quantity),
                "entry_price": str(entry),
            },
            self.config.max_history,
        )
        self._clear_pending()
        return None

    def _context(self, view: MarketView) -> ScalingContext:
        position = view.position
        if position is None:
            raise ValueError("position scaling context requires an open position")
        features: dict[str, Any] = {}
        source_ids: list[str] = []
        keys = {
            self.config.setup_validity_feature,
            self.config.market_regime_feature,
            self.config.atr_feature,
            self.config.scaling_context_feature,
            self.config.instrument_rules_feature,
            self.config.portfolio_risk_feature,
        }
        keys.update(rule.feature_key for rule in self.config.rules if rule.feature_key)
        if view.features is not None:
            for key in sorted(keys):
                feature = view.features.latest(key, symbol=view.current.symbol)
                if feature is not None:
                    features[key] = feature.value
                    if feature.feature_id:
                        source_ids.append(feature.feature_id)
        setup_valid = _extract_bool(features.get(self.config.setup_validity_feature))
        regime = _extract_regime(features.get(self.config.market_regime_feature))
        atr = _extract_decimal(features.get(self.config.atr_feature))
        extra = features.get(self.config.scaling_context_feature)
        extra_mapping = extra if isinstance(extra, Mapping) else {}
        attached_index = (
            view.index if self._state.attached_index is None else self._state.attached_index
        )
        bar_seconds = max(
            1,
            int((view.current.close_time - view.current.timestamp).total_seconds()),
        )
        return ScalingContext(
            position_id=position.id,
            setup_id=position.setup_id,
            symbol=view.current.symbol,
            side=position.side.value,
            quantity=decimal_from(position.quantity, "position.quantity"),
            average_price=decimal_from(position.entry_price, "position.entry_price"),
            initial_quantity=self._state.initial_quantity,
            initial_entry_price=self._state.initial_entry_price,
            initial_stop_price=self._state.initial_stop_price,
            stop_price=decimal_from(position.stop_loss, "position.stop_loss"),
            current_price=decimal_from(view.current.close, "current.close"),
            unrealized_pnl=decimal_from(position.unrealized_pnl, "position.unrealized_pnl"),
            equity=decimal_from(view.equity, "view.equity"),
            cash=decimal_from(view.cash, "view.cash"),
            index=view.index,
            attached_index=attached_index,
            decision_time=view.current.close_time,
            bar_seconds=bar_seconds,
            last_adjustment_index=self._state.last_adjustment_index,
            last_adjustment_price=self._state.last_adjustment_price,
            scale_in_count=self._state.scale_in_count,
            scale_out_count=self._state.scale_out_count,
            data_gap=view.data_gap,
            exit_pending=view.exit_pending,
            pending_order_count=len(view.pending_orders),
            setup_valid=setup_valid,
            market_regime=regime,
            atr=atr,
            liquidation_distance_fraction=_extract_decimal(
                extra_mapping.get("liquidation_distance_fraction")
            ),
            margin_usage_fraction=_extract_decimal(extra_mapping.get("margin_usage_fraction")),
            available_margin=_extract_decimal(extra_mapping.get("available_margin")),
            source_feature_ids=tuple(source_ids),
            features=features,
        )

    def _instrument_rules(self, view: MarketView) -> Mapping[str, Any] | None:
        feature = self._mapping_feature(view, self.config.instrument_rules_feature)
        if feature is not None:
            return feature
        return self.config.instrument_rules

    def _mapping_feature(self, view: MarketView, key: str) -> Mapping[str, Any] | None:
        if view.features is None:
            return None
        feature = view.features.latest(key, symbol=view.current.symbol)
        return feature.value if feature is not None and isinstance(feature.value, Mapping) else None

    def _feature(
        self,
        view: MarketView,
        key: str,
        value: Mapping[str, Any],
        semantic_id: str,
    ) -> Feature:
        return Feature(
            key=key,
            source_module=self.name,
            created_at=view.current.close_time,
            value=value,
            symbol=view.current.symbol,
            timeframe=view.current.timeframe,
            valid_until=view.current.close_time,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=stable_id("feature", {"key": key, "id": semantic_id}),
            invalidate_on_gap=key not in {FEATURE_STATUS},
            metadata={"semantic_id": semantic_id},
        )

    def _status_features(self, view: MarketView, reason: ReasonCode) -> list[Feature]:
        if not self.config.emit_status_feature:
            return []
        return [
            self._feature(
                view,
                FEATURE_STATUS,
                self._status_mapping(reason),
                stable_id(
                    "status",
                    {"index": view.index, "position": self._state.position_id, "reason": reason},
                ),
            )
        ]

    def _status_or_skip(self, view: MarketView, reason: ReasonCode) -> ModuleDecision:
        if reason is ReasonCode.DATA_GAP:
            self._state.counters["gaps"] += 1
        return ModuleDecision(
            features=tuple(self._status_features(view, reason)),
            skips=(reason.value,),
            diagnostics=self._diagnostics(reason=reason),
        )

    def _skip(self, reason: ReasonCode) -> ModuleDecision:
        return ModuleDecision(skips=(reason.value,), diagnostics=self._diagnostics(reason=reason))

    def _status_mapping(self, reason: ReasonCode) -> Mapping[str, Any]:
        return {
            "schema_version": "1",
            "position_id": self._state.position_id,
            "lifecycle": self._state.lifecycle.value,
            "reason_code": reason.value,
            "scale_in_count": self._state.scale_in_count,
            "scale_out_count": self._state.scale_out_count,
            "completed_stages": sorted(self._state.completed_stages),
            "pending_adjustment_id": None
            if self._state.pending_plan is None
            else self._state.pending_plan.get("adjustment_id"),
            "restore_count": self._restore_count,
            "pending_approved_quantity": (
                None
                if self._state.pending_approved_quantity is None
                else canonical_decimal(self._state.pending_approved_quantity)
            ),
            "feedback_received": self._state.pending_feedback_fingerprint is not None,
            "cooldown_until_index": self._state.cooldown_until_index,
            "last_seen_index": self._state.last_seen_index,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": self.config.fingerprint,
            "counters": dict(self._state.counters),
        }

    def _diagnostics(
        self, *, reason: ReasonCode, adjustment_id: str | None = None
    ) -> Mapping[str, object]:
        values: dict[str, object] = {
            "reason_code": reason.value,
            "lifecycle": self._state.lifecycle.value,
            "position_id": self._state.position_id,
            "scale_in_count": self._state.scale_in_count,
            "scale_out_count": self._state.scale_out_count,
            "history_size": len(self._state.history),
            "processed_event_count": len(self._state.processed_event_ids),
            "restore_count": self._restore_count,
            "pending_approved_quantity": (
                None
                if self._state.pending_approved_quantity is None
                else canonical_decimal(self._state.pending_approved_quantity)
            ),
            "feedback_received": self._state.pending_feedback_fingerprint is not None,
            "cooldown_until_index": self._state.cooldown_until_index,
            "last_seen_index": self._state.last_seen_index,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": self.config.fingerprint,
            "counters": MappingProxyType(dict(self._state.counters)),
        }
        if adjustment_id is not None:
            values["adjustment_id"] = adjustment_id
        return MappingProxyType(values)


def _validate_typed_risk_adjustment(
    decision: Mapping[str, Any],
    plan: PositionAdjustmentPlan,
    risk_quantity: Decimal,
) -> None:
    payload = decision.get("position_adjustment")
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValueError("position_adjustment decision must be a mapping")
    allowed = {
        "schema_version",
        "adjustment_id",
        "position_id",
        "requested_quantity",
        "approved_quantity",
        "projected_total_quantity",
        "projected_average_price",
        "projected_notional",
        "projected_leverage",
        "projected_total_risk",
        "opens_new_position",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unknown position_adjustment decision fields: " + ", ".join(unknown))
    if str(payload.get("schema_version", "")) != "1":
        raise ValueError("unsupported position_adjustment decision schema")
    if str(payload.get("adjustment_id", "")) != plan.adjustment_id:
        raise ValueError("risk adjustment_id disagrees with pending plan")
    if str(payload.get("position_id", "")) != plan.position_id:
        raise ValueError("risk position_id disagrees with pending plan")
    if payload.get("opens_new_position") is not False:
        raise ValueError("risk adjustment must target an existing position")
    requested_quantity = decimal_from(
        payload.get("requested_quantity"),
        "risk adjustment requested_quantity",
    )
    approved_quantity = nonnegative_decimal(
        payload.get("approved_quantity"),
        "risk adjustment approved_quantity",
    )
    projected_total_quantity = decimal_from(
        payload.get("projected_total_quantity"),
        "risk adjustment projected_total_quantity",
    )
    projected_average_price = decimal_from(
        payload.get("projected_average_price"),
        "risk adjustment projected_average_price",
    )
    if requested_quantity != plan.desired_quantity:
        raise ValueError("risk requested quantity disagrees with pending plan")
    if approved_quantity != risk_quantity:
        raise ValueError("risk approved quantity disagrees with decision quantity")
    if approved_quantity > min(plan.desired_quantity, plan.maximum_quantity):
        raise ValueError("risk approved quantity exceeds pending plan")
    expected_total = plan.current_quantity + approved_quantity
    if projected_total_quantity != expected_total:
        raise ValueError("risk projected total quantity is inconsistent")
    expected_average = weighted_average(
        plan.current_average_price,
        plan.current_quantity,
        plan.desired_price,
        approved_quantity,
    )
    if projected_average_price != expected_average:
        raise ValueError("risk projected average price is inconsistent")
    for name in (
        "projected_notional",
        "projected_leverage",
        "projected_total_risk",
    ):
        nonnegative_decimal(payload.get(name), f"risk adjustment {name}")


class PositionScalingApprovalModule(StrategyModule):
    name = "position_scaling_approval"
    manifest = ModuleManifest(
        name=name,
        version="0.2.2",
        api_version="1.0",
        required_features=(FEATURE_PLAN, FEATURE_RISK_DECISION),
        produced_features=(FEATURE_APPROVED, FEATURE_REJECTED),
        parameters_schema={
            "max_processed_adjustments": {"type": "integer", "minimum": 1, "default": 1024}
        },
        description="Matches scaling plans with risk decisions and publishes execution-ready approvals.",
        state_schema_version=STATE_SCHEMA_VERSION,
        skip_until_ready=False,
    )

    def __init__(self, max_processed_adjustments: int = 1024) -> None:
        if (
            isinstance(max_processed_adjustments, bool)
            or not isinstance(max_processed_adjustments, int)
            or max_processed_adjustments < 1
        ):
            raise ValueError("max_processed_adjustments must be an integer >= 1")
        self.max_processed_adjustments = max_processed_adjustments
        self.parameters = MappingProxyType({"max_processed_adjustments": max_processed_adjustments})
        self._fingerprints: dict[str, str] = {}
        self._order: deque[str] = deque()
        self._last_index = -1
        self._counters = {
            "received": 0,
            "approved": 0,
            "rejected": 0,
            "duplicates": 0,
            "conflicts": 0,
        }

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index < self._last_index:
            return ModuleDecision(skips=(ReasonCode.OUT_OF_ORDER.value,))
        self._last_index = view.index
        if view.features is None:
            return ModuleDecision(skips=(ReasonCode.NO_TRIGGER.value,))
        feature = view.features.latest(FEATURE_PLAN, symbol=view.current.symbol)
        if feature is None:
            return ModuleDecision(skips=(ReasonCode.NO_TRIGGER.value,))
        if feature.schema_version != FEATURE_SCHEMA_VERSION:
            return ModuleDecision(skips=(ReasonCode.INVALID_RULE.value,))
        if feature.created_at != view.current.close_time:
            return ModuleDecision(skips=(ReasonCode.EXPIRED_PLAN.value,))
        if not isinstance(feature.value, Mapping):
            return ModuleDecision(skips=(ReasonCode.INVALID_RULE.value,))
        try:
            plan = PositionAdjustmentPlan.from_mapping(feature.value)
        except (KeyError, TypeError, ValueError):
            return ModuleDecision(skips=(ReasonCode.INVALID_RULE.value,))
        if plan.action in {AdjustmentAction.HOLD, AdjustmentAction.REJECTED}:
            return self._terminal_reject(
                view, plan, ReasonCode.INVALID_RULE, "plan action is not executable"
            )
        if plan.symbol != view.current.symbol:
            return self._reject(
                view, plan, ReasonCode.POSITION_SNAPSHOT_MISMATCH, "plan symbol mismatch"
            )
        if plan.expires_index < view.index or plan.expires_at < view.current.close_time:
            return self._terminal_reject(view, plan, ReasonCode.EXPIRED_PLAN, "plan expired")
        payload_fingerprint = fingerprint(plan.to_mapping())
        previous = self._fingerprints.get(plan.adjustment_id)
        if previous == payload_fingerprint:
            self._counters["duplicates"] += 1
            return ModuleDecision(
                skips=(ReasonCode.DUPLICATE.value,), diagnostics=self._diagnostics()
            )
        if previous is not None:
            self._counters["conflicts"] += 1
            return self._reject(
                view,
                plan,
                ReasonCode.CONFLICT,
                "adjustment_id reused with different payload",
            )
        position = view.position
        if position is None or decimal_from(position.quantity, "position.quantity") <= ZERO:
            return self._terminal_reject(
                view, plan, ReasonCode.NO_OPEN_POSITION, "position is no longer open"
            )
        if (
            position.id != plan.position_id
            or position.setup_id != plan.setup_id
            or position.side.value != plan.side
        ):
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.POSITION_SNAPSHOT_MISMATCH,
                "position identity or side changed",
            )
        current_stop = (
            None
            if position.stop_loss is None
            else decimal_from(position.stop_loss, "position.stop_loss")
        )
        if (
            decimal_from(position.quantity, "position.quantity") != plan.current_quantity
            or decimal_from(position.entry_price, "position.entry_price")
            != plan.current_average_price
            or current_stop != plan.stop_price
        ):
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.POSITION_SNAPSHOT_MISMATCH,
                "position snapshot changed after planning",
            )
        if view.exit_pending:
            return self._terminal_reject(
                view, plan, ReasonCode.EXIT_PENDING, "exit has priority over adjustment"
            )
        if plan.direction is AdjustmentDirection.INCREASE and view.data_gap:
            return self._terminal_reject(
                view, plan, ReasonCode.DATA_GAP, "scale-in is forbidden on a data gap"
            )
        self._counters["received"] += 1
        if plan.direction is not AdjustmentDirection.INCREASE:
            self._remember(plan.adjustment_id, payload_fingerprint)
            approved = ApprovedAdjustment(
                adjustment_id=plan.adjustment_id,
                position_id=plan.position_id,
                symbol=plan.symbol,
                side=plan.side,
                action=plan.action,
                direction=plan.direction,
                approved_quantity=plan.desired_quantity,
                desired_price=plan.desired_price,
                risk_request_id=None,
                reason_code=ReasonCode.REDUCTION_APPROVED,
                approved_at=view.current.close_time,
                plan=plan.to_mapping(),
            )
            self._counters["approved"] += 1
            return self._publish(
                view, FEATURE_APPROVED, approved.to_mapping(), plan.adjustment_id, plan
            )
        decision_feature = view.features.latest(FEATURE_RISK_DECISION, symbol=plan.symbol)
        if decision_feature is None or not isinstance(decision_feature.value, Mapping):
            return ModuleDecision(
                skips=(ReasonCode.RISK_DECISION_MISSING.value,),
                diagnostics=self._diagnostics(),
            )
        decision = decision_feature.value
        if (
            decision_feature.schema_version != FEATURE_SCHEMA_VERSION
            or decision_feature.created_at != view.current.close_time
            or str(decision.get("schema_version", "")) != "1"
        ):
            return self._terminal_reject(
                view, plan, ReasonCode.RISK_DECISION_STALE, "risk decision is stale"
            )
        if str(decision.get("request_id", "")) != plan.risk_request_id:
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.RISK_DECISION_MISMATCH,
                "risk request identity mismatch",
            )
        if str(decision.get("symbol", "")) != plan.symbol:
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.RISK_DECISION_MISMATCH,
                "risk decision symbol mismatch",
            )
        decision_setup = None if decision.get("setup_id") is None else str(decision.get("setup_id"))
        if decision_setup != plan.setup_id:
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.RISK_DECISION_MISMATCH,
                "risk decision setup mismatch",
            )
        if decision.get("approved") is not True or str(decision.get("status", "")) != "approved":
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.RISK_REJECTED,
                str(decision.get("message", "risk rejected")),
            )
        try:
            risk_quantity = decimal_from(decision.get("quantity"), "risk decision quantity")
        except (TypeError, ValueError):
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.RISK_DECISION_INVALID,
                "risk decision quantity is invalid",
            )
        try:
            _validate_typed_risk_adjustment(decision, plan, risk_quantity)
        except (KeyError, TypeError, ValueError):
            return self._terminal_reject(
                view,
                plan,
                ReasonCode.RISK_DECISION_INVALID,
                "typed risk adjustment decision is invalid",
            )
        approved_quantity = min(
            plan.desired_quantity,
            plan.maximum_quantity,
            risk_quantity,
        )
        if approved_quantity <= ZERO:
            return self._terminal_reject(
                view, plan, ReasonCode.RISK_REJECTED, "risk approved zero quantity"
            )
        self._remember(plan.adjustment_id, payload_fingerprint)
        approved = ApprovedAdjustment(
            adjustment_id=plan.adjustment_id,
            position_id=plan.position_id,
            symbol=plan.symbol,
            side=plan.side,
            action=plan.action,
            direction=plan.direction,
            approved_quantity=approved_quantity,
            desired_price=plan.desired_price,
            risk_request_id=plan.risk_request_id,
            reason_code=ReasonCode.RISK_APPROVED,
            approved_at=view.current.close_time,
            plan=plan.to_mapping(),
            risk_decision=decision,
        )
        self._counters["approved"] += 1
        return self._publish(
            view, FEATURE_APPROVED, approved.to_mapping(), plan.adjustment_id, plan
        )

    def snapshot_state(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "max_processed_adjustments": self.max_processed_adjustments,
            "fingerprints": {key: self._fingerprints[key] for key in self._order},
            "order": list(self._order),
            "last_index": self._last_index,
            "counters": dict(self._counters),
        }
        payload["state_fingerprint"] = fingerprint(payload)
        return payload

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not state:
            self._fingerprints.clear()
            self._order.clear()
            self._last_index = -1
            self._counters = {
                "received": 0,
                "approved": 0,
                "rejected": 0,
                "duplicates": 0,
                "conflicts": 0,
            }
            return
        schema_version = str(state.get("schema_version", ""))
        if schema_version not in {"2", "3", STATE_SCHEMA_VERSION}:
            raise ValueError("unsupported approval state schema_version")
        allowed = {
            "schema_version",
            "max_processed_adjustments",
            "fingerprints",
            "order",
            "last_index",
            "counters",
            "state_fingerprint",
        }
        unknown = sorted(set(state) - allowed)
        if unknown:
            raise ValueError(f"unknown approval state fields: {', '.join(unknown)}")
        state_fingerprint = state.get("state_fingerprint")
        if schema_version == STATE_SCHEMA_VERSION:
            if not isinstance(state_fingerprint, str) or not state_fingerprint:
                raise ValueError("approval state fingerprint is required")
            fingerprint_payload = dict(state)
            fingerprint_payload.pop("state_fingerprint", None)
            if fingerprint(fingerprint_payload) != state_fingerprint:
                raise ValueError("approval state fingerprint mismatch")
        elif state_fingerprint is not None:
            raise ValueError("legacy approval state cannot carry state_fingerprint")
        if state.get("max_processed_adjustments") != self.max_processed_adjustments:
            raise ValueError("approval state configuration mismatch")
        fingerprints = state.get("fingerprints")
        order = state.get("order")
        counters = state.get("counters")
        last_index = state.get("last_index")
        if (
            not isinstance(fingerprints, Mapping)
            or isinstance(order, (str, bytes, bytearray))
            or not isinstance(order, Sequence)
            or not isinstance(counters, Mapping)
        ):
            raise ValueError("malformed approval state")
        if len(order) > self.max_processed_adjustments or len(order) != len(
            set(str(item) for item in order)
        ):
            raise ValueError("approval order is invalid or oversized")
        if set(str(item) for item in order) != set(str(key) for key in fingerprints):
            raise ValueError("approval fingerprints and order disagree")
        if isinstance(last_index, bool) or not isinstance(last_index, int):
            raise ValueError("last_index must be integer")
        restored_counters: dict[str, int] = {}
        for key in self._counters:
            value = counters.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("invalid approval counter")
            restored_counters[key] = value
        self._fingerprints = {str(key): str(value) for key, value in fingerprints.items()}
        self._order = deque(str(item) for item in order)
        self._last_index = last_index
        self._counters = restored_counters

    def _terminal_reject(
        self,
        view: MarketView,
        plan: PositionAdjustmentPlan,
        reason: ReasonCode,
        message: str,
    ) -> ModuleDecision:
        self._remember(plan.adjustment_id, fingerprint(plan.to_mapping()))
        return self._reject(view, plan, reason, message)

    def _reject(
        self,
        view: MarketView,
        plan: PositionAdjustmentPlan,
        reason: ReasonCode,
        message: str,
    ) -> ModuleDecision:
        rejected = RejectedAdjustment(
            adjustment_id=plan.adjustment_id,
            position_id=plan.position_id,
            symbol=plan.symbol,
            reason_code=reason,
            message=message or reason.value,
            rejected_at=view.current.close_time,
            risk_request_id=plan.risk_request_id,
        )
        self._counters["rejected"] += 1
        return self._publish(
            view, FEATURE_REJECTED, rejected.to_mapping(), plan.adjustment_id, plan
        )

    def _publish(
        self,
        view: MarketView,
        key: str,
        value: Mapping[str, Any],
        semantic_id: str,
        plan: PositionAdjustmentPlan,
    ) -> ModuleDecision:
        bar_seconds = max(
            1,
            int((view.current.close_time - view.current.timestamp).total_seconds()),
        )
        feedback_valid_until = plan.expires_at + timedelta(seconds=bar_seconds)
        feature = Feature(
            key=key,
            source_module=self.name,
            created_at=view.current.close_time,
            value=value,
            symbol=view.current.symbol,
            timeframe=view.current.timeframe,
            valid_until=feedback_valid_until,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=stable_id("feature", {"key": key, "id": semantic_id}),
            invalidate_on_gap=False,
            metadata={"adjustment_id": semantic_id},
        )
        return ModuleDecision(features=(feature,), diagnostics=self._diagnostics())

    def _remember(self, adjustment_id: str, value: str) -> None:
        self._fingerprints[adjustment_id] = value
        self._order.append(adjustment_id)
        while len(self._order) > self.max_processed_adjustments:
            removed = self._order.popleft()
            self._fingerprints.pop(removed, None)

    def _diagnostics(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "counters": MappingProxyType(dict(self._counters)),
                "cache_size": len(self._order),
                "cache_capacity": self.max_processed_adjustments,
                "last_index": self._last_index,
                "state_schema_version": STATE_SCHEMA_VERSION,
            }
        )


def _extract_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        candidate = value.get("valid")
        return candidate if isinstance(candidate, bool) else None
    return None


def _extract_regime(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        candidate = value.get("regime") or value.get("state")
        return str(candidate) if candidate is not None else None
    return None


def _extract_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        if "value" in value:
            value = value["value"]
        else:
            value = value.get("atr")
    try:
        return decimal_from(value, "feature decimal")
    except (TypeError, ValueError):
        return None
