from decimal import Decimal

from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    PositionAdjustment,
    RiskCalculator,
    RiskRejectReason,
    RiskRequest,
    RiskSizingConfig,
    TradeSide,
)


def _request(
    adjustment: PositionAdjustment,
    *,
    entry_price: str,
    stop_loss: str,
    equity: str = "1000",
    available_margin: str = "500",
    contract_size: str = "1",
    portfolio_gross: str | None = None,
    portfolio_symbol: str | None = None,
    portfolio_risk: str = "0",
    requested_risk_fraction: str = "0.005",
    instrument_max_notional: str | None = None,
) -> RiskRequest:
    current_notional = adjustment.current_notional
    return RiskRequest(
        request_id=f"risk-{adjustment.adjustment_id}",
        symbol="BTCUSDT",
        side=TradeSide.BUY,
        entry_price=Decimal(entry_price),
        stop_loss=Decimal(stop_loss),
        equity=Decimal(equity),
        instrument=InstrumentRiskRules(
            quantity_step=Decimal("0.01"),
            contract_size=Decimal(contract_size),
            max_leverage=Decimal("3"),
            max_notional=(
                None if instrument_max_notional is None else Decimal(instrument_max_notional)
            ),
        ),
        portfolio=PortfolioRiskSnapshot(
            gross_notional=(
                current_notional if portfolio_gross is None else Decimal(portfolio_gross)
            ),
            symbol_notional=(
                current_notional if portfolio_symbol is None else Decimal(portfolio_symbol)
            ),
            open_risk_amount=Decimal(portfolio_risk),
            open_positions=10,
        ),
        requested_risk_fraction=Decimal(requested_risk_fraction),
        available_margin=Decimal(available_margin),
        position_adjustment=adjustment,
    )


def test_protected_profit_adjustment_can_be_risk_neutral() -> None:
    adjustment = PositionAdjustment(
        adjustment_id="adj-profit",
        position_id="pos-profit",
        kind="pyramiding",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=Decimal("0.25"),
        maximum_quantity=Decimal("0.25"),
        projected_quantity=Decimal("1.25"),
        current_average_price=Decimal("100"),
        projected_average_price=Decimal("102"),
        valuation_price=Decimal("110"),
        current_total_risk=Decimal("0"),
        projected_total_risk=Decimal("0"),
        current_notional=Decimal("110"),
        projected_notional=Decimal("137.5"),
    )
    decision = RiskCalculator().evaluate(
        _request(
            adjustment,
            entry_price="110",
            stop_loss="102",
            portfolio_risk="20",
        )
    )
    assert decision.approved
    assert decision.quantity == Decimal("0.25")
    assert decision.actual_risk_amount == Decimal("0")
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.projected_total_risk == Decimal("0")


def test_partial_approval_uses_incremental_full_position_risk() -> None:
    adjustment = PositionAdjustment(
        adjustment_id="adj-partial",
        position_id="pos-partial",
        kind="averaging_down",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=Decimal("1"),
        maximum_quantity=Decimal("1"),
        projected_quantity=Decimal("2"),
        current_average_price=Decimal("100"),
        projected_average_price=Decimal("100"),
        valuation_price=Decimal("100"),
        current_total_risk=Decimal("10"),
        projected_total_risk=Decimal("20"),
        current_notional=Decimal("100"),
        projected_notional=Decimal("200"),
    )
    decision = RiskCalculator(RiskSizingConfig(allow_risk_reduction=True)).evaluate(
        _request(
            adjustment,
            entry_price="100",
            stop_loss="90",
            requested_risk_fraction="0.005",
            portfolio_risk="10",
        )
    )
    assert decision.approved
    assert decision.quantity == Decimal("0.50")
    assert decision.actual_risk_amount == Decimal("5.0")
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.projected_total_risk == Decimal("15.0")


def test_mark_to_market_projected_notional_may_be_below_current_notional() -> None:
    adjustment = PositionAdjustment(
        adjustment_id="adj-mark",
        position_id="pos-mark",
        kind="averaging_down",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=Decimal("0.01"),
        maximum_quantity=Decimal("0.01"),
        projected_quantity=Decimal("1.01"),
        current_average_price=Decimal("100"),
        projected_average_price=Decimal("99.50495049504950495049504950"),
        valuation_price=Decimal("100"),
        current_total_risk=Decimal("10"),
        projected_total_risk=Decimal("9.6"),
        current_notional=Decimal("100"),
        projected_notional=Decimal("50.5"),
    )
    assert adjustment.projected_notional < adjustment.current_notional
    assert adjustment.projected_total_risk < adjustment.current_total_risk


def test_portfolio_snapshot_must_include_current_position() -> None:
    adjustment = PositionAdjustment(
        adjustment_id="adj-missing",
        position_id="pos-missing",
        kind="position_increase",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=Decimal("0.25"),
        maximum_quantity=Decimal("0.25"),
        projected_quantity=Decimal("1.25"),
        current_average_price=Decimal("100"),
        projected_average_price=Decimal("98"),
        valuation_price=Decimal("100"),
        current_total_risk=Decimal("10"),
        projected_total_risk=Decimal("10"),
        current_notional=Decimal("100"),
        projected_notional=Decimal("120"),
    )
    decision = RiskCalculator().evaluate(
        _request(
            adjustment,
            entry_price="96",
            stop_loss="90",
            portfolio_gross="99",
            portfolio_symbol="99",
            portfolio_risk="10",
        )
    )
    assert not decision.approved
    assert decision.reject_reason is RiskRejectReason.INVALID_POSITION_ADJUSTMENT


def test_projected_instrument_notional_applies_to_total_position() -> None:
    adjustment = PositionAdjustment(
        adjustment_id="adj-max-notional",
        position_id="pos-max-notional",
        kind="position_increase",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=Decimal("0.50"),
        maximum_quantity=Decimal("0.50"),
        projected_quantity=Decimal("1.50"),
        current_average_price=Decimal("100"),
        projected_average_price=Decimal("100"),
        valuation_price=Decimal("100"),
        current_total_risk=Decimal("10"),
        projected_total_risk=Decimal("15"),
        current_notional=Decimal("100"),
        projected_notional=Decimal("150"),
    )
    decision = RiskCalculator().evaluate(
        _request(
            adjustment,
            entry_price="100",
            stop_loss="90",
            portfolio_risk="10",
            instrument_max_notional="120",
        )
    )
    assert decision.approved
    assert decision.quantity == Decimal("0.20")
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.projected_notional == Decimal("120.00")


def test_derivative_risk_geometry_uses_contract_size() -> None:
    adjustment = PositionAdjustment(
        adjustment_id="adj-contract",
        position_id="pos-contract",
        kind="position_increase",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=Decimal("0.25"),
        maximum_quantity=Decimal("0.25"),
        projected_quantity=Decimal("1.25"),
        current_average_price=Decimal("100"),
        projected_average_price=Decimal("99.2"),
        valuation_price=Decimal("100"),
        current_total_risk=Decimal("100"),
        projected_total_risk=Decimal("115"),
        current_notional=Decimal("1000"),
        projected_notional=Decimal("1200"),
    )
    decision = RiskCalculator().evaluate(
        _request(
            adjustment,
            entry_price="96",
            stop_loss="90",
            equity="10000",
            available_margin="5000",
            contract_size="10",
            portfolio_risk="100",
        )
    )
    assert decision.approved
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.projected_notional == Decimal("1200")
    assert decision.position_adjustment.projected_total_risk == Decimal("115")
