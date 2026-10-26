from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
import re
from types import MappingProxyType
from typing import Any, Mapping

from modular_core import (
    OrderRequest,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from modular_core.instruments import InstrumentSpec

from ._utils import canonical_json, decimal, sha256_text, utc
from .enums import FindingSeverity, IntentRole, PaperReasonCode, PlanAction
from .exceptions import PlanValidationError

SUPPORTED_PLAN_SCHEMA = "1"
MAX_IDENTIFIER_CHARS = 256
MAX_PLAN_INTENTS = 1_024
MAX_INPUT_HASHES = 1_024
MAX_INTENT_METADATA_ITEMS = 1_024
MAX_INTENT_METADATA_CHARS = 1 * 1024 * 1024
MAX_PLAN_CANONICAL_CHARS = 4 * 1024 * 1024
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _require_hash(value: str, name: str) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_identifier(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must be non-blank")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{name} exceeds maximum length")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


@dataclass(frozen=True, slots=True)
class StateVersions:
    portfolio: int
    positions: int
    orders: int

    def __post_init__(self) -> None:
        for name in ("portfolio", "positions", "orders"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} version must be int")
            if value < 0:
                raise ValueError(f"{name} version cannot be negative")


@dataclass(frozen=True, slots=True)
class VenueMetadataSnapshot:
    venue_id: str
    instrument: str
    market_type: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal | None
    min_notional: Decimal
    max_notional: Decimal | None
    contract_multiplier: Decimal
    permitted_order_types: tuple[OrderType, ...]
    permitted_time_in_force: tuple[TimeInForce, ...]
    post_only_supported: bool
    reduce_only_supported: bool
    metadata_version: str
    effective_from: datetime
    effective_to: datetime | None = None
    source_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "venue_id",
            "instrument",
            "market_type",
            "base_asset",
            "quote_asset",
            "metadata_version",
        ):
            _require_identifier(getattr(self, name), name)
        for name in ("tick_size", "quantity_step"):
            object.__setattr__(
                self,
                name,
                decimal(getattr(self, name), name, positive=True),
            )
        for name in ("min_quantity", "min_notional"):
            value = decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "contract_multiplier",
            decimal(
                self.contract_multiplier,
                "contract_multiplier",
                positive=True,
            ),
        )
        for name in ("max_quantity", "max_notional"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    decimal(value, name, positive=True),
                )
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity cannot be below min_quantity")
        if self.max_notional is not None and self.max_notional < self.min_notional:
            raise ValueError("max_notional cannot be below min_notional")
        object.__setattr__(
            self,
            "effective_from",
            utc(self.effective_from, "effective_from"),
        )
        if self.effective_to is not None:
            object.__setattr__(
                self,
                "effective_to",
                utc(self.effective_to, "effective_to"),
            )
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be after effective_from")
        try:
            order_types = tuple(
                value if isinstance(value, OrderType) else OrderType(value)
                for value in self.permitted_order_types
            )
            time_in_force = tuple(
                value if isinstance(value, TimeInForce) else TimeInForce(value)
                for value in self.permitted_time_in_force
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported venue metadata enum value") from exc
        if not order_types:
            raise ValueError("permitted_order_types cannot be empty")
        if not time_in_force:
            raise ValueError("permitted_time_in_force cannot be empty")
        if len(set(order_types)) != len(order_types):
            raise ValueError("permitted_order_types cannot contain duplicates")
        if len(set(time_in_force)) != len(time_in_force):
            raise ValueError("permitted_time_in_force cannot contain duplicates")
        object.__setattr__(self, "permitted_order_types", order_types)
        object.__setattr__(self, "permitted_time_in_force", time_in_force)
        _require_bool(self.post_only_supported, "post_only_supported")
        _require_bool(self.reduce_only_supported, "reduce_only_supported")
        _require_hash(self.source_hash, "source_hash")

    def active_at(self, timestamp: datetime) -> bool:
        moment = utc(timestamp, "metadata timestamp")
        return moment >= self.effective_from and (
            self.effective_to is None or moment < self.effective_to
        )

    def to_core_spec(self) -> InstrumentSpec:
        return InstrumentSpec(
            symbol=self.instrument,
            base_asset=self.base_asset,
            quote_asset=self.quote_asset,
            market_type=self.market_type,
            tick_size=float(self.tick_size),
            quantity_step=float(self.quantity_step),
            min_quantity=float(self.min_quantity),
            max_quantity=(float(self.max_quantity) if self.max_quantity is not None else None),
            min_notional=float(self.min_notional),
            max_notional=(float(self.max_notional) if self.max_notional is not None else None),
            contract_size=float(self.contract_multiplier),
        )


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    intent_id: str
    role: IntentRole
    side: Side | None = None
    order_type: OrderType | None = None
    quantity: Decimal | None = None
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    close_position: bool = False
    position_side: PositionSide | None = None
    target_index: int | None = None
    oco_group_id: str | None = None
    cancel_client_order_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self,
                "role",
                self.role if isinstance(self.role, IntentRole) else IntentRole(self.role),
            )
            if self.side is not None:
                object.__setattr__(
                    self,
                    "side",
                    self.side if isinstance(self.side, Side) else Side(self.side),
                )
            if self.order_type is not None:
                object.__setattr__(
                    self,
                    "order_type",
                    self.order_type
                    if isinstance(self.order_type, OrderType)
                    else OrderType(self.order_type),
                )
            object.__setattr__(
                self,
                "time_in_force",
                self.time_in_force
                if isinstance(self.time_in_force, TimeInForce)
                else TimeInForce(self.time_in_force),
            )
            if self.position_side is not None:
                object.__setattr__(
                    self,
                    "position_side",
                    self.position_side
                    if isinstance(self.position_side, PositionSide)
                    else PositionSide(self.position_side),
                )
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported execution intent enum value") from exc
        _require_identifier(self.intent_id, "intent_id")
        _require_bool(self.post_only, "post_only")
        _require_bool(self.reduce_only, "reduce_only")
        _require_bool(self.close_position, "close_position")
        if self.target_index is not None:
            if not isinstance(self.target_index, int) or isinstance(
                self.target_index,
                bool,
            ):
                raise TypeError("target_index must be int")
            if self.target_index < 0:
                raise ValueError("target_index cannot be negative")
        if self.oco_group_id is not None:
            _require_identifier(self.oco_group_id, "oco_group_id")
        if self.cancel_client_order_id is not None:
            _require_identifier(
                self.cancel_client_order_id,
                "cancel_client_order_id",
            )

        if self.role is IntentRole.CANCEL:
            if self.cancel_client_order_id is None:
                raise ValueError("cancel intent requires cancel_client_order_id")
            if (
                any(
                    value is not None
                    for value in (
                        self.side,
                        self.order_type,
                        self.quantity,
                        self.limit_price,
                        self.trigger_price,
                        self.position_side,
                        self.oco_group_id,
                    )
                )
                or self.post_only
                or self.reduce_only
                or self.close_position
            ):
                raise ValueError("cancel intent cannot contain order fields")
        else:
            if self.cancel_client_order_id is not None:
                raise ValueError("order intent cannot contain cancel_client_order_id")
            if self.side is None or self.order_type is None:
                raise ValueError("order intent requires side and order_type")
            if self.close_position:
                if not self.reduce_only:
                    raise ValueError("close_position requires reduce_only")
                if self.quantity is not None:
                    raise ValueError("close_position cannot define quantity")
            else:
                if self.quantity is None:
                    raise ValueError("order intent requires quantity")
                object.__setattr__(
                    self,
                    "quantity",
                    decimal(self.quantity, "quantity", positive=True),
                )
            for name in ("limit_price", "trigger_price"):
                value = getattr(self, name)
                if value is not None:
                    object.__setattr__(
                        self,
                        name,
                        decimal(value, name, positive=True),
                    )
            if self.order_type.requires_limit_price and self.limit_price is None:
                raise ValueError(f"{self.order_type.value} requires limit_price")
            if self.order_type.requires_trigger_price and self.trigger_price is None:
                raise ValueError(f"{self.order_type.value} requires trigger_price")
            if self.post_only and not self.order_type.requires_limit_price:
                raise ValueError("post_only requires a limit order type")
            if self.role in {IntentRole.STOP, IntentRole.TARGET} and not self.reduce_only:
                raise ValueError("protection and target intents must be reduce_only")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("intent metadata must be a mapping")
        if len(self.metadata) > MAX_INTENT_METADATA_ITEMS:
            raise ValueError("intent metadata exceeds item limit")
        metadata = dict(self.metadata)
        for key in metadata:
            _require_identifier(key, "intent metadata key")
        encoded_metadata = canonical_json(metadata)
        if len(encoded_metadata) > MAX_INTENT_METADATA_CHARS:
            raise ValueError("intent metadata exceeds canonical size limit")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "intent_id": self.intent_id,
            "role": self.role,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "trigger_price": self.trigger_price,
            "time_in_force": self.time_in_force,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
            "close_position": self.close_position,
            "position_side": self.position_side,
            "target_index": self.target_index,
            "oco_group_id": self.oco_group_id,
            "cancel_client_order_id": self.cancel_client_order_id,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ExecutableTradePlan:
    plan_id: str
    schema_version: str
    created_at: datetime
    valid_from: datetime
    expires_at: datetime
    portfolio_id: str
    instrument: str
    market: str
    action: PlanAction
    intents: tuple[ExecutionIntent, ...]
    expected_versions: StateVersions
    metadata_version: str
    configuration_hash: str
    content_hash: str
    risk_reservation_id: str | None = None
    input_hashes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self,
                "action",
                self.action if isinstance(self.action, PlanAction) else PlanAction(self.action),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported plan action") from exc
        for name in (
            "plan_id",
            "schema_version",
            "portfolio_id",
            "instrument",
            "market",
            "metadata_version",
        ):
            _require_identifier(getattr(self, name), name)
        _require_hash(self.configuration_hash, "configuration_hash")
        _require_hash(self.content_hash, "content_hash")
        if self.risk_reservation_id is not None:
            _require_identifier(
                self.risk_reservation_id,
                "risk_reservation_id",
            )
        if not isinstance(self.expected_versions, StateVersions):
            raise TypeError("expected_versions must be StateVersions")
        object.__setattr__(
            self,
            "created_at",
            utc(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "valid_from",
            utc(self.valid_from, "valid_from"),
        )
        object.__setattr__(
            self,
            "expires_at",
            utc(self.expires_at, "expires_at"),
        )
        if self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be after valid_from")
        if self.created_at > self.expires_at:
            raise ValueError("created_at cannot be after expires_at")
        intents = tuple(self.intents)
        if not intents:
            raise ValueError("plan intents cannot be empty")
        if len(intents) > MAX_PLAN_INTENTS:
            raise ValueError("plan intents exceed item limit")
        if any(not isinstance(intent, ExecutionIntent) for intent in intents):
            raise TypeError("plan intents must be ExecutionIntent values")
        if len({intent.intent_id for intent in intents}) != len(intents):
            raise ValueError("intent_id values must be unique")
        object.__setattr__(self, "intents", intents)
        if not isinstance(self.input_hashes, Mapping):
            raise TypeError("input_hashes must be a mapping")
        if len(self.input_hashes) > MAX_INPUT_HASHES:
            raise ValueError("input_hashes exceed item limit")
        input_hashes = dict(self.input_hashes)
        for name, digest in input_hashes.items():
            _require_identifier(name, "input hash name")
            _require_hash(digest, f"input_hashes[{name!r}]")
        object.__setattr__(
            self,
            "input_hashes",
            MappingProxyType(input_hashes),
        )
        self._validate_action_semantics()
        self._canonical_plan_json()

    def _validate_action_semantics(self) -> None:
        roles = tuple(intent.role for intent in self.intents)
        if self.action is PlanAction.CANCEL_ORDERS:
            if any(role is not IntentRole.CANCEL for role in roles):
                raise ValueError("cancel_orders plan may contain only cancel intents")
            return
        if self.action is PlanAction.REPLACE_ORDER:
            if (
                len(roles) != 2
                or roles[0] is not IntentRole.CANCEL
                or roles[1] is not IntentRole.REPLACEMENT
            ):
                raise ValueError(
                    "replace_order requires one cancel followed by one replacement intent"
                )
            return
        if self.action is PlanAction.REVERSE_POSITION:
            raise PlanValidationError(
                PaperReasonCode.UNSUPPORTED_PLAN_ACTION,
                "reversal sequencing is not implemented in the current paper execution checkpoint",
            )
        if any(role in {IntentRole.CANCEL, IntentRole.REPLACEMENT} for role in roles):
            raise ValueError(f"{self.action.value} cannot contain cancel or replacement intents")
        if self.action in {
            PlanAction.OPEN_POSITION,
            PlanAction.ADD_TO_POSITION,
        } and not any(role is IntentRole.ENTRY for role in roles):
            raise ValueError(f"{self.action.value} requires an entry intent")
        if self.action in {
            PlanAction.REDUCE_POSITION,
            PlanAction.CLOSE_POSITION,
        } and any(not intent.reduce_only for intent in self.intents):
            raise ValueError(f"{self.action.value} requires reduce_only intents")

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        created_at: datetime,
        valid_from: datetime,
        expires_at: datetime,
        portfolio_id: str,
        instrument: str,
        market: str,
        action: PlanAction,
        intents: tuple[ExecutionIntent, ...],
        expected_versions: StateVersions,
        metadata_version: str,
        configuration_hash: str,
        risk_reservation_id: str | None = None,
        input_hashes: Mapping[str, str] | None = None,
        schema_version: str = SUPPORTED_PLAN_SCHEMA,
    ) -> "ExecutableTradePlan":
        provisional = cls(
            plan_id=plan_id,
            schema_version=schema_version,
            created_at=created_at,
            valid_from=valid_from,
            expires_at=expires_at,
            portfolio_id=portfolio_id,
            instrument=instrument,
            market=market,
            action=action,
            intents=intents,
            expected_versions=expected_versions,
            metadata_version=metadata_version,
            configuration_hash=configuration_hash,
            content_hash="0" * 64,
            risk_reservation_id=risk_reservation_id,
            input_hashes={} if input_hashes is None else input_hashes,
        )
        return replace(
            provisional,
            content_hash=provisional.compute_content_hash(),
        )

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "portfolio_id": self.portfolio_id,
            "instrument": self.instrument,
            "market": self.market,
            "action": self.action,
            "intents": [intent.canonical_payload() for intent in self.intents],
            "expected_versions": {
                "portfolio": self.expected_versions.portfolio,
                "positions": self.expected_versions.positions,
                "orders": self.expected_versions.orders,
            },
            "metadata_version": self.metadata_version,
            "configuration_hash": self.configuration_hash,
            "risk_reservation_id": self.risk_reservation_id,
            "input_hashes": self.input_hashes,
        }

    def _canonical_plan_json(self) -> str:
        encoded = canonical_json(self.canonical_payload())
        if len(encoded) > MAX_PLAN_CANONICAL_CHARS:
            raise ValueError("plan exceeds canonical size limit")
        return encoded

    def compute_content_hash(self) -> str:
        return sha256_text(self._canonical_plan_json())

    def validate(self, now: datetime, versions: StateVersions) -> None:
        moment = utc(now, "now")
        if self.schema_version != SUPPORTED_PLAN_SCHEMA:
            raise PlanValidationError(
                PaperReasonCode.UNSUPPORTED_PLAN_SCHEMA,
                f"unsupported plan schema: {self.schema_version}",
            )
        if self.compute_content_hash() != self.content_hash:
            raise PlanValidationError(
                PaperReasonCode.PLAN_HASH_MISMATCH,
                "plan content hash mismatch",
            )
        if moment < self.valid_from:
            raise PlanValidationError(
                PaperReasonCode.PLAN_NOT_YET_VALID,
                "plan is not valid yet",
                retryable=True,
            )
        if moment >= self.expires_at:
            raise PlanValidationError(
                PaperReasonCode.STALE_PLAN,
                "plan has expired",
            )
        if versions != self.expected_versions:
            raise PlanValidationError(
                PaperReasonCode.STATE_VERSION_MISMATCH,
                f"expected state versions {self.expected_versions!r}, got {versions!r}",
                retryable=True,
            )


@dataclass(frozen=True, slots=True)
class NormalizationAudit:
    intent_id: str
    requested_quantity: Decimal | None
    normalized_quantity: Decimal | None
    requested_price: Decimal | None
    normalized_price: Decimal | None
    notional: Decimal | None
    metadata_version: str
    rounding_mode: str


@dataclass(frozen=True, slots=True)
class NormalizedOrder:
    request: OrderRequest
    audit: NormalizationAudit
    client_order_id: str
    role: IntentRole
    oco_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    plan_id: str
    content_hash: str
    accepted: bool
    terminal: bool
    client_order_ids: tuple[str, ...]
    reason_code: PaperReasonCode | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    category: str
    severity: FindingSeverity
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]
    evidence: Mapping[str, Any]
    automatic_resolution_eligible: bool
    recommended_action: str
    blocking: bool
