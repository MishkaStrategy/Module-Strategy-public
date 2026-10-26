from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from .config import RiskConfig
from .models import OrderRequest
from .validation import nonnegative_number, positive_number


class RiskError(ValueError):
    pass


class FixedFractionalSizer:
    def __init__(self, config: RiskConfig) -> None:
        if not isinstance(config, RiskConfig):
            raise TypeError("config must be RiskConfig")
        self.config = config

    def size(
        self,
        request: OrderRequest,
        *,
        equity: float,
        reference_price: float,
        contract_size: float = 1.0,
        risk_per_unit: float | None = None,
    ) -> float:
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be OrderRequest")
        try:
            equity = positive_number(equity, "equity")
            reference_price = positive_number(reference_price, "reference_price")
            contract_size = positive_number(contract_size, "contract_size")
            risk_per_unit = (
                positive_number(risk_per_unit, "risk_per_unit")
                if risk_per_unit is not None
                else None
            )
            max_notional_multiple = positive_number(
                self.config.max_notional_multiple, "max_notional_multiple"
            )
            min_quantity = nonnegative_number(self.config.min_quantity, "min_quantity")
            min_notional = nonnegative_number(self.config.min_notional, "min_notional")
            step = nonnegative_number(self.config.quantity_step, "quantity_step")
        except (TypeError, ValueError) as exc:
            raise RiskError(str(exc)) from exc

        if request.quantity is not None:
            try:
                quantity = positive_number(request.quantity, "quantity")
            except (TypeError, ValueError) as exc:
                raise RiskError(str(exc)) from exc
        else:
            if request.stop_loss is None:
                raise RiskError("quantity or stop_loss is required for risk-based sizing")
            try:
                stop_loss = positive_number(request.stop_loss, "stop_loss")
                risk_fraction = positive_number(
                    request.risk_fraction
                    if request.risk_fraction is not None
                    else self.config.default_risk_fraction,
                    "risk_fraction",
                )
                max_risk_fraction = positive_number(
                    self.config.max_risk_fraction, "max_risk_fraction"
                )
                min_stop_distance_pct = nonnegative_number(
                    self.config.min_stop_distance_pct,
                    "min_stop_distance_pct",
                )
                max_stop_distance_pct = (
                    positive_number(
                        self.config.max_stop_distance_pct,
                        "max_stop_distance_pct",
                    )
                    if self.config.max_stop_distance_pct is not None
                    else None
                )
            except (TypeError, ValueError) as exc:
                raise RiskError(str(exc)) from exc
            if risk_fraction > max_risk_fraction:
                raise RiskError("risk_fraction exceeds configured maximum")
            distance = abs(reference_price - stop_loss)
            if distance <= 0:
                raise RiskError("stop distance must be positive")
            distance_pct = distance / reference_price
            if distance_pct < min_stop_distance_pct:
                raise RiskError("stop distance below configured minimum")
            if max_stop_distance_pct is not None and distance_pct > max_stop_distance_pct:
                raise RiskError("stop distance above configured maximum")
            risk_amount = equity * risk_fraction
            if risk_per_unit is None:
                quantity = risk_amount / (distance * contract_size)
            else:
                quantity = risk_amount / risk_per_unit
                quantity /= contract_size

        max_notional = equity * max_notional_multiple
        quantity = min(quantity, max_notional / (reference_price * contract_size))

        if step > 0:
            quantity = _floor_to_step(quantity, step)

        notional = quantity * reference_price * contract_size
        if quantity <= 0 or quantity < min_quantity:
            raise RiskError("quantity below minimum after constraints")
        if notional < min_notional:
            raise RiskError("notional below minimum after constraints")
        return quantity


def _floor_to_step(value: float, step: float) -> float:
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    units = (d_value / d_step).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * d_step)
