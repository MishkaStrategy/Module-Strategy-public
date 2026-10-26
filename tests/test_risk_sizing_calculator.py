from decimal import Decimal
import random

import pytest

from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    RiskCalculator,
    RiskRejectReason,
    RiskRequest,
    RiskSizingConfig,
    TradeSide,
)


def request(
    *,
    side: TradeSide = TradeSide.BUY,
    entry: str = "100",
    stop: str = "99",
    equity: str = "10000",
    risk: str | None = "0.005",
    instrument: InstrumentRiskRules | None = None,
    portfolio: PortfolioRiskSnapshot | None = None,
    margin: str | None = None,
) -> RiskRequest:
    return RiskRequest(
        request_id="r1",
        symbol="BTCUSDT",
        side=side,
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop),
        equity=Decimal(equity),
        requested_risk_fraction=None if risk is None else Decimal(risk),
        available_margin=None if margin is None else Decimal(margin),
        instrument=instrument
        or InstrumentRiskRules(
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            tick_size=Decimal("0.1"),
            max_leverage=Decimal("20"),
        ),
        portfolio=portfolio or PortfolioRiskSnapshot(),
    )


def test_fixed_fractional_quantity_and_risk() -> None:
    decision = RiskCalculator().evaluate(request())
    assert decision.approved
    assert decision.quantity == Decimal("50")
    assert decision.actual_risk_amount == Decimal("50")
    assert decision.actual_risk_fraction == Decimal("0.005")


def test_buy_sell_symmetry() -> None:
    calc = RiskCalculator()
    buy = calc.evaluate(request(side=TradeSide.BUY, entry="100", stop="99"))
    sell = calc.evaluate(request(side=TradeSide.SELL, entry="100", stop="101"))
    assert buy.approved and sell.approved
    assert buy.quantity == sell.quantity
    assert buy.actual_risk_amount == sell.actual_risk_amount


@pytest.mark.parametrize(
    ("side", "entry", "stop"),
    [
        (TradeSide.BUY, "100", "100"),
        (TradeSide.BUY, "100", "101"),
        (TradeSide.SELL, "100", "100"),
        (TradeSide.SELL, "100", "99"),
    ],
)
def test_invalid_geometry(side: TradeSide, entry: str, stop: str) -> None:
    result = RiskCalculator().evaluate(request(side=side, entry=entry, stop=stop))
    assert result.reject_reason is RiskRejectReason.INVALID_PRICE_GEOMETRY


def test_quantity_always_rounds_down() -> None:
    instrument = InstrumentRiskRules(quantity_step=Decimal("0.3"), tick_size=Decimal("0.1"))
    result = RiskCalculator().evaluate(request(instrument=instrument, stop="97"))
    assert result.approved
    assert result.quantity % Decimal("0.3") == 0
    assert result.quantity <= result.raw_quantity
    assert result.actual_risk_amount <= result.risk_budget


def test_arbitrary_non_power_of_ten_step() -> None:
    instrument = InstrumentRiskRules(quantity_step=Decimal("0.125"), tick_size=Decimal("0.05"))
    result = RiskCalculator().evaluate(request(instrument=instrument, entry="100.00", stop="99.05"))
    assert result.approved
    assert result.quantity % Decimal("0.125") == 0


def test_huge_exact_floor_does_not_round_up() -> None:
    instrument = InstrumentRiskRules(quantity_step=Decimal("0.0000000000000000000000000003"))
    cfg = RiskSizingConfig(
        max_trade_notional_multiple=Decimal("1e40"),
        max_portfolio_notional_multiple=Decimal("1e40"),
        max_symbol_notional_multiple=Decimal("1e40"),
        max_leverage=Decimal("1e40"),
    )
    result = RiskCalculator(cfg).evaluate(
        request(
            entry="1",
            stop="0.9999999999999999999999999999",
            equity="1e20",
            instrument=instrument,
            margin="1e20",
        )
    )
    assert result.approved
    assert result.quantity <= result.raw_quantity
    assert result.actual_risk_amount <= result.risk_budget


def test_contract_size_is_applied_to_risk_and_notional() -> None:
    instrument = InstrumentRiskRules(
        quantity_step=Decimal("1"),
        contract_size=Decimal("10"),
        max_leverage=Decimal("100"),
    )
    result = RiskCalculator().evaluate(request(instrument=instrument))
    assert result.approved
    assert result.quantity == Decimal("5")
    assert result.notional == Decimal("5000")
    assert result.actual_risk_amount == Decimal("50")


def test_portfolio_risk_reduction_and_binding_reason() -> None:
    portfolio = PortfolioRiskSnapshot(open_risk_amount=Decimal("290"))
    result = RiskCalculator().evaluate(request(portfolio=portfolio))
    assert result.approved
    assert result.risk_budget == Decimal("10")
    assert "portfolio_risk" in result.binding_constraints


def test_portfolio_risk_reduction_can_be_disabled() -> None:
    cfg = RiskSizingConfig(allow_risk_reduction=False)
    portfolio = PortfolioRiskSnapshot(open_risk_amount=Decimal("290"))
    result = RiskCalculator(cfg).evaluate(request(portfolio=portfolio))
    assert result.reject_reason is RiskRejectReason.NO_RISK_CAPACITY


def test_notional_and_leverage_caps() -> None:
    cfg = RiskSizingConfig(
        max_trade_notional_multiple=Decimal("1"),
        max_symbol_notional_multiple=Decimal("1"),
        max_portfolio_notional_multiple=Decimal("1"),
        max_leverage=Decimal("1"),
    )
    instrument = InstrumentRiskRules(quantity_step=Decimal("0.001"), tick_size=Decimal("0.01"))
    result = RiskCalculator(cfg).evaluate(request(stop="99.99", instrument=instrument))
    assert result.approved
    assert result.notional <= Decimal("10000")
    assert result.required_leverage <= Decimal("1")


def test_zero_margin_rejected() -> None:
    result = RiskCalculator().evaluate(request(margin="0"))
    assert not result.approved
    assert result.reject_reason in {
        RiskRejectReason.NO_NOTIONAL_CAPACITY,
        RiskRejectReason.LEVERAGE_LIMIT,
    }


def test_minimums_never_force_rounding_up() -> None:
    instrument = InstrumentRiskRules(
        quantity_step=Decimal("1"),
        min_quantity=Decimal("100"),
        min_notional=Decimal("10000"),
    )
    result = RiskCalculator().evaluate(request(instrument=instrument, risk="0.0001"))
    assert result.reject_reason in {
        RiskRejectReason.BELOW_MIN_QUANTITY,
        RiskRejectReason.BELOW_MIN_NOTIONAL,
    }


def test_randomized_invariants() -> None:
    rng = random.Random(20260721)
    calc = RiskCalculator()
    for index in range(5000):
        side = TradeSide.BUY if index % 2 == 0 else TradeSide.SELL
        entry = Decimal(rng.randrange(1000, 1_000_000)) / Decimal("100")
        distance = Decimal(rng.randrange(1, 10000)) / Decimal("10000")
        stop = entry - distance if side is TradeSide.BUY else entry + distance
        step = Decimal(rng.choice(["0.001", "0.01", "0.1", "0.125", "0.25"]))
        result = calc.evaluate(
            request(
                side=side,
                entry=str(entry),
                stop=str(stop),
                instrument=InstrumentRiskRules(
                    quantity_step=step,
                    tick_size=Decimal("0.0001"),
                    max_leverage=Decimal("20"),
                ),
            )
        )
        if result.approved:
            assert result.quantity > 0
            assert result.quantity % step == 0
            assert result.quantity <= result.raw_quantity
            assert result.actual_risk_amount <= result.risk_budget
            assert result.actual_risk_fraction <= Decimal("0.005")
            assert result.required_leverage <= Decimal("3")


def test_default_risk_fraction_is_used() -> None:
    result = RiskCalculator().evaluate(request(risk=None))
    assert result.approved
    assert result.risk_fraction == Decimal("0.005")


@pytest.mark.parametrize("risk", ["0", "0.00001", "0.021", "1"])
def test_risk_fraction_boundaries_reject_outside_config(risk: str) -> None:
    result = RiskCalculator().evaluate(request(risk=risk))
    assert result.reject_reason is RiskRejectReason.RISK_FRACTION_OUT_OF_RANGE


def test_max_positions_boundary_is_inclusive() -> None:
    portfolio = PortfolioRiskSnapshot(open_positions=10)
    result = RiskCalculator().evaluate(request(portfolio=portfolio))
    assert result.reject_reason is RiskRejectReason.MAX_POSITIONS_REACHED


def test_exhausted_open_risk_rejected() -> None:
    portfolio = PortfolioRiskSnapshot(open_risk_amount=Decimal("300"))
    result = RiskCalculator().evaluate(request(portfolio=portfolio))
    assert result.reject_reason is RiskRejectReason.NO_RISK_CAPACITY


def test_exhausted_gross_notional_rejected() -> None:
    portfolio = PortfolioRiskSnapshot(gross_notional=Decimal("50000"))
    result = RiskCalculator().evaluate(request(portfolio=portfolio))
    assert result.reject_reason is RiskRejectReason.NO_NOTIONAL_CAPACITY


def test_zero_quantity_after_rounding() -> None:
    instrument = InstrumentRiskRules(quantity_step=Decimal("1000"), tick_size=Decimal("0.1"))
    result = RiskCalculator().evaluate(request(instrument=instrument))
    assert result.reject_reason is RiskRejectReason.ZERO_QUANTITY_AFTER_ROUNDING


def test_below_min_quantity() -> None:
    instrument = InstrumentRiskRules(
        quantity_step=Decimal("1"),
        min_quantity=Decimal("100"),
        tick_size=Decimal("0.1"),
    )
    result = RiskCalculator().evaluate(request(instrument=instrument))
    assert result.reject_reason is RiskRejectReason.BELOW_MIN_QUANTITY


def test_below_min_notional() -> None:
    instrument = InstrumentRiskRules(
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("100000"),
        tick_size=Decimal("0.1"),
    )
    result = RiskCalculator().evaluate(request(instrument=instrument))
    assert result.reject_reason is RiskRejectReason.BELOW_MIN_NOTIONAL


def test_price_alignment_can_be_disabled() -> None:
    cfg = RiskSizingConfig(require_price_alignment=False)
    result = RiskCalculator(cfg).evaluate(request(entry="100.03", stop="99.01"))
    assert result.approved


def test_non_positive_equity_rejected() -> None:
    result = RiskCalculator().evaluate(request(equity="0"))
    assert result.reject_reason is RiskRejectReason.NON_POSITIVE_EQUITY


def test_non_positive_prices_rejected() -> None:
    result = RiskCalculator().evaluate(request(entry="0", stop="-1"))
    assert result.reject_reason is RiskRejectReason.INVALID_PRICE_GEOMETRY


def test_instrument_quantity_and_notional_caps_are_binding() -> None:
    instrument = InstrumentRiskRules(
        quantity_step=Decimal("0.1"),
        max_quantity=Decimal("3"),
        max_notional=Decimal("250"),
        tick_size=Decimal("0.1"),
        max_leverage=Decimal("100"),
    )
    result = RiskCalculator(
        RiskSizingConfig(
            max_trade_notional_multiple=Decimal("100"),
            max_portfolio_notional_multiple=Decimal("100"),
            max_symbol_notional_multiple=Decimal("100"),
            max_leverage=Decimal("100"),
        )
    ).evaluate(request(instrument=instrument))
    assert result.approved
    assert result.quantity == Decimal("2.5")
    assert "instrument_max_notional" in result.binding_constraints


def test_required_leverage_respects_instrument_lower_limit() -> None:
    instrument = InstrumentRiskRules(
        quantity_step=Decimal("0.1"),
        tick_size=Decimal("0.1"),
        max_leverage=Decimal("0.5"),
    )
    result = RiskCalculator().evaluate(request(instrument=instrument))
    assert result.approved
    assert result.required_leverage <= Decimal("0.5")
