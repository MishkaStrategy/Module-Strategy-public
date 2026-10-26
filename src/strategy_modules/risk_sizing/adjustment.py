from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from ._decimal import ZERO, decimal_from

_ALLOWED_KINDS = frozenset(
    {
        "position_increase",
        "scale_in",
        "averaging_down",
        "pyramiding",
        "dca",
        "rebalance_increase",
    }
)


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{field_name} cannot have surrounding whitespace")
    return value


def _first(payload: Mapping[str, Any], *names: str) -> Any:
    found = [name for name in names if name in payload]
    if not found:
        raise ValueError(f"missing position adjustment field: {names[0]}")
    value = payload[found[0]]
    for name in found[1:]:
        if payload[name] != value:
            raise ValueError(f"conflicting aliases for position adjustment field: {names[0]}")
    return value


@dataclass(frozen=True, slots=True)
class PositionAdjustment:
    adjustment_id: str
    position_id: str
    kind: str
    opens_new_position: bool
    current_quantity: Decimal
    requested_quantity: Decimal
    maximum_quantity: Decimal
    projected_quantity: Decimal
    current_average_price: Decimal
    projected_average_price: Decimal
    valuation_price: Decimal | None
    current_total_risk: Decimal
    projected_total_risk: Decimal
    current_notional: Decimal
    projected_notional: Decimal
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adjustment_id",
            _required_identifier(self.adjustment_id, "adjustment_id"),
        )
        object.__setattr__(
            self,
            "position_id",
            _required_identifier(self.position_id, "position_id"),
        )
        if self.schema_version != "1":
            raise ValueError("unsupported position adjustment schema_version")
        if self.kind not in _ALLOWED_KINDS:
            raise ValueError("unsupported position adjustment kind")
        if not isinstance(self.opens_new_position, bool):
            raise ValueError("opens_new_position must be boolean")
        if self.opens_new_position:
            raise ValueError("position adjustment cannot open a new position")

        for name in (
            "current_quantity",
            "requested_quantity",
            "maximum_quantity",
            "projected_quantity",
            "current_average_price",
            "projected_average_price",
            "current_total_risk",
            "projected_total_risk",
            "current_notional",
            "projected_notional",
        ):
            value = decimal_from(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if self.valuation_price is not None:
            valuation_price = decimal_from(self.valuation_price, "valuation_price")
            if valuation_price <= ZERO:
                raise ValueError("valuation_price must be positive")
            object.__setattr__(self, "valuation_price", valuation_price)

        for name in (
            "current_quantity",
            "requested_quantity",
            "maximum_quantity",
            "projected_quantity",
            "current_average_price",
            "projected_average_price",
            "current_notional",
            "projected_notional",
        ):
            if getattr(self, name) <= ZERO:
                raise ValueError(f"{name} must be positive")
        if self.maximum_quantity < self.requested_quantity:
            raise ValueError("maximum_quantity cannot be below requested_quantity")
        if self.projected_quantity != self.current_quantity + self.requested_quantity:
            raise ValueError("projected_quantity must equal current_quantity + requested_quantity")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        legacy_metadata: bool = False,
    ) -> "PositionAdjustment":
        if not isinstance(payload, Mapping):
            raise ValueError("position_adjustment must be a mapping")
        aliases = {
            "schema_version",
            "adjustment_id",
            "position_id",
            "kind",
            "adjustment_kind",
            "opens_new_position",
            "current_quantity",
            "requested_quantity",
            "requested_delta_quantity",
            "desired_quantity",
            "maximum_quantity",
            "maximum_delta_quantity",
            "projected_quantity",
            "projected_total_quantity",
            "current_average_price",
            "current_average_entry_price",
            "projected_average_price",
            "projected_average_entry_price",
            "valuation_price",
            "current_market_price",
            "current_total_risk",
            "projected_total_risk",
            "current_notional",
            "projected_notional",
        }
        unknown = sorted(set(payload) - aliases)
        if unknown and not legacy_metadata:
            raise ValueError(f"unknown position adjustment fields: {', '.join(unknown)}")

        opens_new_position = payload.get("opens_new_position")
        if not isinstance(opens_new_position, bool):
            raise ValueError("opens_new_position must be boolean")
        if not legacy_metadata and not ({"valuation_price", "current_market_price"} & set(payload)):
            raise ValueError("missing position adjustment field: valuation_price")
        valuation_raw = payload.get(
            "valuation_price",
            payload.get("current_market_price"),
        )
        return cls(
            schema_version=str(payload.get("schema_version", "1")),
            adjustment_id=_required_identifier(
                payload.get("adjustment_id"),
                "adjustment_id",
            ),
            position_id=_required_identifier(
                payload.get("position_id"),
                "position_id",
            ),
            kind=str(_first(payload, "kind", "adjustment_kind")),
            opens_new_position=opens_new_position,
            current_quantity=decimal_from(
                _first(payload, "current_quantity"),
                "current_quantity",
            ),
            requested_quantity=decimal_from(
                _first(
                    payload,
                    "requested_quantity",
                    "requested_delta_quantity",
                    "desired_quantity",
                ),
                "requested_quantity",
            ),
            maximum_quantity=decimal_from(
                _first(payload, "maximum_quantity", "maximum_delta_quantity"),
                "maximum_quantity",
            ),
            projected_quantity=decimal_from(
                _first(payload, "projected_quantity", "projected_total_quantity"),
                "projected_quantity",
            ),
            current_average_price=decimal_from(
                _first(
                    payload,
                    "current_average_price",
                    "current_average_entry_price",
                ),
                "current_average_price",
            ),
            projected_average_price=decimal_from(
                _first(
                    payload,
                    "projected_average_price",
                    "projected_average_entry_price",
                ),
                "projected_average_price",
            ),
            valuation_price=(
                None if valuation_raw is None else decimal_from(valuation_raw, "valuation_price")
            ),
            current_total_risk=decimal_from(
                _first(payload, "current_total_risk"),
                "current_total_risk",
            ),
            projected_total_risk=decimal_from(
                _first(payload, "projected_total_risk"),
                "projected_total_risk",
            ),
            current_notional=decimal_from(
                _first(payload, "current_notional"),
                "current_notional",
            ),
            projected_notional=decimal_from(
                _first(payload, "projected_notional"),
                "projected_notional",
            ),
        )

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adjustment_id": self.adjustment_id,
            "position_id": self.position_id,
            "kind": self.kind,
            "opens_new_position": self.opens_new_position,
            "current_quantity": str(self.current_quantity),
            "requested_quantity": str(self.requested_quantity),
            "maximum_quantity": str(self.maximum_quantity),
            "projected_quantity": str(self.projected_quantity),
            "current_average_price": str(self.current_average_price),
            "projected_average_price": str(self.projected_average_price),
            "valuation_price": (
                None if self.valuation_price is None else str(self.valuation_price)
            ),
            "current_total_risk": str(self.current_total_risk),
            "projected_total_risk": str(self.projected_total_risk),
            "current_notional": str(self.current_notional),
            "projected_notional": str(self.projected_notional),
        }


@dataclass(frozen=True, slots=True)
class PositionAdjustmentDecision:
    adjustment_id: str
    position_id: str
    requested_quantity: Decimal
    approved_quantity: Decimal
    projected_total_quantity: Decimal
    projected_average_price: Decimal
    projected_notional: Decimal
    projected_leverage: Decimal
    projected_total_risk: Decimal
    opens_new_position: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adjustment_id",
            _required_identifier(self.adjustment_id, "adjustment_id"),
        )
        object.__setattr__(
            self,
            "position_id",
            _required_identifier(self.position_id, "position_id"),
        )
        if self.schema_version != "1":
            raise ValueError("unsupported position adjustment decision schema_version")
        if not isinstance(self.opens_new_position, bool) or self.opens_new_position:
            raise ValueError("position adjustment decision must describe an existing position")
        for name in (
            "requested_quantity",
            "approved_quantity",
            "projected_total_quantity",
            "projected_average_price",
            "projected_notional",
            "projected_leverage",
            "projected_total_risk",
        ):
            value = decimal_from(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if self.requested_quantity <= ZERO:
            raise ValueError("requested_quantity must be positive")
        if self.approved_quantity > self.requested_quantity:
            raise ValueError("approved_quantity cannot exceed requested_quantity")
        if self.projected_total_quantity <= ZERO:
            raise ValueError("projected_total_quantity must be positive")
        if self.projected_average_price <= ZERO:
            raise ValueError("projected_average_price must be positive")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
    ) -> "PositionAdjustmentDecision":
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
            raise ValueError("unknown position adjustment decision fields: " + ", ".join(unknown))
        opens_new_position = payload.get("opens_new_position", False)
        if not isinstance(opens_new_position, bool):
            raise ValueError("opens_new_position must be boolean")
        return cls(
            schema_version=str(payload.get("schema_version", "")),
            adjustment_id=_required_identifier(
                payload.get("adjustment_id"),
                "adjustment_id",
            ),
            position_id=_required_identifier(
                payload.get("position_id"),
                "position_id",
            ),
            requested_quantity=decimal_from(
                payload.get("requested_quantity", 0),
                "requested_quantity",
            ),
            approved_quantity=decimal_from(
                payload.get("approved_quantity", 0),
                "approved_quantity",
            ),
            projected_total_quantity=decimal_from(
                payload.get("projected_total_quantity", 0),
                "projected_total_quantity",
            ),
            projected_average_price=decimal_from(
                payload.get("projected_average_price", 0),
                "projected_average_price",
            ),
            projected_notional=decimal_from(
                payload.get("projected_notional", 0),
                "projected_notional",
            ),
            projected_leverage=decimal_from(
                payload.get("projected_leverage", 0),
                "projected_leverage",
            ),
            projected_total_risk=decimal_from(
                payload.get("projected_total_risk", 0),
                "projected_total_risk",
            ),
            opens_new_position=opens_new_position,
        )

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adjustment_id": self.adjustment_id,
            "position_id": self.position_id,
            "requested_quantity": str(self.requested_quantity),
            "approved_quantity": str(self.approved_quantity),
            "projected_total_quantity": str(self.projected_total_quantity),
            "projected_average_price": str(self.projected_average_price),
            "projected_notional": str(self.projected_notional),
            "projected_leverage": str(self.projected_leverage),
            "projected_total_risk": str(self.projected_total_risk),
            "opens_new_position": self.opens_new_position,
        }
