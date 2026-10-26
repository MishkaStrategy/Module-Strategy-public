from __future__ import annotations

from enum import StrEnum


class TradeSide(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def order_side(self) -> str:
        return "buy" if self is TradeSide.LONG else "sell"

    @property
    def opposite(self) -> "TradeSide":
        return TradeSide.SHORT if self is TradeSide.LONG else TradeSide.LONG


class TradeAction(StrEnum):
    OPEN_POSITION = "open_position"
    ADD_TO_POSITION = "add_to_position"
    REDUCE_POSITION = "reduce_position"
    CLOSE_POSITION = "close_position"
    REVERSE_POSITION = "reverse_position"
    REPLACE_PENDING_PLAN = "replace_pending_plan"
    CANCEL_PENDING_PLAN = "cancel_pending_plan"
    UPDATE_PROTECTION = "update_protection"
    NO_ACTION = "no_action"
    REJECTED = "rejected"


class PlanVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReasonCode(StrEnum):
    ACCEPTED = "accepted"
    MISSING_ENTRY_DECISION = "missing_entry_decision"
    MISSING_PROTECTION = "missing_protection"
    MISSING_SIZING = "missing_sizing"
    SIZING_REJECTED = "sizing_rejected"
    STALE_DECISION = "stale_decision"
    FUTURE_DECISION = "future_decision"
    PORTFOLIO_STATE_STALE = "portfolio_state_stale"
    LONG_SHORT_CONFLICT = "long_short_conflict"
    SETUP_CONFLICT = "setup_conflict"
    POSITION_CONFLICT = "position_conflict"
    PENDING_ORDER_CONFLICT = "pending_order_conflict"
    ACTIVE_PLAN_CONFLICT = "active_plan_conflict"
    DUPLICATE_DECISION = "duplicate_decision"
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    EXPOSURE_LIMIT_EXCEEDED = "exposure_limit_exceeded"
    PORTFOLIO_LOCKED = "portfolio_locked"
    INSTRUMENT_LOCKED = "instrument_locked"
    STRATEGY_LOCKED = "strategy_locked"
    CIRCUIT_BREAKER_ACTIVE = "circuit_breaker_active"
    EMERGENCY_STATE_ACTIVE = "emergency_state_active"
    DAILY_LOSS_LIMIT_ACTIVE = "daily_loss_limit_active"
    DRAWDOWN_LIMIT_ACTIVE = "drawdown_limit_active"
    INVALID_SCALING = "invalid_scaling"
    INCOMPATIBLE_DECISIONS = "incompatible_decisions"
    UNSUPPORTED_POLICY = "unsupported_policy"
    PLAN_INVARIANT_FAILED = "plan_invariant_failed"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_INPUT = "invalid_input"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
    STALE_PLAN = "stale_plan"
    REPLACEMENT_REQUIRED = "replacement_required"
    CANCELLATION_REQUIRED = "cancellation_required"


class LongShortConflictPolicy(StrEnum):
    REJECT_ALL = "reject_all"
    HIGHER_PRIORITY = "higher_priority"
    EARLIEST_SIGNAL = "earliest_signal"
    FRESHEST_SIGNAL = "freshest_signal"


class ExistingPositionPolicy(StrEnum):
    REJECT = "reject"
    ALLOW_SAME_SIDE_ADD = "allow_same_side_add"
    CLOSE_THEN_REVERSE = "close_then_reverse"
    HEDGE = "hedge"


class PendingOrderPolicy(StrEnum):
    REJECT = "reject"
    REPLACE_SAME_SETUP = "replace_same_setup"
    CANCEL_OPPOSITE = "cancel_opposite"


class ActivePlanPolicy(StrEnum):
    REJECT_NEW_EXPOSURE = "reject_new_exposure"
    ALLOW_MULTIPLE = "allow_multiple"


class MultiSetupPolicy(StrEnum):
    HIGHEST_PRIORITY = "highest_priority"
    REJECT = "reject"


class ScalingAction(StrEnum):
    ADD = "add"
    REDUCE = "reduce"
    REBALANCE = "rebalance"
    HOLD = "hold"
    STOP_SCALING = "stop_scaling"


class ScalingDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    NONE = "none"
