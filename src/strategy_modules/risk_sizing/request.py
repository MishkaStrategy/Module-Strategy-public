from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from ._decimal import ZERO, decimal_from, decimal_work_precision, freeze_value
from .adjustment import PositionAdjustment
from .enums import TradeSide
from .instrument import InstrumentRiskRules, PortfolioRiskSnapshot


def _legacy_adjustment(metadata: Mapping[str, Any]) -> PositionAdjustment | None:
    required = {"adjustment_id", "position_id", "opens_new_position"}
    if not required.issubset(metadata):
        return None
    if not ({"kind", "adjustment_kind"} & set(metadata)):
        return None
    return PositionAdjustment.from_mapping(metadata, legacy_metadata=True)


@dataclass(frozen=True, slots=True)
class RiskRequest:
    request_id: str
    symbol: str
    side: TradeSide
    entry_price: Decimal
    stop_loss: Decimal
    equity: Decimal
    instrument: InstrumentRiskRules
    portfolio: PortfolioRiskSnapshot = field(default_factory=PortfolioRiskSnapshot)
    requested_risk_fraction: Decimal | None = None
    available_margin: Decimal | None = None
    setup_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    position_adjustment: PositionAdjustment | None = None
    schema_version: str = "1"
    _calculation_precision: int = field(init=False, repr=False, compare=False)
    _prices_positive: bool = field(init=False, repr=False, compare=False)
    _price_geometry_valid: bool = field(init=False, repr=False, compare=False)
    _prices_aligned: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id is required")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol is required")
        if not isinstance(self.side, TradeSide):
            raise ValueError("side must be TradeSide")
        if not isinstance(self.instrument, InstrumentRiskRules):
            raise ValueError("instrument must be InstrumentRiskRules")
        if not isinstance(self.portfolio, PortfolioRiskSnapshot):
            raise ValueError("portfolio must be PortfolioRiskSnapshot")
        if self.setup_id is not None and (
            not isinstance(self.setup_id, str) or not self.setup_id.strip()
        ):
            raise ValueError("setup_id must be a non-blank string")
        if self.schema_version != "1":
            raise ValueError("unsupported risk request schema_version")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        if self.position_adjustment is not None and not isinstance(
            self.position_adjustment,
            PositionAdjustment,
        ):
            raise ValueError("position_adjustment must be PositionAdjustment")

        for name in ("entry_price", "stop_loss", "equity"):
            object.__setattr__(self, name, decimal_from(getattr(self, name), name))
        if self.requested_risk_fraction is not None:
            object.__setattr__(
                self,
                "requested_risk_fraction",
                decimal_from(self.requested_risk_fraction, "requested_risk_fraction"),
            )
        if self.available_margin is not None:
            margin = decimal_from(self.available_margin, "available_margin")
            if margin < ZERO:
                raise ValueError("available_margin cannot be negative")
            object.__setattr__(self, "available_margin", margin)

        legacy = _legacy_adjustment(self.metadata)
        if self.position_adjustment is None:
            object.__setattr__(self, "position_adjustment", legacy)
        elif legacy is not None and legacy != self.position_adjustment:
            raise ValueError("top-level and metadata position adjustment evidence disagree")
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

        precision_values = [
            self.entry_price,
            self.stop_loss,
            self.equity,
            self.instrument.quantity_step,
            self.instrument.min_quantity,
            self.instrument.min_notional,
            self.instrument.contract_size,
            self.portfolio.gross_notional,
            self.portfolio.symbol_notional,
            self.portfolio.open_risk_amount,
        ]
        for optional in (
            self.requested_risk_fraction,
            self.available_margin,
            self.instrument.max_quantity,
            self.instrument.max_notional,
            self.instrument.tick_size,
            self.instrument.max_leverage,
        ):
            if optional is not None:
                precision_values.append(optional)
        if self.position_adjustment is not None:
            adjustment = self.position_adjustment
            precision_values.extend(
                [
                    adjustment.current_quantity,
                    adjustment.requested_quantity,
                    adjustment.maximum_quantity,
                    adjustment.projected_quantity,
                    adjustment.current_average_price,
                    adjustment.projected_average_price,
                    adjustment.current_total_risk,
                    adjustment.projected_total_risk,
                    adjustment.current_notional,
                    adjustment.projected_notional,
                ]
            )
            if adjustment.valuation_price is not None:
                precision_values.append(adjustment.valuation_price)
        object.__setattr__(
            self,
            "_calculation_precision",
            decimal_work_precision(*precision_values),
        )
        object.__setattr__(
            self,
            "_prices_positive",
            self.entry_price > ZERO and self.stop_loss > ZERO,
        )
        geometry_valid = (
            self.stop_loss < self.entry_price
            if self.side is TradeSide.BUY
            else self.stop_loss > self.entry_price
        )
        object.__setattr__(self, "_price_geometry_valid", geometry_valid)

        tick_ratio = self.instrument.tick_size_ratio
        if tick_ratio is None:
            aligned = True
        else:
            step_num, step_den = tick_ratio
            entry_num, entry_den = self.entry_price.as_integer_ratio()
            stop_num, stop_den = self.stop_loss.as_integer_ratio()
            aligned = (entry_num * step_den) % (entry_den * step_num) == 0 and (
                stop_num * step_den
            ) % (stop_den * step_num) == 0
        object.__setattr__(self, "_prices_aligned", aligned)

    @property
    def calculation_precision(self) -> int:
        return self._calculation_precision

    @property
    def prices_positive(self) -> bool:
        return self._prices_positive

    @property
    def price_geometry_valid(self) -> bool:
        return self._price_geometry_valid

    @property
    def prices_aligned(self) -> bool:
        return self._prices_aligned

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RiskRequest":
        allowed = {
            "request_id",
            "symbol",
            "side",
            "entry_price",
            "stop_loss",
            "equity",
            "instrument",
            "portfolio",
            "requested_risk_fraction",
            "available_margin",
            "setup_id",
            "metadata",
            "position_adjustment",
            "schema_version",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown risk request fields: {', '.join(unknown)}")
        schema_version = str(payload.get("schema_version", "1"))
        if schema_version != "1":
            raise ValueError("unsupported risk request schema_version")
        side_value = payload.get("side")
        try:
            side = (
                side_value
                if isinstance(side_value, TradeSide)
                else TradeSide(str(side_value).lower())
            )
        except ValueError as exc:
            raise ValueError("side must be 'buy' or 'sell'") from exc
        instrument_payload = payload.get("instrument")
        if not isinstance(instrument_payload, Mapping):
            raise ValueError("instrument must be a mapping")
        portfolio_payload = payload.get("portfolio")
        if portfolio_payload is not None and not isinstance(
            portfolio_payload,
            Mapping,
        ):
            raise ValueError("portfolio must be a mapping")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        adjustment_payload = payload.get("position_adjustment")
        if adjustment_payload is not None and not isinstance(
            adjustment_payload,
            Mapping,
        ):
            raise ValueError("position_adjustment must be a mapping")
        return cls(
            request_id=str(payload.get("request_id", "")),
            symbol=str(payload.get("symbol", "")),
            side=side,
            entry_price=decimal_from(payload["entry_price"], "entry_price"),
            stop_loss=decimal_from(payload["stop_loss"], "stop_loss"),
            equity=decimal_from(payload["equity"], "equity"),
            instrument=InstrumentRiskRules.from_mapping(instrument_payload),
            portfolio=PortfolioRiskSnapshot.from_mapping(portfolio_payload),
            requested_risk_fraction=(
                None
                if payload.get("requested_risk_fraction") is None
                else decimal_from(
                    payload["requested_risk_fraction"],
                    "requested_risk_fraction",
                )
            ),
            available_margin=(
                None
                if payload.get("available_margin") is None
                else decimal_from(payload["available_margin"], "available_margin")
            ),
            setup_id=(None if payload.get("setup_id") is None else str(payload["setup_id"])),
            metadata=metadata,
            position_adjustment=(
                None
                if adjustment_payload is None
                else PositionAdjustment.from_mapping(adjustment_payload)
            ),
            schema_version=schema_version,
        )

    def to_mapping(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_price": str(self.entry_price),
            "stop_loss": str(self.stop_loss),
            "equity": str(self.equity),
            "instrument": self.instrument.to_mapping(),
            "portfolio": self.portfolio.to_mapping(),
            "requested_risk_fraction": (
                None if self.requested_risk_fraction is None else str(self.requested_risk_fraction)
            ),
            "available_margin": (
                None if self.available_margin is None else str(self.available_margin)
            ),
            "setup_id": self.setup_id,
            "metadata": dict(self.metadata),
        }
        if self.position_adjustment is not None:
            result["position_adjustment"] = self.position_adjustment.to_mapping()
        return result
