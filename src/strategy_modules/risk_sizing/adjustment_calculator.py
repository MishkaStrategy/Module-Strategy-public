from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext
from typing import Protocol

from ._decimal import ZERO, decimal_work_precision
from .adjustment import PositionAdjustment, PositionAdjustmentDecision
from .config import RiskSizingConfig
from .decision import RiskDecision
from .enums import RiskDecisionStatus, RiskRejectReason, TradeSide
from .request import RiskRequest


class _CalculatorLike(Protocol):
    @property
    def config(self) -> RiskSizingConfig: ...

    @property
    def _config_precision(self) -> int: ...


def _floor_to_step(
    value: Decimal,
    step: Decimal,
    step_ratio: tuple[int, int],
) -> Decimal:
    if value <= ZERO:
        return ZERO
    value_num, value_den = value.as_integer_ratio()
    step_num, step_den = step_ratio
    units = (value_num * step_den) // (value_den * step_num)
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


def _signed_risk_before_contract(
    side: TradeSide,
    average_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
) -> Decimal:
    if side is TradeSide.BUY:
        return (average_price - stop_loss) * quantity
    return (stop_loss - average_price) * quantity


def _total_risk(
    side: TradeSide,
    average_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    contract_size: Decimal,
) -> Decimal:
    return (
        max(
            ZERO,
            _signed_risk_before_contract(
                side,
                average_price,
                stop_loss,
                quantity,
            ),
        )
        * contract_size
    )


def _projected_total_risk(
    *,
    side: TradeSide,
    current_average_price: Decimal,
    current_quantity: Decimal,
    entry_price: Decimal,
    approved_quantity: Decimal,
    stop_loss: Decimal,
    contract_size: Decimal,
) -> Decimal:
    signed_total = _signed_risk_before_contract(
        side,
        current_average_price,
        stop_loss,
        current_quantity,
    ) + _signed_risk_before_contract(
        side,
        entry_price,
        stop_loss,
        approved_quantity,
    )
    return max(ZERO, signed_total) * contract_size


def _valuation_price(
    adjustment: PositionAdjustment,
    contract_size: Decimal,
) -> Decimal:
    if adjustment.valuation_price is not None:
        return adjustment.valuation_price
    return adjustment.current_notional / (adjustment.current_quantity * contract_size)


def _reject(
    request: RiskRequest,
    reason: RiskRejectReason,
    message: str,
    *,
    risk_fraction: Decimal = ZERO,
    risk_budget: Decimal = ZERO,
    raw_quantity: Decimal = ZERO,
    diagnostics: dict[str, object] | None = None,
    position_adjustment: PositionAdjustmentDecision | None = None,
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
        position_adjustment=position_adjustment,
    )


def _decision_geometry(
    request: RiskRequest,
    approved_quantity: Decimal,
) -> PositionAdjustmentDecision:
    adjustment = request.position_adjustment
    if adjustment is None:
        raise RuntimeError("position adjustment is required")
    contract_size = request.instrument.contract_size
    total_quantity = adjustment.current_quantity + approved_quantity
    if approved_quantity > ZERO:
        average_price = (
            adjustment.current_average_price * adjustment.current_quantity
            + request.entry_price * approved_quantity
        ) / total_quantity
        projected_notional = total_quantity * request.entry_price * contract_size
    else:
        average_price = adjustment.current_average_price
        projected_notional = adjustment.current_notional
    projected_risk = _projected_total_risk(
        side=request.side,
        current_average_price=adjustment.current_average_price,
        current_quantity=adjustment.current_quantity,
        entry_price=request.entry_price,
        approved_quantity=approved_quantity,
        stop_loss=request.stop_loss,
        contract_size=contract_size,
    )
    projected_leverage = ZERO if request.equity <= ZERO else projected_notional / request.equity
    return PositionAdjustmentDecision(
        adjustment_id=adjustment.adjustment_id,
        position_id=adjustment.position_id,
        requested_quantity=adjustment.requested_quantity,
        approved_quantity=approved_quantity,
        projected_total_quantity=total_quantity,
        projected_average_price=average_price,
        projected_notional=projected_notional,
        projected_leverage=projected_leverage,
        projected_total_risk=projected_risk,
        opens_new_position=False,
    )


def _geometry_error(
    request: RiskRequest,
    *,
    projected_quantity: Decimal,
    projected_average: Decimal,
    current_notional: Decimal,
    projected_notional: Decimal,
    current_risk: Decimal,
    projected_risk: Decimal,
) -> str | None:
    adjustment = request.position_adjustment
    if adjustment is None:
        return "position adjustment evidence is missing"
    checks = (
        (
            adjustment.projected_quantity,
            projected_quantity,
            "projected_quantity",
        ),
        (
            adjustment.projected_average_price,
            projected_average,
            "projected_average_price",
        ),
        (
            adjustment.current_notional,
            current_notional,
            "current_notional",
        ),
        (
            adjustment.projected_notional,
            projected_notional,
            "projected_notional",
        ),
        (
            adjustment.current_total_risk,
            current_risk,
            "current_total_risk",
        ),
        (
            adjustment.projected_total_risk,
            projected_risk,
            "projected_total_risk",
        ),
    )
    for evidence, calculated, name in checks:
        if evidence != calculated:
            return (
                f"position adjustment {name} disagrees with independently "
                f"calculated geometry: evidence={evidence}, "
                f"calculated={calculated}"
            )
    return None


def _risk_capacity(
    *,
    adjustment: PositionAdjustment,
    side: TradeSide,
    entry_price: Decimal,
    stop_loss: Decimal,
    contract_size: Decimal,
    current_risk: Decimal,
    additional_risk_budget: Decimal,
) -> Decimal:
    signed_current = _signed_risk_before_contract(
        side,
        adjustment.current_average_price,
        stop_loss,
        adjustment.current_quantity,
    )
    slope = entry_price - stop_loss if side is TradeSide.BUY else stop_loss - entry_price
    if slope <= ZERO:
        return adjustment.requested_quantity
    maximum_signed_total = (current_risk + additional_risk_budget) / contract_size
    return (maximum_signed_total - signed_current) / slope


def _capacity_rows(
    *,
    request: RiskRequest,
    config: RiskSizingConfig,
    current_notional: Decimal,
    additional_risk_budget: Decimal,
    current_risk: Decimal,
    leverage_limit: Decimal,
    margin_base: Decimal,
) -> list[tuple[str, Decimal]]:
    adjustment = request.position_adjustment
    if adjustment is None:
        raise RuntimeError("position adjustment is required")
    notional_per_quantity = request.entry_price * request.instrument.contract_size
    non_position_gross = request.portfolio.gross_notional - current_notional
    non_position_symbol = request.portfolio.symbol_notional - current_notional
    rows: list[tuple[str, Decimal]] = [
        ("requested_adjustment", adjustment.requested_quantity),
        ("maximum_adjustment", adjustment.maximum_quantity),
        (
            "risk_budget",
            _risk_capacity(
                adjustment=adjustment,
                side=request.side,
                entry_price=request.entry_price,
                stop_loss=request.stop_loss,
                contract_size=request.instrument.contract_size,
                current_risk=current_risk,
                additional_risk_budget=additional_risk_budget,
            ),
        ),
        (
            "trade_projected_notional",
            request.equity * config.max_trade_notional_multiple / notional_per_quantity
            - adjustment.current_quantity,
        ),
        (
            "portfolio_notional",
            (request.equity * config.max_portfolio_notional_multiple - non_position_gross)
            / notional_per_quantity
            - adjustment.current_quantity,
        ),
        (
            "symbol_notional",
            (request.equity * config.max_symbol_notional_multiple - non_position_symbol)
            / notional_per_quantity
            - adjustment.current_quantity,
        ),
        (
            "projected_leverage",
            request.equity * leverage_limit / notional_per_quantity - adjustment.current_quantity,
        ),
        (
            "available_margin_leverage",
            margin_base * leverage_limit / notional_per_quantity,
        ),
    ]
    if request.instrument.max_quantity is not None:
        rows.append(
            (
                "instrument_projected_quantity",
                request.instrument.max_quantity - adjustment.current_quantity,
            )
        )
    if request.instrument.max_notional is not None:
        rows.append(
            (
                "instrument_projected_notional",
                request.instrument.max_notional / notional_per_quantity
                - adjustment.current_quantity,
            )
        )
    return rows


def evaluate_position_adjustment(
    calculator: _CalculatorLike,
    request: RiskRequest,
    risk_fraction: Decimal,
) -> RiskDecision:
    adjustment = request.position_adjustment
    if adjustment is None:
        raise RuntimeError("position adjustment is required")
    config = calculator.config
    instrument = request.instrument
    portfolio = request.portfolio
    leverage_limit = min(
        config.max_leverage,
        instrument.max_leverage if instrument.max_leverage is not None else config.max_leverage,
    )
    margin_base = (
        request.available_margin if request.available_margin is not None else request.equity
    )

    with localcontext() as context:
        context.prec = max(
            request.calculation_precision,
            calculator._config_precision,
            decimal_work_precision(request.entry_price, request.stop_loss),
        )
        contract_size = instrument.contract_size
        current_quantity = adjustment.current_quantity
        requested_quantity = adjustment.requested_quantity
        projected_quantity = current_quantity + requested_quantity
        projected_average = (
            adjustment.current_average_price * current_quantity
            + request.entry_price * requested_quantity
        ) / projected_quantity
        valuation_price = _valuation_price(adjustment, contract_size)
        current_notional = current_quantity * valuation_price * contract_size
        projected_notional = projected_quantity * request.entry_price * contract_size
        current_risk = _total_risk(
            request.side,
            adjustment.current_average_price,
            request.stop_loss,
            current_quantity,
            contract_size,
        )
        projected_risk = _projected_total_risk(
            side=request.side,
            current_average_price=adjustment.current_average_price,
            current_quantity=current_quantity,
            entry_price=request.entry_price,
            approved_quantity=requested_quantity,
            stop_loss=request.stop_loss,
            contract_size=contract_size,
        )

        error = _geometry_error(
            request,
            projected_quantity=projected_quantity,
            projected_average=projected_average,
            current_notional=current_notional,
            projected_notional=projected_notional,
            current_risk=current_risk,
            projected_risk=projected_risk,
        )
        if error is not None:
            return _reject(
                request,
                RiskRejectReason.INVALID_POSITION_ADJUSTMENT,
                error,
                risk_fraction=risk_fraction,
            )
        if (
            portfolio.gross_notional < current_notional
            or portfolio.symbol_notional < current_notional
            or portfolio.open_risk_amount < current_risk
        ):
            return _reject(
                request,
                RiskRejectReason.INVALID_POSITION_ADJUSTMENT,
                "portfolio snapshot does not include the current position geometry",
                risk_fraction=risk_fraction,
                diagnostics={
                    "current_notional": str(current_notional),
                    "portfolio_gross_notional": str(portfolio.gross_notional),
                    "portfolio_symbol_notional": str(portfolio.symbol_notional),
                    "current_risk": str(current_risk),
                    "portfolio_open_risk": str(portfolio.open_risk_amount),
                },
            )
        if margin_base <= ZERO:
            return _reject(
                request,
                RiskRejectReason.LEVERAGE_LIMIT,
                "positive available margin is required",
                risk_fraction=risk_fraction,
            )

        requested_risk_budget = request.equity * risk_fraction
        remaining_portfolio_risk = (
            request.equity * config.max_portfolio_risk_fraction - portfolio.open_risk_amount
        )
        additional_risk_budget = min(
            requested_risk_budget,
            max(ZERO, remaining_portfolio_risk),
        )
        requested_additional_risk = max(
            ZERO,
            projected_risk - current_risk,
        )
        zero_geometry = _decision_geometry(request, ZERO)
        if not config.allow_risk_reduction and requested_additional_risk > additional_risk_budget:
            return _reject(
                request,
                RiskRejectReason.NO_RISK_CAPACITY,
                "requested adjustment risk exceeds remaining portfolio risk capacity",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                diagnostics={
                    "remaining_portfolio_risk": str(remaining_portfolio_risk),
                    "requested_additional_risk": str(requested_additional_risk),
                },
                position_adjustment=zero_geometry,
            )

        capacities = _capacity_rows(
            request=request,
            config=config,
            current_notional=current_notional,
            additional_risk_budget=additional_risk_budget,
            current_risk=current_risk,
            leverage_limit=leverage_limit,
            margin_base=margin_base,
        )
        non_positive = [name for name, value in capacities if value <= ZERO]
        if non_positive:
            reason = (
                RiskRejectReason.NO_RISK_CAPACITY
                if "risk_budget" in non_positive
                else RiskRejectReason.PROJECTED_POSITION_LIMIT
            )
            return _reject(
                request,
                reason,
                "projected position has no remaining capacity",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                diagnostics={name: str(value) for name, value in capacities},
                position_adjustment=zero_geometry,
            )

        raw_quantity, binding = _minimum(capacities)
        quantity = _floor_to_step(
            raw_quantity,
            instrument.quantity_step,
            instrument.quantity_step_ratio,
        )
        if quantity <= ZERO:
            return _reject(
                request,
                RiskRejectReason.ZERO_QUANTITY_AFTER_ROUNDING,
                "quantity rounds to zero",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                raw_quantity=raw_quantity,
                position_adjustment=zero_geometry,
            )
        if quantity < instrument.min_quantity:
            return _reject(
                request,
                RiskRejectReason.BELOW_MIN_QUANTITY,
                "rounded quantity is below instrument minimum",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                raw_quantity=raw_quantity,
                diagnostics={"min_quantity": str(instrument.min_quantity)},
                position_adjustment=zero_geometry,
            )
        order_notional = quantity * request.entry_price * contract_size
        if order_notional < instrument.min_notional:
            return _reject(
                request,
                RiskRejectReason.BELOW_MIN_NOTIONAL,
                "order notional is below instrument minimum",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                raw_quantity=raw_quantity,
                diagnostics={
                    "notional": str(order_notional),
                    "min_notional": str(instrument.min_notional),
                },
                position_adjustment=zero_geometry,
            )

        decision_geometry = _decision_geometry(request, quantity)
        actual_additional_risk = max(
            ZERO,
            decision_geometry.projected_total_risk - current_risk,
        )
        if actual_additional_risk > additional_risk_budget:
            return _reject(
                request,
                RiskRejectReason.NO_RISK_CAPACITY,
                "rounded adjustment exceeds additional risk budget",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                raw_quantity=raw_quantity,
                position_adjustment=zero_geometry,
            )
        if decision_geometry.projected_leverage > leverage_limit:
            return _reject(
                request,
                RiskRejectReason.LEVERAGE_LIMIT,
                "projected position leverage exceeds configured or instrument limit",
                risk_fraction=risk_fraction,
                risk_budget=additional_risk_budget,
                raw_quantity=raw_quantity,
                diagnostics={
                    "projected_leverage": str(decision_geometry.projected_leverage),
                    "leverage_limit": str(leverage_limit),
                },
                position_adjustment=zero_geometry,
            )
        required_leverage = order_notional / margin_base

    constraints = list(binding)
    if actual_additional_risk > ZERO and additional_risk_budget < requested_risk_budget:
        constraints.append("portfolio_risk")
    return RiskDecision._from_calculation(
        request_id=request.request_id,
        symbol=request.symbol,
        status=RiskDecisionStatus.APPROVED,
        quantity=quantity,
        raw_quantity=raw_quantity,
        risk_fraction=risk_fraction,
        risk_budget=additional_risk_budget,
        actual_risk_amount=actual_additional_risk,
        actual_risk_fraction=actual_additional_risk / request.equity,
        notional=order_notional,
        required_leverage=required_leverage,
        binding_constraints=tuple(constraints),
        setup_id=request.setup_id,
        diagnostics={
            "requested_risk_budget": str(requested_risk_budget),
            "remaining_portfolio_risk": str(remaining_portfolio_risk),
            "requested_additional_risk": str(requested_additional_risk),
            "approved_additional_risk": str(actual_additional_risk),
            "quantity_step": str(instrument.quantity_step),
            "leverage_limit": str(leverage_limit),
            "opens_new_position": False,
        },
        position_adjustment=decision_geometry,
    )
