from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any
import unicodedata

from .canonical import (
    canonical_value,
    deep_freeze,
    has_control_characters,
    sha256_hex,
    stable_id,
)
from .decimal_math import (
    DecimalDomainError,
    exact_abs_difference,
    exact_sum,
    validate_decimal_domain,
)
from .enums import PlanVerdict, ReasonCode, ScalingAction, ScalingDirection, TradeAction, TradeSide
from .errors import PlanInvariantError, RouterInputError
from .limits import MAX_ROUTER_COLLECTION_ITEMS


ZERO = Decimal("0")
ONE = Decimal("1")


MAX_DOMAIN_TEXT_LENGTH = 512
MAX_DETAIL_TEXT_LENGTH = 4_096
MAX_DOMAIN_INTEGER_ABS = 9_223_372_036_854_775_807
MAX_PRIORITY_ABS = 2_147_483_647

PENDING_ORDER_TYPES = frozenset(
    {
        "market",
        "limit",
        "stop",
        "stop_market",
        "stop_limit",
        "take_profit_market",
        "take_profit_limit",
    }
)
ACTIVE_PENDING_ORDER_STATUSES = frozenset(
    {
        "new",
        "pending",
        "partially_filled",
        "cancel_pending",
    }
)
TERMINAL_PENDING_ORDER_STATUSES = frozenset(
    {
        "filled",
        "cancelled",
        "expired",
        "rejected",
    }
)
PENDING_ORDER_STATUSES = ACTIVE_PENDING_ORDER_STATUSES | TERMINAL_PENDING_ORDER_STATUSES


def _text(value: str, name: str, *, max_length: int = MAX_DOMAIN_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouterInputError(f"{name} must be a non-blank string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if len(normalized) > max_length:
        raise RouterInputError(f"{name} exceeds maximum length {max_length}")
    if has_control_characters(normalized):
        raise RouterInputError(f"{name} cannot contain control characters")
    return normalized


def _optional_text(value: str | None, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _sha256_text(value: str, name: str) -> str:
    result = _text(value, name).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise RouterInputError(f"{name} must be a lowercase SHA-256 hex digest")
    return result


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RouterInputError(f"{name} must be boolean")
    return value


def _bounded_int(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int = MAX_DOMAIN_INTEGER_ABS,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouterInputError(f"{name} must be integer")
    if not minimum <= value <= maximum:
        raise RouterInputError(f"{name} must be in range [{minimum}, {maximum}]")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RouterInputError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: Decimal | str | int | float, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise RouterInputError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RouterInputError(f"{name} must be numeric") from exc
    try:
        validate_decimal_domain(result, name)
    except DecimalDomainError as exc:
        raise RouterInputError(str(exc)) from exc
    if positive and result <= ZERO:
        raise RouterInputError(f"{name} must be positive")
    if not positive and result < ZERO:
        raise RouterInputError(f"{name} must be non-negative")
    return result


def _optional_decimal(
    value: Decimal | str | int | float | None, name: str, *, positive: bool = False
) -> Decimal | None:
    return None if value is None else _decimal(value, name, positive=positive)


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise RouterInputError("metadata value must be a mapping")
    try:
        return deep_freeze(value)
    except (TypeError, ValueError) as exc:
        raise RouterInputError(f"invalid mapping content: {exc}") from exc


def _plain_sequence(value: Any, name: str) -> tuple[Any, ...]:
    """Snapshot bounded built-in list/tuple inputs without invoking user code."""
    if type(value) not in {list, tuple}:
        raise RouterInputError(f"{name} must be a built-in list or tuple")
    if len(value) > MAX_ROUTER_COLLECTION_ITEMS:
        raise RouterInputError(
            f"{name} exceeds maximum item count {MAX_ROUTER_COLLECTION_ITEMS}",
            ReasonCode.INPUT_LIMIT_EXCEEDED,
        )
    return tuple(value)


@dataclass(frozen=True, slots=True)
class EntryCandidate:
    candidate_id: str
    source_decision_id: str
    strategy_id: str
    setup_id: str
    module_id: str
    module_version: str
    portfolio_id: str
    symbol: str
    market: str
    side: TradeSide
    signal_time: datetime
    available_at: datetime
    created_at: datetime
    expires_at: datetime
    entry_kind: str
    order_type: str
    ttl_bars: int
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    limit_price: Decimal | None = None
    post_only: bool = False
    priority: int = 0
    confidence: Decimal | None = None
    incompatibility_tags: tuple[str, ...] = ()
    exclusivity_group: str | None = None
    correlation_group: str | None = None
    reason_codes: tuple[str, ...] = ()
    input_hash: str = ""
    schema_version: str = "1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "source_decision_id",
            "strategy_id",
            "setup_id",
            "module_id",
            "module_version",
            "portfolio_id",
            "symbol",
            "market",
            "entry_kind",
            "order_type",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.schema_version != "1":
            raise RouterInputError(
                "unsupported normalized EntryCandidate schema", ReasonCode.UNSUPPORTED_SCHEMA
            )
        if not isinstance(self.side, TradeSide):
            raise RouterInputError("side must be TradeSide")
        object.__setattr__(self, "post_only", _boolean(self.post_only, "post_only"))
        object.__setattr__(
            self, "exclusivity_group", _optional_text(self.exclusivity_group, "exclusivity_group")
        )
        object.__setattr__(
            self, "correlation_group", _optional_text(self.correlation_group, "correlation_group")
        )
        for name in ("signal_time", "available_at", "created_at", "expires_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.available_at < self.signal_time:
            raise RouterInputError("available_at cannot precede signal_time")
        if self.created_at < self.available_at:
            raise RouterInputError("created_at cannot precede available_at")
        if self.expires_at <= self.created_at:
            raise RouterInputError("entry candidate expires_at must be after created_at")
        object.__setattr__(
            self,
            "ttl_bars",
            _bounded_int(self.ttl_bars, "ttl_bars", minimum=1),
        )
        object.__setattr__(
            self,
            "priority",
            _bounded_int(
                self.priority,
                "priority",
                minimum=-MAX_PRIORITY_ABS,
                maximum=MAX_PRIORITY_ABS,
            ),
        )
        for name in ("price", "trigger_price", "limit_price"):
            object.__setattr__(
                self, name, _optional_decimal(getattr(self, name), name, positive=True)
            )
        if self.confidence is not None:
            confidence = _decimal(self.confidence, "confidence")
            if confidence > ONE:
                raise RouterInputError("confidence must be in [0, 1]")
            object.__setattr__(self, "confidence", confidence)
        if self.order_type == "market":
            if any(
                value is not None for value in (self.price, self.trigger_price, self.limit_price)
            ):
                raise RouterInputError("market entry cannot carry price fields")
            if self.post_only:
                raise RouterInputError("market entry cannot be post_only")
        elif self.order_type == "limit":
            if self.limit_price is None and self.price is None:
                raise RouterInputError("limit entry requires a price")
        elif self.order_type == "stop_market":
            if self.trigger_price is None:
                raise RouterInputError("stop_market entry requires trigger_price")
        elif self.order_type == "stop_limit":
            if self.trigger_price is None or self.limit_price is None:
                raise RouterInputError("stop_limit entry requires trigger_price and limit_price")
        else:
            raise RouterInputError(f"unsupported entry order_type: {self.order_type}")
        for name in ("incompatibility_tags", "reason_codes"):
            raw_values = _plain_sequence(getattr(self, name), name)
            values = tuple(_text(item, name) for item in raw_values)
            object.__setattr__(self, name, tuple(sorted(set(values))))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        expected_hash = sha256_hex(self.semantic_mapping(include_hash=False))
        if self.input_hash and self.input_hash != expected_hash:
            raise RouterInputError("entry candidate input_hash does not match canonical content")
        object.__setattr__(self, "input_hash", expected_hash)

    @property
    def reference_price(self) -> Decimal | None:
        return self.limit_price or self.price or self.trigger_price

    def semantic_mapping(self, *, include_hash: bool = True) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "source_decision_id": self.source_decision_id,
            "strategy_id": self.strategy_id,
            "setup_id": self.setup_id,
            "module_id": self.module_id,
            "module_version": self.module_version,
            "portfolio_id": self.portfolio_id,
            "symbol": self.symbol,
            "market": self.market,
            "side": self.side.value,
            "signal_time": self.signal_time,
            "available_at": self.available_at,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "entry_kind": self.entry_kind,
            "order_type": self.order_type,
            "ttl_bars": self.ttl_bars,
            "price": self.price,
            "trigger_price": self.trigger_price,
            "limit_price": self.limit_price,
            "post_only": self.post_only,
            "priority": self.priority,
            "confidence": self.confidence,
            "incompatibility_tags": self.incompatibility_tags,
            "exclusivity_group": self.exclusivity_group,
            "correlation_group": self.correlation_group,
            "reason_codes": self.reason_codes,
            "metadata": self.metadata,
        }
        if include_hash:
            result["input_hash"] = self.input_hash
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class TargetIntent:
    price: Decimal
    allocation: Decimal = ONE
    target_type: str = "limit"

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price, "target.price", positive=True))
        allocation = _decimal(self.allocation, "target.allocation", positive=True)
        if allocation > ONE:
            raise RouterInputError("target allocation cannot exceed 1")
        object.__setattr__(self, "allocation", allocation)
        object.__setattr__(self, "target_type", _text(self.target_type, "target_type"))


@dataclass(frozen=True, slots=True)
class ProtectionDecision:
    decision_id: str
    entry_candidate_id: str
    setup_id: str
    symbol: str
    side: TradeSide
    entry_price: Decimal
    stop_loss: Decimal
    targets: tuple[TargetIntent, ...]
    created_at: datetime
    expires_at: datetime | None
    risk_distance: Decimal
    risk_reward: Decimal | None = None
    max_holding_seconds: int | None = None
    no_progress_rule: Mapping[str, Any] = field(default_factory=dict)
    invalidation_rule: Mapping[str, Any] = field(default_factory=dict)
    source_decision_id: str = ""
    input_hash: str = ""
    schema_version: str = "1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("decision_id", "entry_candidate_id", "setup_id", "symbol"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.schema_version != "1":
            raise RouterInputError(
                "unsupported normalized ProtectionDecision schema", ReasonCode.UNSUPPORTED_SCHEMA
            )
        if not isinstance(self.side, TradeSide):
            raise RouterInputError("side must be TradeSide")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
            if self.expires_at <= self.created_at:
                raise RouterInputError("protection expires_at must be after created_at")
        for name in ("entry_price", "stop_loss", "risk_distance"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True))
        targets = _plain_sequence(self.targets, "targets")
        if not targets or not all(isinstance(target, TargetIntent) for target in targets):
            raise RouterInputError("targets must contain TargetIntent values")
        try:
            allocation = exact_sum((target.allocation for target in targets), ZERO)
        except DecimalDomainError as exc:
            raise RouterInputError(str(exc)) from exc
        if allocation > ONE:
            raise RouterInputError("target allocations cannot exceed 1")
        if self.risk_reward is not None:
            object.__setattr__(self, "risk_reward", _decimal(self.risk_reward, "risk_reward"))
        if self.max_holding_seconds is not None:
            object.__setattr__(
                self,
                "max_holding_seconds",
                _bounded_int(
                    self.max_holding_seconds,
                    "max_holding_seconds",
                    minimum=1,
                ),
            )
        try:
            expected_distance = exact_abs_difference(self.entry_price, self.stop_loss)
        except DecimalDomainError as exc:
            raise RouterInputError(str(exc)) from exc
        if self.risk_distance != expected_distance:
            raise RouterInputError("risk_distance must equal absolute entry/stop distance")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "no_progress_rule", _frozen_mapping(self.no_progress_rule))
        object.__setattr__(self, "invalidation_rule", _frozen_mapping(self.invalidation_rule))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        if not self.source_decision_id:
            object.__setattr__(self, "source_decision_id", self.decision_id)
        expected = sha256_hex(self.semantic_mapping(include_hash=False))
        if self.input_hash and self.input_hash != expected:
            raise RouterInputError("protection input_hash does not match canonical content")
        object.__setattr__(self, "input_hash", expected)

    def semantic_mapping(self, *, include_hash: bool = True) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "entry_candidate_id": self.entry_candidate_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "targets": self.targets,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "risk_distance": self.risk_distance,
            "risk_reward": self.risk_reward,
            "max_holding_seconds": self.max_holding_seconds,
            "no_progress_rule": self.no_progress_rule,
            "invalidation_rule": self.invalidation_rule,
            "source_decision_id": self.source_decision_id,
            "metadata": self.metadata,
        }
        if include_hash:
            result["input_hash"] = self.input_hash
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class SizingDecision:
    decision_id: str
    request_id: str
    setup_id: str
    symbol: str
    approved: bool
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal
    risk_fraction: Decimal
    leverage: Decimal
    created_at: datetime
    expires_at: datetime | None = None
    binding_constraints: tuple[str, ...] = ()
    rejection_reason: str | None = None
    entry_candidate_id: str | None = None
    protection_decision_id: str | None = None
    source_decision_id: str = ""
    input_hash: str = ""
    schema_version: str = "1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("decision_id", "request_id", "setup_id", "symbol"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.schema_version != "1":
            raise RouterInputError(
                "unsupported normalized SizingDecision schema", ReasonCode.UNSUPPORTED_SCHEMA
            )
        if not isinstance(self.approved, bool):
            raise RouterInputError("approved must be boolean")
        object.__setattr__(
            self, "rejection_reason", _optional_text(self.rejection_reason, "rejection_reason")
        )
        object.__setattr__(
            self,
            "entry_candidate_id",
            _optional_text(self.entry_candidate_id, "entry_candidate_id"),
        )
        object.__setattr__(
            self,
            "protection_decision_id",
            _optional_text(self.protection_decision_id, "protection_decision_id"),
        )
        for name in ("quantity", "notional", "risk_amount", "risk_fraction", "leverage"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.approved and self.quantity <= ZERO:
            raise RouterInputError("approved sizing requires positive quantity")
        if self.approved:
            for name in ("notional", "risk_amount", "risk_fraction", "leverage"):
                if getattr(self, name) <= ZERO:
                    raise RouterInputError(f"approved sizing requires positive {name}")
        if not self.approved and self.quantity != ZERO:
            raise RouterInputError("rejected sizing quantity must be zero")
        if self.risk_fraction > ONE:
            raise RouterInputError("risk_fraction cannot exceed 1")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
            if self.expires_at < self.created_at:
                raise RouterInputError("sizing expires_at cannot be before created_at")
        raw_constraints = _plain_sequence(self.binding_constraints, "binding_constraints")
        constraints = tuple(
            sorted(set(_text(item, "binding_constraint") for item in raw_constraints))
        )
        object.__setattr__(self, "binding_constraints", constraints)
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        if not self.source_decision_id:
            object.__setattr__(self, "source_decision_id", self.decision_id)
        expected = sha256_hex(self.semantic_mapping(include_hash=False))
        if self.input_hash and self.input_hash != expected:
            raise RouterInputError("sizing input_hash does not match canonical content")
        object.__setattr__(self, "input_hash", expected)

    def semantic_mapping(self, *, include_hash: bool = True) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "approved": self.approved,
            "quantity": self.quantity,
            "notional": self.notional,
            "risk_amount": self.risk_amount,
            "risk_fraction": self.risk_fraction,
            "leverage": self.leverage,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "binding_constraints": self.binding_constraints,
            "rejection_reason": self.rejection_reason,
            "entry_candidate_id": self.entry_candidate_id,
            "protection_decision_id": self.protection_decision_id,
            "source_decision_id": self.source_decision_id,
            "metadata": self.metadata,
        }
        if include_hash:
            result["input_hash"] = self.input_hash
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class ScalingDecision:
    decision_id: str
    position_id: str
    setup_id: str | None
    symbol: str
    side: TradeSide
    action: ScalingAction
    approved_quantity: Decimal
    desired_price: Decimal
    created_at: datetime
    order_type: str = "limit"
    trigger_price: Decimal | None = None
    direction: ScalingDirection | None = None
    risk_request_id: str | None = None
    expires_at: datetime | None = None
    stage: int = 1
    source_decision_id: str = ""
    input_hash: str = ""
    schema_version: str = "1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("decision_id", "position_id", "symbol"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.setup_id is not None:
            object.__setattr__(self, "setup_id", _text(self.setup_id, "setup_id"))
        if self.schema_version != "1":
            raise RouterInputError(
                "unsupported normalized ScalingDecision schema", ReasonCode.UNSUPPORTED_SCHEMA
            )
        if not isinstance(self.side, TradeSide):
            raise RouterInputError("side must be TradeSide")
        if not isinstance(self.action, ScalingAction):
            raise RouterInputError("action must be ScalingAction")
        order_type = _text(self.order_type, "order_type").lower()
        if order_type not in {"market", "limit", "stop_market", "stop_limit"}:
            raise RouterInputError(f"unsupported scaling order_type: {order_type}")
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(
            self,
            "trigger_price",
            _optional_decimal(self.trigger_price, "trigger_price", positive=True),
        )
        if order_type in {"stop_market", "stop_limit"} and self.trigger_price is None:
            raise RouterInputError(f"{order_type} scaling requires trigger_price")
        direction = self.direction
        if direction is None:
            direction = {
                ScalingAction.ADD: ScalingDirection.INCREASE,
                ScalingAction.REDUCE: ScalingDirection.DECREASE,
                ScalingAction.HOLD: ScalingDirection.NONE,
                ScalingAction.STOP_SCALING: ScalingDirection.NONE,
            }.get(self.action)
            if direction is None:
                raise RouterInputError("rebalance scaling requires explicit direction")
        if not isinstance(direction, ScalingDirection):
            raise RouterInputError("direction must be ScalingDirection")
        expected_direction = {
            ScalingAction.ADD: ScalingDirection.INCREASE,
            ScalingAction.REDUCE: ScalingDirection.DECREASE,
            ScalingAction.HOLD: ScalingDirection.NONE,
            ScalingAction.STOP_SCALING: ScalingDirection.NONE,
        }.get(self.action)
        if expected_direction is not None and direction is not expected_direction:
            raise RouterInputError("scaling action and direction are inconsistent")
        if self.action is ScalingAction.REBALANCE and direction is ScalingDirection.NONE:
            raise RouterInputError("rebalance scaling requires quantity-changing direction")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(
            self, "risk_request_id", _optional_text(self.risk_request_id, "risk_request_id")
        )
        if direction is ScalingDirection.INCREASE and self.risk_request_id is None:
            raise RouterInputError("increase scaling requires risk_request_id")
        if direction is not ScalingDirection.INCREASE and self.risk_request_id is not None:
            raise RouterInputError("non-increase scaling cannot reserve risk")
        object.__setattr__(
            self, "approved_quantity", _decimal(self.approved_quantity, "approved_quantity")
        )
        object.__setattr__(
            self, "desired_price", _decimal(self.desired_price, "desired_price", positive=True)
        )
        if (
            direction in {ScalingDirection.INCREASE, ScalingDirection.DECREASE}
            and self.approved_quantity <= ZERO
        ):
            raise RouterInputError("approved scaling action requires positive quantity")
        if direction is ScalingDirection.NONE and self.approved_quantity != ZERO:
            raise RouterInputError("non-quantity scaling action requires zero quantity")
        object.__setattr__(
            self,
            "stage",
            _bounded_int(self.stage, "scaling stage", minimum=1),
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
            if self.expires_at <= self.created_at:
                raise RouterInputError("scaling expires_at must be after created_at")
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        if not self.source_decision_id:
            object.__setattr__(self, "source_decision_id", self.decision_id)
        expected_hash = sha256_hex(self.semantic_mapping(include_hash=False))
        if self.input_hash and self.input_hash != expected_hash:
            raise RouterInputError("scaling input_hash does not match canonical content")
        object.__setattr__(self, "input_hash", expected_hash)

    def semantic_mapping(self, *, include_hash: bool = True) -> Mapping[str, Any]:
        direction = self.direction
        if not isinstance(direction, ScalingDirection):
            raise RouterInputError("direction must be ScalingDirection")
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "position_id": self.position_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "action": self.action.value,
            "direction": direction.value,
            "risk_request_id": self.risk_request_id,
            "approved_quantity": self.approved_quantity,
            "desired_price": self.desired_price,
            "order_type": self.order_type,
            "trigger_price": self.trigger_price,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "stage": self.stage,
            "source_decision_id": self.source_decision_id,
            "metadata": self.metadata,
        }
        if include_hash:
            result["input_hash"] = self.input_hash
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class PositionState:
    position_id: str
    symbol: str
    side: TradeSide
    quantity: Decimal
    average_entry: Decimal
    version: str
    market: str | None = None
    strategy_id: str | None = None
    setup_id: str | None = None
    initial_risk: Decimal = ZERO
    remaining_risk: Decimal = ZERO
    reserved_risk: Decimal = ZERO
    opened_at: datetime | None = None
    scaling_stage: int = 0

    def __post_init__(self) -> None:
        for name in ("position_id", "symbol", "version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.side, TradeSide):
            raise RouterInputError("side must be TradeSide")
        object.__setattr__(self, "market", _optional_text(self.market, "market"))
        object.__setattr__(self, "strategy_id", _optional_text(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "setup_id", _optional_text(self.setup_id, "setup_id"))
        for name in ("quantity", "average_entry"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True))
        for name in ("initial_risk", "remaining_risk", "reserved_risk"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.opened_at is not None:
            object.__setattr__(self, "opened_at", _utc(self.opened_at, "opened_at"))
        object.__setattr__(
            self,
            "scaling_stage",
            _bounded_int(self.scaling_stage, "scaling_stage", minimum=0),
        )


@dataclass(frozen=True, slots=True)
class PendingOrderState:
    order_intent_id: str
    plan_id: str | None
    symbol: str
    side: TradeSide
    order_type: str
    quantity: Decimal
    status: str
    version: str
    created_at: datetime
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    post_only: bool = False
    reserved_risk: Decimal = ZERO
    strategy_id: str | None = None
    setup_id: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("order_intent_id", "symbol", "order_type", "status", "version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.side, TradeSide):
            raise RouterInputError("side must be TradeSide")
        order_type = self.order_type.lower()
        status = self.status.lower()
        if order_type not in PENDING_ORDER_TYPES:
            raise RouterInputError(f"unsupported pending order type: {order_type}")
        if status not in PENDING_ORDER_STATUSES:
            raise RouterInputError(f"unsupported pending order status: {status}")
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "plan_id", _optional_text(self.plan_id, "plan_id"))
        object.__setattr__(self, "strategy_id", _optional_text(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "setup_id", _optional_text(self.setup_id, "setup_id"))
        object.__setattr__(self, "reduce_only", _boolean(self.reduce_only, "reduce_only"))
        object.__setattr__(self, "post_only", _boolean(self.post_only, "post_only"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "reserved_risk", _decimal(self.reserved_risk, "reserved_risk"))
        for name in ("price", "stop_price"):
            object.__setattr__(
                self, name, _optional_decimal(getattr(self, name), name, positive=True)
            )
        if order_type in {"limit", "stop_limit", "take_profit_limit"} and self.price is None:
            raise RouterInputError(f"{order_type} pending order requires price")
        if (
            order_type
            in {
                "stop",
                "stop_market",
                "stop_limit",
                "take_profit_market",
                "take_profit_limit",
            }
            and self.stop_price is None
        ):
            raise RouterInputError(f"{order_type} pending order requires stop_price")
        if self.post_only and order_type not in {"limit", "stop_limit", "take_profit_limit"}:
            raise RouterInputError("post_only pending order requires a limit order type")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
            if self.expires_at <= self.created_at:
                raise RouterInputError("pending order expires_at must be after created_at")

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_PENDING_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    portfolio_id: str
    version: str
    timestamp: datetime
    equity: Decimal
    available_balance: Decimal
    used_margin: Decimal
    free_margin: Decimal
    risk_already_reserved: Decimal
    available_risk_budget: Decimal
    position_state_version: str
    order_state_version: str
    risk_reservation_version: str
    positions: tuple[PositionState, ...] = ()
    pending_orders: tuple[PendingOrderState, ...] = ()
    active_plan_ids: tuple[str, ...] = ()
    instrument_locks: tuple[str, ...] = ()
    strategy_locks: tuple[str, ...] = ()
    portfolio_locked: bool = False
    daily_loss_limit_active: bool = False
    drawdown_limit_active: bool = False
    emergency_state: bool = False
    circuit_breaker_active: bool = False
    current_drawdown: Decimal = ZERO
    schema_version: str = "1"
    input_hash: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    _position_hashes: tuple[str, ...] = field(init=False, repr=False, compare=False, default=())
    _pending_order_hashes: tuple[str, ...] = field(
        init=False, repr=False, compare=False, default=()
    )

    def __post_init__(self) -> None:
        for name in (
            "portfolio_id",
            "version",
            "position_state_version",
            "order_state_version",
            "risk_reservation_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.schema_version != "1":
            raise RouterInputError(
                "unsupported PortfolioSnapshot schema", ReasonCode.UNSUPPORTED_SCHEMA
            )
        for name in (
            "portfolio_locked",
            "daily_loss_limit_active",
            "drawdown_limit_active",
            "emergency_state",
            "circuit_breaker_active",
        ):
            object.__setattr__(self, name, _boolean(getattr(self, name), name))
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        for name in (
            "equity",
            "available_balance",
            "used_margin",
            "free_margin",
            "risk_already_reserved",
            "available_risk_budget",
            "current_drawdown",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        raw_positions = _plain_sequence(self.positions, "positions")
        if not all(isinstance(item, PositionState) for item in raw_positions):
            raise RouterInputError("positions must contain PositionState values")
        raw_pending_orders = _plain_sequence(self.pending_orders, "pending_orders")
        if not all(isinstance(item, PendingOrderState) for item in raw_pending_orders):
            raise RouterInputError("pending_orders must contain PendingOrderState values")
        positions = tuple(sorted(raw_positions, key=lambda item: (item.symbol, item.position_id)))
        pending_orders = tuple(
            sorted(
                raw_pending_orders,
                key=lambda item: (item.symbol, item.order_intent_id),
            )
        )
        for position in positions:
            if position.opened_at is not None and position.opened_at > self.timestamp:
                raise RouterInputError("position opened_at cannot be after portfolio timestamp")
        for order in pending_orders:
            if order.created_at > self.timestamp:
                raise RouterInputError(
                    "pending order created_at cannot be after portfolio timestamp"
                )
        if len({item.position_id for item in positions}) != len(positions):
            raise RouterInputError("portfolio positions contain duplicate position_id")
        if len({item.order_intent_id for item in pending_orders}) != len(pending_orders):
            raise RouterInputError("pending orders contain duplicate order_intent_id")
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "pending_orders", pending_orders)
        object.__setattr__(
            self,
            "_position_hashes",
            tuple(sha256_hex(item) for item in positions),
        )
        object.__setattr__(
            self,
            "_pending_order_hashes",
            tuple(sha256_hex(item) for item in pending_orders),
        )
        for name in ("active_plan_ids", "instrument_locks", "strategy_locks"):
            raw_values = _plain_sequence(getattr(self, name), name)
            values = tuple(_text(item, name) for item in raw_values)
            object.__setattr__(self, name, tuple(sorted(set(values))))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        expected_hash = sha256_hex(self.semantic_mapping(include_hash=False))
        if self.input_hash and self.input_hash != expected_hash:
            raise RouterInputError("portfolio snapshot input_hash does not match canonical content")
        object.__setattr__(self, "input_hash", expected_hash)

    def semantic_mapping(self, *, include_hash: bool = True) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "portfolio_id": self.portfolio_id,
            "version": self.version,
            "timestamp": self.timestamp,
            "equity": self.equity,
            "available_balance": self.available_balance,
            "used_margin": self.used_margin,
            "free_margin": self.free_margin,
            "risk_already_reserved": self.risk_already_reserved,
            "available_risk_budget": self.available_risk_budget,
            "position_state_version": self.position_state_version,
            "order_state_version": self.order_state_version,
            "risk_reservation_version": self.risk_reservation_version,
            "position_hashes": self._position_hashes,
            "pending_order_hashes": self._pending_order_hashes,
            "active_plan_ids": self.active_plan_ids,
            "instrument_locks": self.instrument_locks,
            "strategy_locks": self.strategy_locks,
            "portfolio_locked": self.portfolio_locked,
            "daily_loss_limit_active": self.daily_loss_limit_active,
            "drawdown_limit_active": self.drawdown_limit_active,
            "emergency_state": self.emergency_state,
            "circuit_breaker_active": self.circuit_breaker_active,
            "current_drawdown": self.current_drawdown,
            "metadata": self.metadata,
        }
        if include_hash:
            result["input_hash"] = self.input_hash
        return MappingProxyType(result)

    def positions_for(self, symbol: str) -> tuple[PositionState, ...]:
        return tuple(item for item in self.positions if item.symbol == symbol)

    def pending_for(self, symbol: str) -> tuple[PendingOrderState, ...]:
        return tuple(item for item in self.pending_orders if item.symbol == symbol and item.active)


@dataclass(frozen=True, slots=True)
class RouterInputPackage:
    as_of: datetime
    portfolio: PortfolioSnapshot
    entries: tuple[EntryCandidate, ...]
    protections: tuple[ProtectionDecision, ...]
    sizings: tuple[SizingDecision, ...]
    scalings: tuple[ScalingDecision, ...] = ()
    correlation_id: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise RouterInputError(
                "unsupported RouterInputPackage schema", ReasonCode.UNSUPPORTED_SCHEMA
            )
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        if not isinstance(self.portfolio, PortfolioSnapshot):
            raise RouterInputError("portfolio must be PortfolioSnapshot")
        collection_types = (
            ("entries", self.entries, EntryCandidate),
            ("protections", self.protections, ProtectionDecision),
            ("sizings", self.sizings, SizingDecision),
            ("scalings", self.scalings, ScalingDecision),
        )
        snapshots: dict[str, tuple[Any, ...]] = {}
        for name, values, expected_type in collection_types:
            snapshot = _plain_sequence(values, name)
            if not all(isinstance(item, expected_type) for item in snapshot):
                raise RouterInputError(f"{name} must contain {expected_type.__name__} values")
            snapshots[name] = snapshot
        object.__setattr__(
            self,
            "entries",
            tuple(
                sorted(
                    snapshots["entries"],
                    key=lambda item: (item.candidate_id, item.input_hash),
                )
            ),
        )
        object.__setattr__(
            self,
            "protections",
            tuple(
                sorted(
                    snapshots["protections"],
                    key=lambda item: (item.decision_id, item.input_hash),
                )
            ),
        )
        object.__setattr__(
            self,
            "sizings",
            tuple(
                sorted(
                    snapshots["sizings"],
                    key=lambda item: (item.decision_id, item.input_hash),
                )
            ),
        )
        object.__setattr__(
            self,
            "scalings",
            tuple(
                sorted(
                    snapshots["scalings"],
                    key=lambda item: (item.decision_id, item.input_hash),
                )
            ),
        )
        expected_correlation_id = stable_id("corr", self.identity_mapping())
        if self.correlation_id:
            supplied_correlation_id = _text(
                self.correlation_id,
                "correlation_id",
            )
            if supplied_correlation_id != expected_correlation_id:
                raise RouterInputError("correlation_id does not match canonical package content")
        object.__setattr__(self, "correlation_id", expected_correlation_id)

    def identity_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "as_of": self.as_of,
                "portfolio_identity": {
                    "portfolio_id": self.portfolio.portfolio_id,
                    "version": self.portfolio.version,
                    "timestamp": self.portfolio.timestamp,
                    "position_state_version": self.portfolio.position_state_version,
                    "order_state_version": self.portfolio.order_state_version,
                    "risk_reservation_version": self.portfolio.risk_reservation_version,
                    "input_hash": self.portfolio.input_hash,
                    "position_count": len(self.portfolio.positions),
                    "pending_order_count": len(self.portfolio.pending_orders),
                },
                "entry_hashes": tuple(item.input_hash for item in self.entries),
                "protection_hashes": tuple(item.input_hash for item in self.protections),
                "sizing_hashes": tuple(item.input_hash for item in self.sizings),
                "scaling_hashes": tuple(item.input_hash for item in self.scalings),
            }
        )


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    candidate_id: str
    setup_id: str
    reason_codes: tuple[ReasonCode, ...]
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "setup_id", _text(self.setup_id, "setup_id"))
        if not self.reason_codes:
            raise RouterInputError("rejected candidate requires reason code")
        if not all(isinstance(item, ReasonCode) for item in self.reason_codes):
            raise RouterInputError("rejected candidate reason_codes must be ReasonCode values")
        object.__setattr__(
            self,
            "detail",
            ""
            if self.detail == ""
            else _text(
                self.detail,
                "rejected candidate detail",
                max_length=MAX_DETAIL_TEXT_LENGTH,
            ),
        )
        object.__setattr__(
            self, "reason_codes", tuple(sorted(set(self.reason_codes), key=lambda item: item.value))
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    stage: str
    outcome: str
    reason_code: ReasonCode | None = None
    candidate_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sequence",
            _bounded_int(self.sequence, "audit sequence", minimum=1),
        )
        object.__setattr__(self, "stage", _text(self.stage, "stage"))
        object.__setattr__(self, "outcome", _text(self.outcome, "outcome"))
        if self.reason_code is not None and not isinstance(self.reason_code, ReasonCode):
            raise RouterInputError("audit reason_code must be ReasonCode")
        object.__setattr__(self, "candidate_id", _optional_text(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "detail", _frozen_mapping(self.detail))


@dataclass(frozen=True, slots=True)
class ExecutionPrecondition:
    kind: str
    expected: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _text(self.kind, "precondition.kind"))
        object.__setattr__(self, "expected", _text(self.expected, "precondition.expected"))


@dataclass(frozen=True, slots=True)
class CancellationIntent:
    order_intent_id: str
    expected_order_version: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("order_intent_id", "expected_order_version", "reason"):
            object.__setattr__(self, name, _text(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class PositionIntent:
    position_id: str
    expected_position_version: str
    current_side: TradeSide
    current_quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_id", _text(self.position_id, "position_id"))
        object.__setattr__(
            self,
            "expected_position_version",
            _text(self.expected_position_version, "expected_position_version"),
        )
        if not isinstance(self.current_side, TradeSide):
            raise RouterInputError("current_side must be TradeSide")
        object.__setattr__(
            self,
            "current_quantity",
            _decimal(self.current_quantity, "current_quantity", positive=True),
        )


@dataclass(frozen=True, slots=True)
class EntryIntent:
    order_type: str
    quantity: Decimal
    reference_entry_price: Decimal
    price: Decimal | None
    trigger_price: Decimal | None
    limit_price: Decimal | None
    post_only: bool
    ttl_bars: int | None
    reduce_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_type", _text(self.order_type, "order_type").lower())
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        object.__setattr__(
            self,
            "reference_entry_price",
            _decimal(self.reference_entry_price, "reference_entry_price", positive=True),
        )
        for name in ("price", "trigger_price", "limit_price"):
            object.__setattr__(
                self, name, _optional_decimal(getattr(self, name), name, positive=True)
            )
        object.__setattr__(self, "post_only", _boolean(self.post_only, "post_only"))
        object.__setattr__(self, "reduce_only", _boolean(self.reduce_only, "reduce_only"))
        if self.ttl_bars is not None:
            object.__setattr__(
                self,
                "ttl_bars",
                _bounded_int(self.ttl_bars, "ttl_bars", minimum=1),
            )
        if self.order_type == "market":
            if any(
                value is not None for value in (self.price, self.trigger_price, self.limit_price)
            ):
                raise RouterInputError("market intent cannot carry price fields")
            if self.post_only:
                raise RouterInputError("market intent cannot be post_only")
        elif self.order_type == "limit":
            if self.limit_price is None and self.price is None:
                raise RouterInputError("limit intent requires a price")
        elif self.order_type == "stop_market":
            if self.trigger_price is None:
                raise RouterInputError("stop_market intent requires trigger_price")
        elif self.order_type == "stop_limit":
            if self.trigger_price is None or self.limit_price is None:
                raise RouterInputError("stop_limit intent requires trigger_price and limit_price")
        else:
            raise RouterInputError(f"unsupported intent order_type: {self.order_type}")


@dataclass(frozen=True, slots=True)
class ProtectionIntent:
    stop_loss: Decimal
    targets: tuple[TargetIntent, ...]
    max_holding_seconds: int | None
    no_progress_rule: Mapping[str, Any]
    invalidation_rule: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop_loss", _decimal(self.stop_loss, "stop_loss", positive=True))
        if not self.targets or not all(isinstance(item, TargetIntent) for item in self.targets):
            raise RouterInputError("protection intent requires TargetIntent values")
        object.__setattr__(self, "targets", tuple(self.targets))
        if self.max_holding_seconds is not None:
            object.__setattr__(
                self,
                "max_holding_seconds",
                _bounded_int(
                    self.max_holding_seconds,
                    "max_holding_seconds",
                    minimum=1,
                ),
            )
        object.__setattr__(self, "no_progress_rule", _frozen_mapping(self.no_progress_rule))
        object.__setattr__(self, "invalidation_rule", _frozen_mapping(self.invalidation_rule))


@dataclass(frozen=True, slots=True)
class SizingIntent:
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal
    risk_fraction: Decimal
    leverage: Decimal
    binding_constraints: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        for name in ("notional", "risk_amount", "risk_fraction", "leverage"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.risk_fraction > ONE:
            raise RouterInputError("risk_fraction cannot exceed 1")
        object.__setattr__(
            self,
            "binding_constraints",
            tuple(
                sorted(set(_text(item, "binding_constraint") for item in self.binding_constraints))
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutableTradePlan:
    plan_id: str
    content_hash: str
    audit_hash: str
    module_version: str
    portfolio_id: str
    strategy_id: str
    setup_id: str
    symbol: str
    market: str
    action: TradeAction
    side: TradeSide
    created_at: datetime
    valid_from: datetime
    expires_at: datetime
    expected_portfolio_version: str
    expected_position_version: str
    expected_order_state_version: str
    expected_risk_reservation_version: str
    selected_candidate_id: str
    position_intent: PositionIntent | None
    entry_intent: EntryIntent
    protection_intent: ProtectionIntent | None
    sizing_intent: SizingIntent
    scaling_decision_id: str | None
    cancellation_intents: tuple[CancellationIntent, ...]
    sequencing_rules: tuple[str, ...]
    preconditions: tuple[ExecutionPrecondition, ...]
    reason_codes: tuple[ReasonCode, ...]
    warnings: tuple[str, ...]
    input_hashes: tuple[str, ...]
    configuration_hash: str
    rejected_candidates: tuple[RejectedCandidate, ...]
    audit_trace: tuple[AuditEvent, ...]
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in (
            "plan_id",
            "module_version",
            "portfolio_id",
            "strategy_id",
            "setup_id",
            "symbol",
            "market",
            "expected_portfolio_version",
            "expected_position_version",
            "expected_order_state_version",
            "expected_risk_reservation_version",
            "selected_candidate_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        try:
            for name in ("content_hash", "audit_hash", "configuration_hash"):
                object.__setattr__(self, name, _sha256_text(getattr(self, name), name))
        except RouterInputError as exc:
            raise PlanInvariantError(str(exc)) from exc
        if self.schema_version != "1":
            raise PlanInvariantError("unsupported ExecutableTradePlan schema")
        if not isinstance(self.action, TradeAction):
            raise PlanInvariantError("action must be TradeAction")
        if not isinstance(self.side, TradeSide):
            raise PlanInvariantError("side must be TradeSide")
        for name in ("created_at", "valid_from", "expires_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.valid_from < self.created_at or self.expires_at <= self.valid_from:
            raise PlanInvariantError("invalid plan validity interval")
        if self.action in {TradeAction.NO_ACTION, TradeAction.REJECTED}:
            raise PlanInvariantError("executable plan cannot use non-executable action")
        if self.position_intent is not None and not isinstance(
            self.position_intent, PositionIntent
        ):
            raise PlanInvariantError("position_intent must be PositionIntent")
        position_actions = {
            TradeAction.ADD_TO_POSITION,
            TradeAction.REDUCE_POSITION,
            TradeAction.CLOSE_POSITION,
            TradeAction.REVERSE_POSITION,
        }
        if self.action in position_actions and self.position_intent is None:
            raise PlanInvariantError("position-changing plan requires explicit position_intent")
        if self.action not in position_actions and self.position_intent is not None:
            raise PlanInvariantError("new-position plan cannot target an existing position")
        if not isinstance(self.entry_intent, EntryIntent):
            raise PlanInvariantError("entry_intent must be EntryIntent")
        risk_reducing_actions = {TradeAction.REDUCE_POSITION, TradeAction.CLOSE_POSITION}
        if self.action in risk_reducing_actions:
            if self.protection_intent is not None:
                raise PlanInvariantError("risk-reducing plan cannot invent new protection")
            if not self.entry_intent.reduce_only:
                raise PlanInvariantError("risk-reducing plan requires reduce_only entry intent")
        else:
            if not isinstance(self.protection_intent, ProtectionIntent):
                raise PlanInvariantError("risk-increasing plan requires ProtectionIntent")
            if self.entry_intent.reduce_only:
                raise PlanInvariantError("risk-increasing plan cannot be reduce_only")
        if not isinstance(self.sizing_intent, SizingIntent):
            raise PlanInvariantError("sizing_intent must be SizingIntent")
        if self.action not in risk_reducing_actions:
            for name in ("notional", "risk_amount", "risk_fraction", "leverage"):
                if getattr(self.sizing_intent, name) <= ZERO:
                    raise PlanInvariantError(
                        f"risk-increasing plan requires positive sizing {name}"
                    )
        if self.entry_intent.quantity <= ZERO:
            raise PlanInvariantError("executable plan quantity must be positive")
        if self.sizing_intent.quantity != self.entry_intent.quantity:
            raise PlanInvariantError("entry and sizing quantities must match")
        if self.position_intent is not None:
            if self.action in {
                TradeAction.ADD_TO_POSITION,
                TradeAction.REDUCE_POSITION,
                TradeAction.CLOSE_POSITION,
            }:
                if self.position_intent.current_side is not self.side:
                    raise PlanInvariantError("target position side must match plan side")
            elif self.position_intent.current_side is not self.side.opposite:
                raise PlanInvariantError("reversal target must be opposite plan side")
            if self.action is TradeAction.REDUCE_POSITION and (
                self.entry_intent.quantity >= self.position_intent.current_quantity
            ):
                raise PlanInvariantError("reduce quantity must be below current position quantity")
            if self.action is TradeAction.CLOSE_POSITION and (
                self.entry_intent.quantity != self.position_intent.current_quantity
            ):
                raise PlanInvariantError("close quantity must equal current position quantity")
        reference = self.entry_intent.reference_entry_price
        if reference <= ZERO:
            raise PlanInvariantError("reference entry price must be positive")
        if self.protection_intent is not None:
            if self.side is TradeSide.LONG:
                if self.protection_intent.stop_loss >= reference:
                    raise PlanInvariantError("long stop must be below entry reference")
                if any(target.price <= reference for target in self.protection_intent.targets):
                    raise PlanInvariantError("long targets must be above entry reference")
            else:
                if self.protection_intent.stop_loss <= reference:
                    raise PlanInvariantError("short stop must be above entry reference")
                if any(target.price >= reference for target in self.protection_intent.targets):
                    raise PlanInvariantError("short targets must be below entry reference")
            try:
                total_allocation = exact_sum(
                    (target.allocation for target in self.protection_intent.targets),
                    ZERO,
                )
            except DecimalDomainError as exc:
                raise PlanInvariantError(str(exc)) from exc
            if total_allocation > ONE:
                raise PlanInvariantError("target allocations exceed 1")
        if not all(isinstance(item, CancellationIntent) for item in self.cancellation_intents):
            raise PlanInvariantError("cancellation_intents must contain CancellationIntent values")
        object.__setattr__(self, "cancellation_intents", tuple(self.cancellation_intents))
        try:
            normalized_rules = tuple(
                _text(item, "sequencing_rule") for item in self.sequencing_rules
            )
        except RouterInputError as exc:
            raise PlanInvariantError(
                "sequencing_rules must contain safe non-blank strings"
            ) from exc
        if len(normalized_rules) != len(set(normalized_rules)):
            raise PlanInvariantError("sequencing_rules cannot contain duplicates")
        object.__setattr__(self, "sequencing_rules", normalized_rules)
        if not all(isinstance(item, ExecutionPrecondition) for item in self.preconditions):
            raise PlanInvariantError("preconditions must contain ExecutionPrecondition values")
        preconditions = tuple(self.preconditions)
        precondition_map: dict[str, str] = {}
        for item in preconditions:
            if item.kind in precondition_map:
                raise PlanInvariantError(f"duplicate execution precondition kind: {item.kind}")
            precondition_map[item.kind] = item.expected
        expected_preconditions = {
            "portfolio_version": self.expected_portfolio_version,
            "position_state_version": self.expected_position_version,
            "order_state_version": self.expected_order_state_version,
            "risk_reservation_version": self.expected_risk_reservation_version,
            "configuration_hash": self.configuration_hash,
        }
        missing = sorted(set(expected_preconditions) - set(precondition_map))
        if "portfolio_content_hash" not in precondition_map:
            missing.append("portfolio_content_hash")
        if missing:
            raise PlanInvariantError(
                "missing execution preconditions: " + ", ".join(sorted(missing))
            )
        for kind, expected in expected_preconditions.items():
            if precondition_map[kind] != expected:
                raise PlanInvariantError(f"execution precondition {kind} conflicts with plan state")
        try:
            portfolio_content_hash = _sha256_text(
                precondition_map["portfolio_content_hash"],
                "portfolio_content_hash",
            )
        except RouterInputError as exc:
            raise PlanInvariantError(str(exc)) from exc
        object.__setattr__(self, "preconditions", preconditions)
        if not self.reason_codes or not all(
            isinstance(item, ReasonCode) for item in self.reason_codes
        ):
            raise PlanInvariantError("reason_codes must contain ReasonCode values")
        object.__setattr__(
            self, "reason_codes", tuple(sorted(set(self.reason_codes), key=lambda item: item.value))
        )
        try:
            warnings = tuple(_text(item, "warning") for item in self.warnings)
        except RouterInputError as exc:
            raise PlanInvariantError("warnings must contain safe non-blank strings") from exc
        object.__setattr__(self, "warnings", tuple(sorted(set(warnings))))
        try:
            hashes = tuple(
                sorted(set(_sha256_text(item, "input_hash") for item in self.input_hashes))
            )
        except RouterInputError as exc:
            raise PlanInvariantError(str(exc)) from exc
        if not hashes:
            raise PlanInvariantError("executable plan requires input hashes")
        if portfolio_content_hash not in hashes:
            raise PlanInvariantError(
                "portfolio_content_hash precondition must be present in input_hashes"
            )
        object.__setattr__(self, "input_hashes", hashes)
        if not all(isinstance(item, RejectedCandidate) for item in self.rejected_candidates):
            raise PlanInvariantError("rejected_candidates must contain RejectedCandidate values")
        rejected_candidates = tuple(
            sorted(self.rejected_candidates, key=lambda item: item.candidate_id)
        )
        rejected_ids = tuple(item.candidate_id for item in rejected_candidates)
        if len(rejected_ids) != len(set(rejected_ids)):
            raise PlanInvariantError("rejected_candidates must be unique by candidate_id")
        object.__setattr__(self, "rejected_candidates", rejected_candidates)
        if not all(isinstance(item, AuditEvent) for item in self.audit_trace):
            raise PlanInvariantError("audit_trace must contain AuditEvent values")
        audit_trace = tuple(self.audit_trace)
        if tuple(item.sequence for item in audit_trace) != tuple(range(1, len(audit_trace) + 1)):
            raise PlanInvariantError("audit_trace sequence must be contiguous and start at 1")
        object.__setattr__(self, "audit_trace", audit_trace)
        semantic = self.semantic_mapping(include_identifiers=False, include_audit=False)
        expected_hash = sha256_hex(semantic)
        expected_id = stable_id("trade-plan", semantic)
        expected_audit_hash = sha256_hex(self.audit_mapping())
        if (
            self.content_hash != expected_hash
            or self.plan_id != expected_id
            or self.audit_hash != expected_audit_hash
        ):
            raise PlanInvariantError("plan identifiers do not match canonical content")

    @classmethod
    def create(cls, **kwargs: Any) -> "ExecutableTradePlan":
        probe = dict(kwargs)
        probe["reason_codes"] = tuple(
            sorted(set(probe.get("reason_codes", ())), key=lambda item: item.value)
        )
        probe["warnings"] = tuple(sorted(set(probe.get("warnings", ()))))
        probe["input_hashes"] = tuple(sorted(set(probe.get("input_hashes", ()))))
        probe["rejected_candidates"] = tuple(
            sorted(probe.get("rejected_candidates", ()), key=lambda item: item.candidate_id)
        )
        probe["cancellation_intents"] = tuple(
            sorted(probe.get("cancellation_intents", ()), key=lambda item: item.order_intent_id)
        )
        probe["sequencing_rules"] = tuple(probe.get("sequencing_rules", ()))
        probe["preconditions"] = tuple(probe.get("preconditions", ()))
        probe["audit_trace"] = tuple(probe.get("audit_trace", ()))
        probe["plan_id"] = "pending"
        probe["content_hash"] = "pending"
        probe["audit_hash"] = "pending"
        instance = object.__new__(cls)
        for field_name in cls.__dataclass_fields__:
            if field_name in probe:
                object.__setattr__(instance, field_name, probe[field_name])
            elif field_name == "schema_version":
                object.__setattr__(instance, field_name, "1")
        semantic = instance.semantic_mapping(include_identifiers=False, include_audit=False)
        probe["content_hash"] = sha256_hex(semantic)
        probe["plan_id"] = stable_id("trade-plan", semantic)
        probe["audit_hash"] = sha256_hex(instance.audit_mapping())
        return cls(**probe)

    def audit_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "warnings": self.warnings,
                "rejected_candidates": self.rejected_candidates,
                "audit_trace": self.audit_trace,
            }
        )

    def semantic_mapping(
        self,
        *,
        include_identifiers: bool = True,
        include_audit: bool = True,
    ) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": getattr(self, "schema_version", "1"),
            "module_version": self.module_version,
            "portfolio_id": self.portfolio_id,
            "strategy_id": self.strategy_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "market": self.market,
            "action": self.action.value,
            "side": self.side.value,
            "created_at": self.created_at,
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "expected_portfolio_version": self.expected_portfolio_version,
            "expected_position_version": self.expected_position_version,
            "expected_order_state_version": self.expected_order_state_version,
            "expected_risk_reservation_version": self.expected_risk_reservation_version,
            "selected_candidate_id": self.selected_candidate_id,
            "position_intent": self.position_intent,
            "entry_intent": self.entry_intent,
            "protection_intent": self.protection_intent,
            "sizing_intent": self.sizing_intent,
            "scaling_decision_id": self.scaling_decision_id,
            "cancellation_intents": self.cancellation_intents,
            "sequencing_rules": self.sequencing_rules,
            "preconditions": self.preconditions,
            "reason_codes": self.reason_codes,
            "input_hashes": self.input_hashes,
            "configuration_hash": self.configuration_hash,
        }
        if include_audit:
            result.update(self.audit_mapping())
        if include_identifiers:
            result["plan_id"] = self.plan_id
            result["content_hash"] = self.content_hash
            result["audit_hash"] = self.audit_hash
        return MappingProxyType(result)

    def to_mapping(self) -> Mapping[str, Any]:
        value = canonical_value(self.semantic_mapping())
        if not isinstance(value, dict):
            raise PlanInvariantError("plan serialization must produce mapping")
        return value


@dataclass(frozen=True, slots=True)
class RoutingResult:
    result_id: str
    verdict: PlanVerdict
    created_at: datetime
    portfolio_id: str
    configuration_hash: str
    plan: ExecutableTradePlan | None
    reason_codes: tuple[ReasonCode, ...]
    rejected_candidates: tuple[RejectedCandidate, ...]
    audit_trace: tuple[AuditEvent, ...]
    warnings: tuple[str, ...] = ()
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        for name in ("result_id", "portfolio_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        try:
            object.__setattr__(
                self,
                "configuration_hash",
                _sha256_text(self.configuration_hash, "configuration_hash"),
            )
        except RouterInputError as exc:
            raise PlanInvariantError(str(exc)) from exc
        if self.schema_version != "1":
            raise PlanInvariantError("unsupported RoutingResult schema")
        if not isinstance(self.verdict, PlanVerdict):
            raise PlanInvariantError("verdict must be PlanVerdict")
        if self.plan is not None and not isinstance(self.plan, ExecutableTradePlan):
            raise PlanInvariantError("plan must be ExecutableTradePlan")
        if self.verdict is PlanVerdict.ACCEPTED and self.plan is None:
            raise PlanInvariantError("accepted RoutingResult requires a plan")
        if self.verdict is PlanVerdict.REJECTED and self.plan is not None:
            raise PlanInvariantError("rejected RoutingResult cannot contain a plan")
        if not self.reason_codes or not all(
            isinstance(item, ReasonCode) for item in self.reason_codes
        ):
            raise PlanInvariantError("RoutingResult requires ReasonCode values")
        object.__setattr__(
            self, "reason_codes", tuple(sorted(set(self.reason_codes), key=lambda item: item.value))
        )
        if not all(isinstance(item, RejectedCandidate) for item in self.rejected_candidates):
            raise PlanInvariantError("rejected_candidates must contain RejectedCandidate values")
        rejected_candidates = tuple(
            sorted(self.rejected_candidates, key=lambda item: item.candidate_id)
        )
        rejected_ids = tuple(item.candidate_id for item in rejected_candidates)
        if len(rejected_ids) != len(set(rejected_ids)):
            raise PlanInvariantError("rejected_candidates must be unique by candidate_id")
        object.__setattr__(self, "rejected_candidates", rejected_candidates)
        if not all(isinstance(item, AuditEvent) for item in self.audit_trace):
            raise PlanInvariantError("audit_trace must contain AuditEvent values")
        audit_trace = tuple(self.audit_trace)
        if tuple(item.sequence for item in audit_trace) != tuple(range(1, len(audit_trace) + 1)):
            raise PlanInvariantError("audit_trace sequence must be contiguous and start at 1")
        object.__setattr__(self, "audit_trace", audit_trace)
        try:
            warnings = tuple(_text(item, "warning") for item in self.warnings)
        except RouterInputError as exc:
            raise PlanInvariantError("warnings must contain safe non-blank strings") from exc
        object.__setattr__(self, "warnings", tuple(sorted(set(warnings))))
        expected = stable_id("routing", self.identity_mapping())
        if self.result_id != expected:
            raise PlanInvariantError("routing result_id does not match canonical content")

    @classmethod
    def create(cls, **kwargs: Any) -> "RoutingResult":
        probe = dict(kwargs)
        probe["reason_codes"] = tuple(
            sorted(set(probe.get("reason_codes", ())), key=lambda item: item.value)
        )
        probe["rejected_candidates"] = tuple(
            sorted(probe.get("rejected_candidates", ()), key=lambda item: item.candidate_id)
        )
        probe["audit_trace"] = tuple(probe.get("audit_trace", ()))
        probe["warnings"] = tuple(sorted(set(probe.get("warnings", ()))))
        probe["result_id"] = "pending"
        instance = object.__new__(cls)
        for field_name in cls.__dataclass_fields__:
            if field_name in probe:
                object.__setattr__(instance, field_name, probe[field_name])
            elif field_name == "schema_version":
                object.__setattr__(instance, field_name, "1")
        probe["result_id"] = stable_id("routing", instance.identity_mapping())
        return cls(**probe)

    def identity_mapping(self) -> Mapping[str, Any]:
        plan_identity = None
        if self.plan is not None:
            plan_identity = {
                "plan_id": self.plan.plan_id,
                "content_hash": self.plan.content_hash,
                "audit_hash": self.plan.audit_hash,
            }
        rejection_hash = None
        if self.plan is None:
            rejection_hash = sha256_hex(
                {
                    "reason_codes": self.reason_codes,
                    "rejected_candidates": self.rejected_candidates,
                    "audit_trace": self.audit_trace,
                    "warnings": self.warnings,
                }
            )
        return MappingProxyType(
            {
                "schema_version": getattr(self, "schema_version", "1"),
                "verdict": self.verdict.value,
                "created_at": self.created_at,
                "portfolio_id": self.portfolio_id,
                "configuration_hash": self.configuration_hash,
                "plan_identity": plan_identity,
                "rejection_hash": rejection_hash,
                "reason_codes": self.reason_codes,
            }
        )

    def semantic_mapping(self, *, include_id: bool = True) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": getattr(self, "schema_version", "1"),
            "verdict": self.verdict.value,
            "created_at": self.created_at,
            "portfolio_id": self.portfolio_id,
            "configuration_hash": self.configuration_hash,
            "plan": self.plan,
            "reason_codes": self.reason_codes,
            "rejected_candidates": self.rejected_candidates,
            "audit_trace": self.audit_trace,
            "warnings": self.warnings,
        }
        if include_id:
            result["result_id"] = self.result_id
        return MappingProxyType(result)

    def to_mapping(self) -> Mapping[str, Any]:
        value = canonical_value(self.semantic_mapping())
        if not isinstance(value, dict):
            raise PlanInvariantError("routing serialization must produce mapping")
        return value
