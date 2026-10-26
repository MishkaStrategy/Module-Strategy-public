from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from ._decimal import ZERO, decimal_from, freeze_value
from .adjustment import PositionAdjustmentDecision
from .enums import RiskDecisionStatus, RiskRejectReason


@dataclass(frozen=True, slots=True)
class RiskDecision:
    request_id: str
    symbol: str
    status: RiskDecisionStatus
    quantity: Decimal = ZERO
    raw_quantity: Decimal = ZERO
    risk_fraction: Decimal = ZERO
    risk_budget: Decimal = ZERO
    actual_risk_amount: Decimal = ZERO
    actual_risk_fraction: Decimal = ZERO
    notional: Decimal = ZERO
    required_leverage: Decimal = ZERO
    binding_constraints: tuple[str, ...] = ()
    reject_reason: RiskRejectReason | None = None
    message: str = ""
    setup_id: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    position_adjustment: PositionAdjustmentDecision | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("decision request_id is required")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("decision symbol is required")
        if not isinstance(self.status, RiskDecisionStatus):
            raise ValueError("status must be RiskDecisionStatus")
        if self.reject_reason is not None and not isinstance(
            self.reject_reason,
            RiskRejectReason,
        ):
            raise ValueError("reject_reason must be RiskRejectReason")
        if self.schema_version != "1":
            raise ValueError("unsupported risk decision schema_version")
        if self.setup_id is not None and (
            not isinstance(self.setup_id, str) or not self.setup_id.strip()
        ):
            raise ValueError("decision setup_id must be a non-blank string")
        if self.position_adjustment is not None and not isinstance(
            self.position_adjustment,
            PositionAdjustmentDecision,
        ):
            raise ValueError("position_adjustment must be PositionAdjustmentDecision")
        for name in (
            "quantity",
            "raw_quantity",
            "risk_fraction",
            "risk_budget",
            "actual_risk_amount",
            "actual_risk_fraction",
            "notional",
            "required_leverage",
        ):
            value = decimal_from(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if self.status is RiskDecisionStatus.APPROVED and self.quantity <= ZERO:
            raise ValueError("approved decision requires positive quantity")
        if (
            self.status is RiskDecisionStatus.APPROVED
            and self.actual_risk_amount > self.risk_budget
        ):
            raise ValueError("approved decision actual risk cannot exceed risk budget")
        if self.status is RiskDecisionStatus.APPROVED and self.reject_reason is not None:
            raise ValueError("approved decision cannot have reject_reason")
        if self.status is RiskDecisionStatus.REJECTED and self.reject_reason is None:
            raise ValueError("rejected decision requires reject_reason")
        if self.status is RiskDecisionStatus.REJECTED and self.quantity != ZERO:
            raise ValueError("rejected decision quantity must be zero")
        if not isinstance(self.binding_constraints, tuple):
            raise ValueError("binding_constraints must be a tuple")
        if any(not isinstance(item, str) or not item for item in self.binding_constraints):
            raise ValueError("binding_constraints must contain non-empty strings")
        if len(self.binding_constraints) != len(set(self.binding_constraints)):
            raise ValueError("binding_constraints must be unique")
        object.__setattr__(self, "diagnostics", freeze_value(self.diagnostics))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RiskDecision":
        allowed = {
            "schema_version",
            "request_id",
            "symbol",
            "status",
            "approved",
            "quantity",
            "raw_quantity",
            "risk_fraction",
            "risk_budget",
            "actual_risk_amount",
            "actual_risk_fraction",
            "notional",
            "required_leverage",
            "binding_constraints",
            "reject_reason",
            "message",
            "setup_id",
            "diagnostics",
            "position_adjustment",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown risk decision fields: {', '.join(unknown)}")
        try:
            status = RiskDecisionStatus(str(payload["status"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid risk decision status") from exc
        approved = payload.get("approved")
        if not isinstance(approved, bool) or approved is not (
            status is RiskDecisionStatus.APPROVED
        ):
            raise ValueError("risk decision approved flag disagrees with status")
        reason_value = payload.get("reject_reason")
        try:
            reason = None if reason_value is None else RiskRejectReason(str(reason_value))
        except ValueError as exc:
            raise ValueError("invalid risk decision reject_reason") from exc
        constraints = payload.get("binding_constraints", ())
        if isinstance(constraints, (str, bytes)) or not isinstance(
            constraints,
            (list, tuple),
        ):
            raise ValueError("binding_constraints must be an array")
        diagnostics = payload.get("diagnostics", {})
        if not isinstance(diagnostics, Mapping):
            raise ValueError("diagnostics must be a mapping")
        adjustment_payload = payload.get("position_adjustment")
        if adjustment_payload is not None and not isinstance(
            adjustment_payload,
            Mapping,
        ):
            raise ValueError("position_adjustment must be a mapping")
        return cls(
            request_id=str(payload.get("request_id", "")),
            symbol=str(payload.get("symbol", "")),
            status=status,
            quantity=decimal_from(payload.get("quantity", 0), "quantity"),
            raw_quantity=decimal_from(
                payload.get("raw_quantity", 0),
                "raw_quantity",
            ),
            risk_fraction=decimal_from(
                payload.get("risk_fraction", 0),
                "risk_fraction",
            ),
            risk_budget=decimal_from(
                payload.get("risk_budget", 0),
                "risk_budget",
            ),
            actual_risk_amount=decimal_from(
                payload.get("actual_risk_amount", 0),
                "actual_risk_amount",
            ),
            actual_risk_fraction=decimal_from(
                payload.get("actual_risk_fraction", 0),
                "actual_risk_fraction",
            ),
            notional=decimal_from(payload.get("notional", 0), "notional"),
            required_leverage=decimal_from(
                payload.get("required_leverage", 0),
                "required_leverage",
            ),
            binding_constraints=tuple(str(item) for item in constraints),
            reject_reason=reason,
            message=str(payload.get("message", "")),
            setup_id=(None if payload.get("setup_id") is None else str(payload["setup_id"])),
            diagnostics=diagnostics,
            position_adjustment=(
                None
                if adjustment_payload is None
                else PositionAdjustmentDecision.from_mapping(adjustment_payload)
            ),
            schema_version=str(payload.get("schema_version", "")),
        )

    @classmethod
    def _from_calculation(
        cls,
        *,
        request_id: str,
        symbol: str,
        status: RiskDecisionStatus,
        quantity: Decimal = ZERO,
        raw_quantity: Decimal = ZERO,
        risk_fraction: Decimal = ZERO,
        risk_budget: Decimal = ZERO,
        actual_risk_amount: Decimal = ZERO,
        actual_risk_fraction: Decimal = ZERO,
        notional: Decimal = ZERO,
        required_leverage: Decimal = ZERO,
        binding_constraints: tuple[str, ...] = (),
        reject_reason: RiskRejectReason | None = None,
        message: str = "",
        setup_id: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
        position_adjustment: PositionAdjustmentDecision | None = None,
    ) -> "RiskDecision":
        if status is RiskDecisionStatus.APPROVED:
            if quantity <= ZERO or actual_risk_amount > risk_budget or reject_reason is not None:
                raise ValueError("invalid approved calculated decision")
        elif status is RiskDecisionStatus.REJECTED:
            if reject_reason is None or quantity != ZERO:
                raise ValueError("invalid rejected calculated decision")
        else:
            raise ValueError("invalid calculated decision status")

        result = object.__new__(cls)
        object.__setattr__(result, "request_id", request_id)
        object.__setattr__(result, "symbol", symbol)
        object.__setattr__(result, "status", status)
        object.__setattr__(result, "quantity", quantity)
        object.__setattr__(result, "raw_quantity", raw_quantity)
        object.__setattr__(result, "risk_fraction", risk_fraction)
        object.__setattr__(result, "risk_budget", risk_budget)
        object.__setattr__(result, "actual_risk_amount", actual_risk_amount)
        object.__setattr__(result, "actual_risk_fraction", actual_risk_fraction)
        object.__setattr__(result, "notional", notional)
        object.__setattr__(result, "required_leverage", required_leverage)
        object.__setattr__(result, "binding_constraints", binding_constraints)
        object.__setattr__(result, "reject_reason", reject_reason)
        object.__setattr__(result, "message", message)
        object.__setattr__(result, "setup_id", setup_id)
        object.__setattr__(
            result,
            "diagnostics",
            MappingProxyType(dict(diagnostics or {})),
        )
        object.__setattr__(result, "position_adjustment", position_adjustment)
        object.__setattr__(result, "schema_version", "1")
        return result

    @property
    def approved(self) -> bool:
        return self.status is RiskDecisionStatus.APPROVED

    def to_mapping(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "symbol": self.symbol,
            "status": self.status.value,
            "approved": self.approved,
            "quantity": str(self.quantity),
            "raw_quantity": str(self.raw_quantity),
            "risk_fraction": str(self.risk_fraction),
            "risk_budget": str(self.risk_budget),
            "actual_risk_amount": str(self.actual_risk_amount),
            "actual_risk_fraction": str(self.actual_risk_fraction),
            "notional": str(self.notional),
            "required_leverage": str(self.required_leverage),
            "binding_constraints": self.binding_constraints,
            "reject_reason": (None if self.reject_reason is None else self.reject_reason.value),
            "message": self.message,
            "setup_id": self.setup_id,
            "diagnostics": dict(self.diagnostics),
        }
        if self.position_adjustment is not None:
            result["position_adjustment"] = self.position_adjustment.to_mapping()
        return result
