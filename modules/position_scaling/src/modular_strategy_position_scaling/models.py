from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .canonical import deep_freeze, deep_thaw
from .decimal_utils import (
    ZERO,
    canonical_decimal,
    nonnegative_decimal,
    positive_decimal,
    weighted_average,
)
from .enums import (
    AdjustmentAction,
    AdjustmentDirection,
    QuantityKind,
    ReasonCode,
    ScalingMode,
    TriggerKind,
)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-blank")
    if value != value.strip():
        raise ValueError(f"{name} cannot have surrounding whitespace")
    return value


def _strict_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class ScalingRule:
    stage: int
    mode: ScalingMode
    action: AdjustmentAction
    trigger_kind: TriggerKind
    trigger_value: Decimal
    quantity_kind: QuantityKind
    quantity_value: Decimal
    desired_entry_kind: str = "limit"
    explicit_price: Decimal | None = None
    cooldown_bars: int = 1
    expires_bars: int = 1
    feature_key: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.stage, bool) or not isinstance(self.stage, int) or self.stage < 1:
            raise ValueError("stage must be an integer >= 1")
        object.__setattr__(
            self, "trigger_value", nonnegative_decimal(self.trigger_value, "trigger_value")
        )
        object.__setattr__(
            self, "quantity_value", positive_decimal(self.quantity_value, "quantity_value")
        )
        if self.explicit_price is not None:
            object.__setattr__(
                self, "explicit_price", positive_decimal(self.explicit_price, "explicit_price")
            )
        for name in ("cooldown_bars", "expires_bars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.trigger_kind is TriggerKind.FEATURE_TRUE:
            if self.feature_key is None or "." not in self.feature_key:
                raise ValueError("feature_true trigger requires a namespaced feature_key")
        if self.trigger_kind is TriggerKind.PRICE_LADDER and self.explicit_price is None:
            raise ValueError("price_ladder trigger requires explicit_price")
        if self.action is AdjustmentAction.ADD and self.mode in {
            ScalingMode.SCALE_OUT,
            ScalingMode.REBALANCE,
        }:
            raise ValueError("ADD action is incompatible with scale_out/rebalance mode")
        if self.action is AdjustmentAction.REDUCE and self.mode not in {
            ScalingMode.SCALE_OUT,
            ScalingMode.REBALANCE,
        }:
            raise ValueError("REDUCE action requires scale_out or rebalance mode")
        if self.mode is ScalingMode.REBALANCE and self.action not in {
            AdjustmentAction.REBALANCE,
            AdjustmentAction.STOP_SCALING,
        }:
            raise ValueError("rebalance mode requires REBALANCE action")
        if self.action is AdjustmentAction.REBALANCE and self.mode is not ScalingMode.REBALANCE:
            raise ValueError("REBALANCE action requires rebalance mode")
        target_kinds = {
            QuantityKind.TARGET_QUANTITY,
            QuantityKind.TARGET_NOTIONAL,
            QuantityKind.EQUITY_FRACTION,
        }
        if self.action is AdjustmentAction.REBALANCE and self.quantity_kind not in target_kinds:
            raise ValueError("rebalance requires a target quantity kind")
        if self.action is not AdjustmentAction.REBALANCE and self.quantity_kind in target_kinds:
            raise ValueError("target quantity kinds are reserved for rebalance")
        if (
            self.trigger_kind is TriggerKind.BARS
            and self.trigger_value != self.trigger_value.to_integral_value()
        ):
            raise ValueError("bars trigger_value must be an integer")
        if self.quantity_kind in {
            QuantityKind.INITIAL_FRACTION,
            QuantityKind.CURRENT_FRACTION,
            QuantityKind.EQUITY_FRACTION,
            QuantityKind.WEIGHT,
        } and self.quantity_value > Decimal("1"):
            raise ValueError("fractional quantity_value must be <= 1")
        object.__setattr__(
            self, "desired_entry_kind", _nonblank(self.desired_entry_kind, "desired_entry_kind")
        )
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "stage": self.stage,
            "mode": self.mode.value,
            "action": self.action.value,
            "trigger_kind": self.trigger_kind.value,
            "trigger_value": canonical_decimal(self.trigger_value),
            "quantity_kind": self.quantity_kind.value,
            "quantity_value": canonical_decimal(self.quantity_value),
            "desired_entry_kind": self.desired_entry_kind,
            "explicit_price": None
            if self.explicit_price is None
            else canonical_decimal(self.explicit_price),
            "cooldown_bars": self.cooldown_bars,
            "expires_bars": self.expires_bars,
            "feature_key": self.feature_key,
            "metadata": deep_thaw(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PositionAdjustmentPlan:
    adjustment_id: str
    position_id: str
    setup_id: str | None
    symbol: str
    side: str
    action: AdjustmentAction
    direction: AdjustmentDirection
    scaling_mode: ScalingMode
    stage: int
    decision_time: datetime
    decision_index: int
    desired_entry_kind: str
    desired_price: Decimal
    trigger_price: Decimal
    desired_quantity: Decimal
    maximum_quantity: Decimal
    risk_budget: Decimal
    notional_budget: Decimal
    current_average_price: Decimal
    expected_average_price: Decimal
    current_quantity: Decimal
    expected_total_quantity: Decimal
    current_total_risk: Decimal
    projected_total_risk: Decimal
    maximum_total_risk: Decimal
    stop_price: Decimal
    invalidation_price: Decimal
    expires_at: datetime
    expires_index: int
    reason_code: ReasonCode
    risk_request_id: str | None
    metadata: Mapping[str, Any]
    source_feature_ids: tuple[str, ...]
    config_fingerprint: str
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in (
            "adjustment_id",
            "position_id",
            "symbol",
            "side",
            "desired_entry_kind",
            "config_fingerprint",
        ):
            _nonblank(str(getattr(self, name)), name)
        if self.setup_id is not None:
            _nonblank(self.setup_id, "setup_id")
        if self.side not in {"long", "short"}:
            raise ValueError("side must be long or short")
        for name, expected in (
            ("action", AdjustmentAction),
            ("direction", AdjustmentDirection),
            ("scaling_mode", ScalingMode),
            ("reason_code", ReasonCode),
        ):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f"{name} must be {expected.__name__}")
        for name in ("stage", "decision_index", "expires_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.stage < 1 or self.decision_index < 0:
            raise ValueError("stage must be >= 1 and decision_index must be >= 0")
        if self.schema_version != "1":
            raise ValueError("unsupported plan schema_version")
        object.__setattr__(self, "decision_time", _utc(self.decision_time, "decision_time"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        if self.expires_at < self.decision_time:
            raise ValueError("expires_at cannot precede decision_time")
        if self.expires_index < self.decision_index:
            raise ValueError("expires_index cannot precede decision_index")
        for name in (
            "desired_price",
            "trigger_price",
            "current_average_price",
            "expected_average_price",
            "current_quantity",
            "expected_total_quantity",
            "stop_price",
            "invalidation_price",
        ):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        for name in ("desired_quantity", "maximum_quantity"):
            object.__setattr__(self, name, nonnegative_decimal(getattr(self, name), name))
        for name in (
            "risk_budget",
            "notional_budget",
            "current_total_risk",
            "projected_total_risk",
            "maximum_total_risk",
        ):
            object.__setattr__(self, name, nonnegative_decimal(getattr(self, name), name))
        if self.maximum_quantity < self.desired_quantity:
            raise ValueError("maximum_quantity cannot be below desired_quantity")
        expected_direction = {
            AdjustmentAction.ADD: AdjustmentDirection.INCREASE,
            AdjustmentAction.REDUCE: AdjustmentDirection.DECREASE,
            AdjustmentAction.STOP_SCALING: AdjustmentDirection.NONE,
            AdjustmentAction.HOLD: AdjustmentDirection.NONE,
            AdjustmentAction.REJECTED: AdjustmentDirection.NONE,
        }.get(self.action)
        if expected_direction is not None and self.direction is not expected_direction:
            raise ValueError("action and direction are inconsistent")
        if self.action is AdjustmentAction.REBALANCE and self.direction is AdjustmentDirection.NONE:
            raise ValueError("rebalance plan requires a quantity-changing direction")
        if (
            self.direction in {AdjustmentDirection.INCREASE, AdjustmentDirection.DECREASE}
            and self.desired_quantity <= ZERO
        ):
            raise ValueError("quantity-changing plan requires positive desired_quantity")
        if self.direction is AdjustmentDirection.NONE and self.desired_quantity != ZERO:
            raise ValueError("non-quantity plan requires zero desired_quantity")
        if (
            self.direction is AdjustmentDirection.INCREASE
            and self.expected_total_quantity <= self.current_quantity
        ):
            raise ValueError("increase plan must increase total quantity")
        if (
            self.direction is AdjustmentDirection.DECREASE
            and self.expected_total_quantity >= self.current_quantity
        ):
            raise ValueError("decrease plan must reduce total quantity")
        if self.direction is AdjustmentDirection.INCREASE and not self.risk_request_id:
            raise ValueError("increase plan requires risk_request_id")
        if self.direction is not AdjustmentDirection.INCREASE and self.risk_request_id is not None:
            raise ValueError("non-increase plan cannot carry risk_request_id")
        if self.direction is AdjustmentDirection.INCREASE:
            expected_total = self.current_quantity + self.desired_quantity
            if self.expected_total_quantity != expected_total:
                raise ValueError("increase expected_total_quantity is inconsistent")
            expected_average = weighted_average(
                self.current_average_price,
                self.current_quantity,
                self.desired_price,
                self.desired_quantity,
            )
            if self.expected_average_price != expected_average:
                raise ValueError("increase expected_average_price is inconsistent")
        elif self.direction is AdjustmentDirection.DECREASE:
            expected_total = self.current_quantity - self.desired_quantity
            if expected_total <= ZERO or self.expected_total_quantity != expected_total:
                raise ValueError("decrease expected_total_quantity is inconsistent")
            if self.expected_average_price != self.current_average_price:
                raise ValueError("decrease expected_average_price must preserve average price")
        else:
            if self.expected_total_quantity != self.current_quantity:
                raise ValueError("non-quantity plan must preserve total quantity")
            if self.expected_average_price != self.current_average_price:
                raise ValueError("non-quantity plan must preserve average price")
        if self.side == "long" and self.stop_price < self.invalidation_price:
            raise ValueError("long plan cannot weaken stop beyond invalidation")
        if self.side == "short" and self.stop_price > self.invalidation_price:
            raise ValueError("short plan cannot weaken stop beyond invalidation")
        if len(self.source_feature_ids) != len(set(self.source_feature_ids)):
            raise ValueError("source_feature_ids must be unique")
        if any(not isinstance(item, str) or not item for item in self.source_feature_ids):
            raise ValueError("source_feature_ids must contain non-empty strings")
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))
        object.__setattr__(self, "source_feature_ids", tuple(self.source_feature_ids))

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adjustment_id": self.adjustment_id,
            "position_id": self.position_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action.value,
            "direction": self.direction.value,
            "scaling_mode": self.scaling_mode.value,
            "stage": self.stage,
            "decision_time": self.decision_time.isoformat(),
            "decision_index": self.decision_index,
            "desired_entry_kind": self.desired_entry_kind,
            "desired_price": canonical_decimal(self.desired_price),
            "trigger_price": canonical_decimal(self.trigger_price),
            "desired_quantity": canonical_decimal(self.desired_quantity),
            "maximum_quantity": canonical_decimal(self.maximum_quantity),
            "risk_budget": canonical_decimal(self.risk_budget),
            "notional_budget": canonical_decimal(self.notional_budget),
            "current_average_price": canonical_decimal(self.current_average_price),
            "expected_average_price": canonical_decimal(self.expected_average_price),
            "current_quantity": canonical_decimal(self.current_quantity),
            "expected_total_quantity": canonical_decimal(self.expected_total_quantity),
            "current_total_risk": canonical_decimal(self.current_total_risk),
            "projected_total_risk": canonical_decimal(self.projected_total_risk),
            "maximum_total_risk": canonical_decimal(self.maximum_total_risk),
            "stop_price": canonical_decimal(self.stop_price),
            "invalidation_price": canonical_decimal(self.invalidation_price),
            "expires_at": self.expires_at.isoformat(),
            "expires_index": self.expires_index,
            "reason_code": self.reason_code.value,
            "risk_request_id": self.risk_request_id,
            "metadata": deep_thaw(self.metadata),
            "source_feature_ids": self.source_feature_ids,
            "config_fingerprint": self.config_fingerprint,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> PositionAdjustmentPlan:
        allowed = {
            "schema_version",
            "adjustment_id",
            "position_id",
            "setup_id",
            "symbol",
            "side",
            "action",
            "direction",
            "scaling_mode",
            "stage",
            "decision_time",
            "decision_index",
            "desired_entry_kind",
            "desired_price",
            "trigger_price",
            "desired_quantity",
            "maximum_quantity",
            "risk_budget",
            "notional_budget",
            "current_average_price",
            "expected_average_price",
            "current_quantity",
            "expected_total_quantity",
            "current_total_risk",
            "projected_total_risk",
            "maximum_total_risk",
            "stop_price",
            "invalidation_price",
            "expires_at",
            "expires_index",
            "reason_code",
            "risk_request_id",
            "metadata",
            "source_feature_ids",
            "config_fingerprint",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown plan fields: {', '.join(unknown)}")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("plan metadata must be a mapping")
        source_ids = payload.get("source_feature_ids", ())
        if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, (list, tuple)):
            raise ValueError("source_feature_ids must be an array")
        return cls(
            adjustment_id=str(payload.get("adjustment_id", "")),
            position_id=str(payload.get("position_id", "")),
            setup_id=None if payload.get("setup_id") is None else str(payload["setup_id"]),
            symbol=str(payload.get("symbol", "")),
            side=str(payload.get("side", "")),
            action=AdjustmentAction(str(payload.get("action", ""))),
            direction=AdjustmentDirection(str(payload.get("direction", ""))),
            scaling_mode=ScalingMode(str(payload.get("scaling_mode", ""))),
            stage=_strict_int(payload.get("stage"), "stage", minimum=1),
            decision_time=datetime.fromisoformat(str(payload.get("decision_time", ""))),
            decision_index=_strict_int(payload.get("decision_index"), "decision_index", minimum=0),
            desired_entry_kind=str(payload.get("desired_entry_kind", "")),
            desired_price=positive_decimal(payload.get("desired_price"), "desired_price"),
            trigger_price=positive_decimal(payload.get("trigger_price"), "trigger_price"),
            desired_quantity=nonnegative_decimal(
                payload.get("desired_quantity"), "desired_quantity"
            ),
            maximum_quantity=nonnegative_decimal(
                payload.get("maximum_quantity"), "maximum_quantity"
            ),
            risk_budget=nonnegative_decimal(payload.get("risk_budget", 0), "risk_budget"),
            notional_budget=nonnegative_decimal(
                payload.get("notional_budget", 0), "notional_budget"
            ),
            current_average_price=positive_decimal(
                payload.get("current_average_price"), "current_average_price"
            ),
            expected_average_price=positive_decimal(
                payload.get("expected_average_price"), "expected_average_price"
            ),
            current_quantity=positive_decimal(payload.get("current_quantity"), "current_quantity"),
            expected_total_quantity=positive_decimal(
                payload.get("expected_total_quantity"), "expected_total_quantity"
            ),
            current_total_risk=nonnegative_decimal(
                payload.get("current_total_risk", 0), "current_total_risk"
            ),
            projected_total_risk=nonnegative_decimal(
                payload.get("projected_total_risk", 0), "projected_total_risk"
            ),
            maximum_total_risk=nonnegative_decimal(
                payload.get("maximum_total_risk", 0), "maximum_total_risk"
            ),
            stop_price=positive_decimal(payload.get("stop_price"), "stop_price"),
            invalidation_price=positive_decimal(
                payload.get("invalidation_price"), "invalidation_price"
            ),
            expires_at=datetime.fromisoformat(str(payload.get("expires_at", ""))),
            expires_index=_strict_int(payload.get("expires_index"), "expires_index", minimum=0),
            reason_code=ReasonCode(str(payload.get("reason_code", ""))),
            risk_request_id=None
            if payload.get("risk_request_id") is None
            else str(payload["risk_request_id"]),
            metadata=metadata,
            source_feature_ids=tuple(str(item) for item in source_ids),
            config_fingerprint=str(payload.get("config_fingerprint", "")),
            schema_version=str(payload.get("schema_version", "")),
        )


@dataclass(frozen=True, slots=True)
class ApprovedAdjustment:
    adjustment_id: str
    position_id: str
    symbol: str
    side: str
    action: AdjustmentAction
    direction: AdjustmentDirection
    approved_quantity: Decimal
    desired_price: Decimal
    risk_request_id: str | None
    reason_code: ReasonCode
    approved_at: datetime
    plan: Mapping[str, Any]
    risk_decision: Mapping[str, Any] | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("adjustment_id", "position_id", "symbol", "side"):
            _nonblank(str(getattr(self, name)), name)
        if self.schema_version != "1":
            raise ValueError("unsupported approved adjustment schema_version")
        if self.side not in {"long", "short"}:
            raise ValueError("side must be long or short")
        if not isinstance(self.action, AdjustmentAction) or not isinstance(
            self.direction, AdjustmentDirection
        ):
            raise TypeError("action and direction must be typed enums")
        if not isinstance(self.reason_code, ReasonCode):
            raise TypeError("reason_code must be ReasonCode")
        if not isinstance(self.plan, Mapping):
            raise TypeError("plan must be a mapping")
        if self.risk_decision is not None and not isinstance(self.risk_decision, Mapping):
            raise TypeError("risk_decision must be a mapping or null")
        object.__setattr__(
            self,
            "approved_quantity",
            nonnegative_decimal(self.approved_quantity, "approved_quantity"),
        )
        if (
            self.direction in {AdjustmentDirection.INCREASE, AdjustmentDirection.DECREASE}
            and self.approved_quantity <= ZERO
        ):
            raise ValueError("quantity-changing approval requires positive quantity")
        object.__setattr__(
            self, "desired_price", positive_decimal(self.desired_price, "desired_price")
        )
        object.__setattr__(self, "approved_at", _utc(self.approved_at, "approved_at"))
        object.__setattr__(self, "plan", deep_freeze(self.plan))
        object.__setattr__(
            self,
            "risk_decision",
            None if self.risk_decision is None else deep_freeze(self.risk_decision),
        )
        embedded = PositionAdjustmentPlan.from_mapping(self.plan)
        if (
            self.adjustment_id != embedded.adjustment_id
            or self.position_id != embedded.position_id
            or self.symbol != embedded.symbol
            or self.side != embedded.side
            or self.action is not embedded.action
            or self.direction is not embedded.direction
            or self.desired_price != embedded.desired_price
            or self.risk_request_id != embedded.risk_request_id
        ):
            raise ValueError("approved adjustment disagrees with embedded plan")
        if self.approved_quantity > min(embedded.desired_quantity, embedded.maximum_quantity):
            raise ValueError("approved quantity exceeds embedded plan")
        if self.direction is AdjustmentDirection.INCREASE:
            if self.reason_code is not ReasonCode.RISK_APPROVED:
                raise ValueError("increase approval requires risk_approved reason")
            if self.risk_decision is None:
                raise ValueError("increase approval requires risk_decision")
            decision = self.risk_decision
            if (
                str(decision.get("schema_version", "")) != "1"
                or decision.get("approved") is not True
                or str(decision.get("status", "")) != "approved"
                or str(decision.get("request_id", "")) != self.risk_request_id
                or str(decision.get("symbol", "")) != self.symbol
                or decision.get("setup_id") != embedded.setup_id
            ):
                raise ValueError("risk_decision disagrees with approved adjustment")
            decision_quantity = positive_decimal(decision.get("quantity"), "risk_decision.quantity")
            if self.approved_quantity != min(
                decision_quantity, embedded.desired_quantity, embedded.maximum_quantity
            ):
                raise ValueError("approved quantity does not match capped risk decision")
        else:
            if self.reason_code is not ReasonCode.REDUCTION_APPROVED:
                raise ValueError("non-increase approval requires reduction_approved reason")
            if self.risk_decision is not None:
                raise ValueError("non-increase approval cannot carry risk_decision")

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adjustment_id": self.adjustment_id,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action.value,
            "direction": self.direction.value,
            "approved_quantity": canonical_decimal(self.approved_quantity),
            "desired_price": canonical_decimal(self.desired_price),
            "risk_request_id": self.risk_request_id,
            "reason_code": self.reason_code.value,
            "approved_at": self.approved_at.isoformat(),
            "plan": deep_thaw(self.plan),
            "risk_decision": None if self.risk_decision is None else deep_thaw(self.risk_decision),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ApprovedAdjustment:
        allowed = {
            "schema_version",
            "adjustment_id",
            "position_id",
            "symbol",
            "side",
            "action",
            "direction",
            "approved_quantity",
            "desired_price",
            "risk_request_id",
            "reason_code",
            "approved_at",
            "plan",
            "risk_decision",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown approved adjustment fields: {', '.join(unknown)}")
        plan = payload.get("plan")
        risk_decision = payload.get("risk_decision")
        if not isinstance(plan, Mapping):
            raise ValueError("approved adjustment plan must be a mapping")
        if risk_decision is not None and not isinstance(risk_decision, Mapping):
            raise ValueError("approved adjustment risk_decision must be a mapping or null")
        result = cls(
            adjustment_id=str(payload.get("adjustment_id", "")),
            position_id=str(payload.get("position_id", "")),
            symbol=str(payload.get("symbol", "")),
            side=str(payload.get("side", "")),
            action=AdjustmentAction(str(payload.get("action", ""))),
            direction=AdjustmentDirection(str(payload.get("direction", ""))),
            approved_quantity=nonnegative_decimal(
                payload.get("approved_quantity"), "approved_quantity"
            ),
            desired_price=positive_decimal(payload.get("desired_price"), "desired_price"),
            risk_request_id=(
                None if payload.get("risk_request_id") is None else str(payload["risk_request_id"])
            ),
            reason_code=ReasonCode(str(payload.get("reason_code", ""))),
            approved_at=datetime.fromisoformat(str(payload.get("approved_at", ""))),
            plan=plan,
            risk_decision=risk_decision,
            schema_version=str(payload.get("schema_version", "")),
        )
        return result


@dataclass(frozen=True, slots=True)
class RejectedAdjustment:
    adjustment_id: str
    position_id: str
    symbol: str
    reason_code: ReasonCode
    message: str
    rejected_at: datetime
    risk_request_id: str | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("adjustment_id", "position_id", "symbol", "message"):
            _nonblank(str(getattr(self, name)), name)
        if self.schema_version != "1":
            raise ValueError("unsupported rejected adjustment schema_version")
        if not isinstance(self.reason_code, ReasonCode):
            raise TypeError("reason_code must be ReasonCode")
        if self.risk_request_id is not None:
            _nonblank(self.risk_request_id, "risk_request_id")
        object.__setattr__(self, "rejected_at", _utc(self.rejected_at, "rejected_at"))

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adjustment_id": self.adjustment_id,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "rejected_at": self.rejected_at.isoformat(),
            "risk_request_id": self.risk_request_id,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RejectedAdjustment:
        allowed = {
            "schema_version",
            "adjustment_id",
            "position_id",
            "symbol",
            "reason_code",
            "message",
            "rejected_at",
            "risk_request_id",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown rejected adjustment fields: {', '.join(unknown)}")
        return cls(
            adjustment_id=str(payload.get("adjustment_id", "")),
            position_id=str(payload.get("position_id", "")),
            symbol=str(payload.get("symbol", "")),
            reason_code=ReasonCode(str(payload.get("reason_code", ""))),
            message=str(payload.get("message", "")),
            rejected_at=datetime.fromisoformat(str(payload.get("rejected_at", ""))),
            risk_request_id=(
                None if payload.get("risk_request_id") is None else str(payload["risk_request_id"])
            ),
            schema_version=str(payload.get("schema_version", "")),
        )
