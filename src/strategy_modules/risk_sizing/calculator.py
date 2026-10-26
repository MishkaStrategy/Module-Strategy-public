from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

from .config import RiskSizingConfig
from .models import (
    ZERO,
    RiskDecision,
    RiskDecisionStatus,
    RiskRejectReason,
    RiskRequest,
    TradeSide,
    decimal_work_precision,
)


def _floor_to_step(
    value: Decimal,
    step: Decimal,
    *,
    precision: int | None = None,
    step_ratio: tuple[int, int] | None = None,
) -> Decimal:
    """Floor ``value`` to an arbitrary positive Decimal step without float conversion."""
    if value <= ZERO:
        return ZERO
    value_num, value_den = value.as_integer_ratio()
    step_num, step_den = step_ratio if step_ratio is not None else step.as_integer_ratio()
    units = (value_num * step_den) // (value_den * step_num)
    if precision is None:
        with localcontext() as context:
            context.prec = decimal_work_precision(value, step, Decimal(units))
            return Decimal(units) * step
    return Decimal(units) * step


def _minimum(
    values: Sequence[tuple[str, Decimal]],
) -> tuple[Decimal, tuple[str, ...]]:
    first_name, minimum = values[0]
    names = [first_name]
    for name, value in values[1:]:
        if value < minimum:
            minimum = value
            names = [name]
        elif value == minimum:
            names.append(name)
    return minimum, tuple(names)


class RiskCalculator:
    """Pure, deterministic fixed-fractional position sizing calculator."""

    def __init__(self, config: RiskSizingConfig | None = None) -> None:
        self.config = config or RiskSizingConfig()
        self._config_precision = decimal_work_precision(
            self.config.default_risk_fraction,
            self.config.min_risk_fraction,
            self.config.max_risk_fraction,
            self.config.max_trade_notional_multiple,
            self.config.max_portfolio_notional_multiple,
            self.config.max_symbol_notional_multiple,
            self.config.max_portfolio_risk_fraction,
            self.config.max_leverage,
        )

    def evaluate(self, request: RiskRequest) -> RiskDecision:
        rejected = self._validate_request(request)
        if rejected is not None:
            return rejected

        config = self.config
        risk_fraction = (
            request.requested_risk_fraction
            if request.requested_risk_fraction is not None
            else config.default_risk_fraction
        )
        if not config.min_risk_fraction <= risk_fraction <= config.max_risk_fraction:
            return self._reject(
                request,
                RiskRejectReason.RISK_FRACTION_OUT_OF_RANGE,
                f"risk fraction {risk_fraction} is outside configured bounds",
                risk_fraction=risk_fraction,
            )
        if request.position_adjustment is not None:
            from .adjustment_calculator import evaluate_position_adjustment

            return evaluate_position_adjustment(self, request, risk_fraction)
        if request.portfolio.open_positions >= config.max_positions:
            return self._reject(
                request,
                RiskRejectReason.MAX_POSITIONS_REACHED,
                "maximum number of open positions reached",
                risk_fraction=risk_fraction,
            )

        instrument = request.instrument
        portfolio = request.portfolio
        margin_base = (
            request.available_margin if request.available_margin is not None else request.equity
        )
        leverage_limit = min(
            config.max_leverage,
            instrument.max_leverage if instrument.max_leverage is not None else config.max_leverage,
        )

        with localcontext() as context:
            context.prec = max(
                request.calculation_precision,
                self._config_precision,
            )
            stop_distance = abs(request.entry_price - request.stop_loss)
            risk_per_quantity = stop_distance * instrument.contract_size
            requested_risk_budget = request.equity * risk_fraction
            remaining_risk = (
                request.equity * config.max_portfolio_risk_fraction - portfolio.open_risk_amount
            )
            if remaining_risk <= ZERO:
                return self._reject(
                    request,
                    RiskRejectReason.NO_RISK_CAPACITY,
                    "portfolio risk capacity is exhausted",
                    risk_fraction=risk_fraction,
                    diagnostics={"remaining_risk": str(remaining_risk)},
                )
            if not config.allow_risk_reduction and remaining_risk < requested_risk_budget:
                return self._reject(
                    request,
                    RiskRejectReason.NO_RISK_CAPACITY,
                    "requested risk exceeds remaining portfolio risk capacity",
                    risk_fraction=risk_fraction,
                    diagnostics={"remaining_risk": str(remaining_risk)},
                )
            effective_risk_budget = min(requested_risk_budget, remaining_risk)
            risk_quantity = effective_risk_budget / risk_per_quantity

            trade_notional_capacity = request.equity * config.max_trade_notional_multiple
            portfolio_notional_capacity = (
                request.equity * config.max_portfolio_notional_multiple - portfolio.gross_notional
            )
            symbol_notional_capacity = (
                request.equity * config.max_symbol_notional_multiple - portfolio.symbol_notional
            )
            leverage_notional_capacity = margin_base * leverage_limit
            notional_per_quantity = request.entry_price * instrument.contract_size

            capacity_rows = [
                ("risk_budget", risk_quantity),
                (
                    "trade_notional",
                    trade_notional_capacity / notional_per_quantity,
                ),
                (
                    "portfolio_notional",
                    portfolio_notional_capacity / notional_per_quantity,
                ),
                (
                    "symbol_notional",
                    symbol_notional_capacity / notional_per_quantity,
                ),
                (
                    "leverage",
                    leverage_notional_capacity / notional_per_quantity,
                ),
            ]
            if instrument.max_quantity is not None:
                capacity_rows.append(("instrument_max_quantity", instrument.max_quantity))
            if instrument.max_notional is not None:
                capacity_rows.append(
                    (
                        "instrument_max_notional",
                        instrument.max_notional / notional_per_quantity,
                    )
                )

            if any(value <= ZERO for name, value in capacity_rows if name != "risk_budget"):
                return self._reject(
                    request,
                    RiskRejectReason.NO_NOTIONAL_CAPACITY,
                    "notional or leverage capacity is exhausted",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    diagnostics={name: str(value) for name, value in capacity_rows},
                )

            raw_quantity, binding = _minimum(capacity_rows)
            quantity = _floor_to_step(
                raw_quantity,
                instrument.quantity_step,
                precision=context.prec,
                step_ratio=instrument.quantity_step_ratio,
            )
            if quantity <= ZERO:
                return self._reject(
                    request,
                    RiskRejectReason.ZERO_QUANTITY_AFTER_ROUNDING,
                    "quantity rounds to zero",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                )
            if quantity < instrument.min_quantity:
                return self._reject(
                    request,
                    RiskRejectReason.BELOW_MIN_QUANTITY,
                    "rounded quantity is below instrument minimum",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                    diagnostics={"min_quantity": str(instrument.min_quantity)},
                )
            if instrument.max_quantity is not None and quantity > instrument.max_quantity:
                return self._reject(
                    request,
                    RiskRejectReason.ABOVE_MAX_QUANTITY,
                    "rounded quantity exceeds instrument maximum",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                )

            notional = quantity * notional_per_quantity
            if notional < instrument.min_notional:
                return self._reject(
                    request,
                    RiskRejectReason.BELOW_MIN_NOTIONAL,
                    "position notional is below instrument minimum",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                    diagnostics={
                        "notional": str(notional),
                        "min_notional": str(instrument.min_notional),
                    },
                )
            if instrument.max_notional is not None and notional > instrument.max_notional:
                return self._reject(
                    request,
                    RiskRejectReason.ABOVE_MAX_NOTIONAL,
                    "position notional exceeds instrument maximum",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                )

            actual_risk = quantity * risk_per_quantity
            actual_risk_fraction = actual_risk / request.equity
            if margin_base <= ZERO:
                return self._reject(
                    request,
                    RiskRejectReason.LEVERAGE_LIMIT,
                    "positive available margin is required",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                )
            required_leverage = notional / margin_base
            if required_leverage > leverage_limit:
                return self._reject(
                    request,
                    RiskRejectReason.LEVERAGE_LIMIT,
                    "required leverage exceeds configured or instrument limit",
                    risk_fraction=risk_fraction,
                    risk_budget=effective_risk_budget,
                    raw_quantity=raw_quantity,
                    diagnostics={
                        "required_leverage": str(required_leverage),
                        "leverage_limit": str(leverage_limit),
                    },
                )

        binding_constraints = list(binding)
        if effective_risk_budget < requested_risk_budget:
            binding_constraints.append("portfolio_risk")
        return RiskDecision._from_calculation(
            request_id=request.request_id,
            symbol=request.symbol,
            status=RiskDecisionStatus.APPROVED,
            quantity=quantity,
            raw_quantity=raw_quantity,
            risk_fraction=risk_fraction,
            risk_budget=effective_risk_budget,
            actual_risk_amount=actual_risk,
            actual_risk_fraction=actual_risk_fraction,
            notional=notional,
            required_leverage=required_leverage,
            binding_constraints=tuple(binding_constraints),
            setup_id=request.setup_id,
            diagnostics={
                "requested_risk_budget": str(requested_risk_budget),
                "remaining_portfolio_risk": str(remaining_risk),
                "stop_distance": str(stop_distance),
                "risk_per_quantity": str(risk_per_quantity),
                "quantity_step": str(instrument.quantity_step),
                "leverage_limit": str(leverage_limit),
            },
        )

    def _validate_request(self, request: RiskRequest) -> RiskDecision | None:
        if request.equity <= ZERO:
            return self._reject(
                request,
                RiskRejectReason.NON_POSITIVE_EQUITY,
                "equity must be positive",
            )
        if not request.prices_positive:
            return self._reject(
                request,
                RiskRejectReason.INVALID_PRICE_GEOMETRY,
                "entry and stop prices must be positive",
            )
        if not request.price_geometry_valid:
            message = (
                "buy stop must be below entry"
                if request.side is TradeSide.BUY
                else "sell stop must be above entry"
            )
            return self._reject(
                request,
                RiskRejectReason.INVALID_PRICE_GEOMETRY,
                message,
            )
        tick_size = request.instrument.tick_size
        if (
            self.config.require_price_alignment
            and tick_size is not None
            and not request.prices_aligned
        ):
            return self._reject(
                request,
                RiskRejectReason.PRICE_NOT_ALIGNED,
                "entry or stop is not aligned to instrument tick size",
                diagnostics={"tick_size": str(tick_size)},
            )
        return None

    @staticmethod
    def _reject(
        request: RiskRequest,
        reason: RiskRejectReason,
        message: str,
        *,
        risk_fraction: Decimal = ZERO,
        risk_budget: Decimal = ZERO,
        raw_quantity: Decimal = ZERO,
        diagnostics: dict[str, str] | None = None,
    ) -> RiskDecision:
        return RiskDecision._from_calculation(
            request_id=request.request_id,
            symbol=request.symbol,
            status=RiskDecisionStatus.REJECTED,
            raw_quantity=raw_quantity,
            risk_fraction=risk_fraction,
            risk_budget=risk_budget,
            reject_reason=reason,
            message=message,
            setup_id=request.setup_id,
            diagnostics=diagnostics or {},
        )
