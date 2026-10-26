from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from ._decimal import ONE, ZERO, decimal_from


@dataclass(frozen=True, slots=True)
class InstrumentRiskRules:
    quantity_step: Decimal
    min_quantity: Decimal = ZERO
    max_quantity: Decimal | None = None
    min_notional: Decimal = ZERO
    max_notional: Decimal | None = None
    tick_size: Decimal | None = None
    contract_size: Decimal = ONE
    max_leverage: Decimal | None = None
    _quantity_step_ratio: tuple[int, int] = field(init=False, repr=False, compare=False)
    _tick_size_ratio: tuple[int, int] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("quantity_step", "min_quantity", "min_notional", "contract_size"):
            object.__setattr__(self, name, decimal_from(getattr(self, name), name))
        for name in ("max_quantity", "max_notional", "tick_size", "max_leverage"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_from(value, name))
        if self.quantity_step <= ZERO:
            raise ValueError("quantity_step must be positive")
        if self.min_quantity < ZERO or self.min_notional < ZERO:
            raise ValueError("minimum instrument limits cannot be negative")
        if self.contract_size <= ZERO:
            raise ValueError("contract_size must be positive")
        if self.max_quantity is not None and self.max_quantity <= ZERO:
            raise ValueError("max_quantity must be positive")
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity cannot be below min_quantity")
        if self.max_notional is not None and self.max_notional <= ZERO:
            raise ValueError("max_notional must be positive")
        if self.max_notional is not None and self.max_notional < self.min_notional:
            raise ValueError("max_notional cannot be below min_notional")
        if self.tick_size is not None and self.tick_size <= ZERO:
            raise ValueError("tick_size must be positive")
        if self.max_leverage is not None and self.max_leverage <= ZERO:
            raise ValueError("max_leverage must be positive")
        object.__setattr__(self, "_quantity_step_ratio", self.quantity_step.as_integer_ratio())
        object.__setattr__(
            self,
            "_tick_size_ratio",
            None if self.tick_size is None else self.tick_size.as_integer_ratio(),
        )

    @property
    def quantity_step_ratio(self) -> tuple[int, int]:
        return self._quantity_step_ratio

    @property
    def tick_size_ratio(self) -> tuple[int, int] | None:
        return self._tick_size_ratio

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "InstrumentRiskRules":
        allowed = {
            "quantity_step",
            "min_quantity",
            "max_quantity",
            "min_notional",
            "max_notional",
            "tick_size",
            "contract_size",
            "max_leverage",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown instrument fields: {', '.join(unknown)}")
        return cls(
            quantity_step=decimal_from(payload["quantity_step"], "quantity_step"),
            min_quantity=decimal_from(payload.get("min_quantity", 0), "min_quantity"),
            max_quantity=(
                None
                if payload.get("max_quantity") is None
                else decimal_from(payload["max_quantity"], "max_quantity")
            ),
            min_notional=decimal_from(payload.get("min_notional", 0), "min_notional"),
            max_notional=(
                None
                if payload.get("max_notional") is None
                else decimal_from(payload["max_notional"], "max_notional")
            ),
            tick_size=(
                None
                if payload.get("tick_size") is None
                else decimal_from(payload["tick_size"], "tick_size")
            ),
            contract_size=decimal_from(payload.get("contract_size", 1), "contract_size"),
            max_leverage=(
                None
                if payload.get("max_leverage") is None
                else decimal_from(payload["max_leverage"], "max_leverage")
            ),
        )

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "quantity_step": str(self.quantity_step),
            "min_quantity": str(self.min_quantity),
            "max_quantity": None if self.max_quantity is None else str(self.max_quantity),
            "min_notional": str(self.min_notional),
            "max_notional": None if self.max_notional is None else str(self.max_notional),
            "tick_size": None if self.tick_size is None else str(self.tick_size),
            "contract_size": str(self.contract_size),
            "max_leverage": None if self.max_leverage is None else str(self.max_leverage),
        }


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    gross_notional: Decimal = ZERO
    symbol_notional: Decimal = ZERO
    open_risk_amount: Decimal = ZERO
    open_positions: int = 0

    def __post_init__(self) -> None:
        for name in ("gross_notional", "symbol_notional", "open_risk_amount"):
            value = decimal_from(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if (
            isinstance(self.open_positions, bool)
            or not isinstance(self.open_positions, int)
            or self.open_positions < 0
        ):
            raise ValueError("open_positions must be a non-negative integer")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "PortfolioRiskSnapshot":
        payload = payload or {}
        allowed = {
            "gross_notional",
            "symbol_notional",
            "open_risk_amount",
            "open_positions",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown portfolio fields: {', '.join(unknown)}")
        open_positions = payload.get("open_positions", 0)
        if isinstance(open_positions, bool) or not isinstance(open_positions, int):
            raise ValueError("open_positions must be a non-negative integer")
        return cls(
            gross_notional=decimal_from(payload.get("gross_notional", 0), "gross_notional"),
            symbol_notional=decimal_from(payload.get("symbol_notional", 0), "symbol_notional"),
            open_risk_amount=decimal_from(payload.get("open_risk_amount", 0), "open_risk_amount"),
            open_positions=open_positions,
        )

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "gross_notional": str(self.gross_notional),
            "symbol_notional": str(self.symbol_notional),
            "open_risk_amount": str(self.open_risk_amount),
            "open_positions": self.open_positions,
        }
