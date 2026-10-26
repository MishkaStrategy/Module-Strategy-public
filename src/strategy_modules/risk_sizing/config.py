from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .models import ONE, ZERO, decimal_from


@dataclass(frozen=True, slots=True)
class RiskSizingConfig:
    default_risk_fraction: Decimal = Decimal("0.005")
    min_risk_fraction: Decimal = Decimal("0.0001")
    max_risk_fraction: Decimal = Decimal("0.02")
    max_trade_notional_multiple: Decimal = Decimal("3")
    max_portfolio_notional_multiple: Decimal = Decimal("5")
    max_symbol_notional_multiple: Decimal = Decimal("3")
    max_portfolio_risk_fraction: Decimal = Decimal("0.03")
    max_leverage: Decimal = Decimal("3")
    max_positions: int = 10
    allow_risk_reduction: bool = True
    require_price_alignment: bool = True
    max_processed_requests: int = 2048

    def __post_init__(self) -> None:
        for name in (
            "default_risk_fraction",
            "min_risk_fraction",
            "max_risk_fraction",
            "max_trade_notional_multiple",
            "max_portfolio_notional_multiple",
            "max_symbol_notional_multiple",
            "max_portfolio_risk_fraction",
            "max_leverage",
        ):
            object.__setattr__(self, name, decimal_from(getattr(self, name), name))
        if (
            not ZERO
            < self.min_risk_fraction
            <= self.default_risk_fraction
            <= self.max_risk_fraction
            <= ONE
        ):
            raise ValueError("risk fractions must satisfy 0 < min <= default <= max <= 1")
        if self.max_portfolio_risk_fraction > ONE:
            raise ValueError("max_portfolio_risk_fraction must be <= 1")
        if not isinstance(self.allow_risk_reduction, bool):
            raise ValueError("allow_risk_reduction must be boolean")
        if not isinstance(self.require_price_alignment, bool):
            raise ValueError("require_price_alignment must be boolean")
        for name in (
            "max_trade_notional_multiple",
            "max_portfolio_notional_multiple",
            "max_symbol_notional_multiple",
            "max_portfolio_risk_fraction",
            "max_leverage",
        ):
            if getattr(self, name) <= ZERO:
                raise ValueError(f"{name} must be positive")
        if self.max_trade_notional_multiple > self.max_portfolio_notional_multiple:
            raise ValueError("trade notional multiple cannot exceed portfolio notional multiple")
        if self.max_symbol_notional_multiple > self.max_portfolio_notional_multiple:
            raise ValueError("symbol notional multiple cannot exceed portfolio notional multiple")
        if (
            isinstance(self.max_positions, bool)
            or not isinstance(self.max_positions, int)
            or self.max_positions < 1
        ):
            raise ValueError("max_positions must be an integer >= 1")
        if (
            isinstance(self.max_processed_requests, bool)
            or not isinstance(self.max_processed_requests, int)
            or self.max_processed_requests < 1
        ):
            raise ValueError("max_processed_requests must be an integer >= 1")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RiskSizingConfig":
        unknown = sorted(
            set(values)
            - {
                "default_risk_fraction",
                "min_risk_fraction",
                "max_risk_fraction",
                "max_trade_notional_multiple",
                "max_portfolio_notional_multiple",
                "max_symbol_notional_multiple",
                "max_portfolio_risk_fraction",
                "max_leverage",
                "max_positions",
                "allow_risk_reduction",
                "require_price_alignment",
                "max_processed_requests",
            }
        )
        if unknown:
            raise ValueError(f"unknown risk sizing parameters: {', '.join(unknown)}")
        kwargs = dict(values)
        for name in ("allow_risk_reduction", "require_price_alignment"):
            if name in kwargs and not isinstance(kwargs[name], bool):
                raise ValueError(f"{name} must be boolean")
        for name in ("max_positions", "max_processed_requests"):
            if name in kwargs and (
                isinstance(kwargs[name], bool) or not isinstance(kwargs[name], int)
            ):
                raise ValueError(f"{name} must be an integer")
        for name in (
            "default_risk_fraction",
            "min_risk_fraction",
            "max_risk_fraction",
            "max_trade_notional_multiple",
            "max_portfolio_notional_multiple",
            "max_symbol_notional_multiple",
            "max_portfolio_risk_fraction",
            "max_leverage",
        ):
            if name in kwargs:
                kwargs[name] = decimal_from(kwargs[name], name)
        return cls(**kwargs)
