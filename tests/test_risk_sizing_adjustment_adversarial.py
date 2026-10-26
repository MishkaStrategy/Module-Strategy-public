from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType

import pytest

from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    PositionAdjustment,
    PositionAdjustmentDecision,
    RiskCalculator,
    RiskDecisionStatus,
    RiskRejectReason,
    RiskRequest,
    RiskSizingConfig,
    TradeSide,
)
from strategy_modules.risk_sizing import adjustment_calculator as adjustment_math


D = Decimal


def _adjustment(**overrides: object) -> PositionAdjustment:
    values: dict[str, object] = {
        "adjustment_id": "adj-1",
        "position_id": "pos-1",
        "kind": "position_increase",
        "opens_new_position": False,
        "current_quantity": D("1"),
        "requested_quantity": D("0.25"),
        "maximum_quantity": D("0.25"),
        "projected_quantity": D("1.25"),
        "current_average_price": D("100"),
        "projected_average_price": D("100"),
        "valuation_price": D("100"),
        "current_total_risk": D("10"),
        "projected_total_risk": D("12.5"),
        "current_notional": D("100"),
        "projected_notional": D("125"),
    }
    values.update(overrides)
    return PositionAdjustment(**values)  # type: ignore[arg-type]


def _decision(**overrides: object) -> PositionAdjustmentDecision:
    values: dict[str, object] = {
        "adjustment_id": "adj-1",
        "position_id": "pos-1",
        "requested_quantity": D("0.25"),
        "approved_quantity": D("0.20"),
        "projected_total_quantity": D("1.20"),
        "projected_average_price": D("100"),
        "projected_notional": D("120"),
        "projected_leverage": D("0.12"),
        "projected_total_risk": D("12"),
    }
    values.update(overrides)
    return PositionAdjustmentDecision(**values)  # type: ignore[arg-type]


def _request(
    *,
    adjustment: PositionAdjustment | None = None,
    side: TradeSide = TradeSide.BUY,
    entry_price: str = "100",
    stop_loss: str = "90",
    equity: str = "1000",
    available_margin: str | None = "1000",
    portfolio: PortfolioRiskSnapshot | None = None,
    instrument: InstrumentRiskRules | None = None,
    requested_risk_fraction: str = "0.005",
) -> RiskRequest:
    active_adjustment = adjustment if adjustment is not None else _adjustment()
    active_portfolio = portfolio or PortfolioRiskSnapshot(
        gross_notional=active_adjustment.current_notional,
        symbol_notional=active_adjustment.current_notional,
        open_risk_amount=active_adjustment.current_total_risk,
        open_positions=10,
    )
    active_instrument = instrument or InstrumentRiskRules(
        quantity_step=D("0.01"),
        min_quantity=D("0.01"),
        min_notional=D("1"),
        contract_size=D("1"),
        max_leverage=D("3"),
    )
    return RiskRequest(
        request_id="risk-adj-1",
        symbol="BTCUSDT",
        side=side,
        entry_price=D(entry_price),
        stop_loss=D(stop_loss),
        equity=D(equity),
        requested_risk_fraction=D(requested_risk_fraction),
        available_margin=(None if available_margin is None else D(available_margin)),
        instrument=active_instrument,
        portfolio=active_portfolio,
        position_adjustment=active_adjustment,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("adjustment_id", "", "non-blank"),
        ("position_id", " pos-1", "surrounding whitespace"),
        ("schema_version", "2", "schema_version"),
        ("kind", "scale_out", "kind"),
        ("opens_new_position", 1, "boolean"),
        ("opens_new_position", True, "cannot open"),
        ("valuation_price", D("0"), "valuation_price"),
        ("maximum_quantity", D("0.1"), "maximum_quantity"),
        ("projected_quantity", D("1.24"), "projected_quantity"),
    ],
)
def test_adjustment_rejects_invalid_contract_fields(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _adjustment(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "current_quantity",
        "requested_quantity",
        "maximum_quantity",
        "projected_quantity",
        "current_average_price",
        "projected_average_price",
        "current_notional",
        "projected_notional",
    ],
)
def test_adjustment_requires_positive_geometry(field: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _adjustment(**{field: D("0")})


@pytest.mark.parametrize("field", ["current_total_risk", "projected_total_risk"])
def test_adjustment_rejects_negative_risk(field: str) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _adjustment(**{field: D("-1")})


def test_adjustment_allows_missing_valuation_only_for_legacy_metadata() -> None:
    payload = dict(_adjustment().to_mapping())
    payload.pop("valuation_price")
    with pytest.raises(ValueError, match="valuation_price"):
        PositionAdjustment.from_mapping(payload)
    restored = PositionAdjustment.from_mapping(payload, legacy_metadata=True)
    assert restored.valuation_price is None


def test_adjustment_mapping_rejects_unknown_missing_and_conflicting_aliases() -> None:
    payload = dict(_adjustment().to_mapping())
    payload["unknown"] = "value"
    with pytest.raises(ValueError, match="unknown position adjustment"):
        PositionAdjustment.from_mapping(payload)

    payload = dict(_adjustment().to_mapping())
    payload.pop("current_quantity")
    with pytest.raises(ValueError, match="missing position adjustment"):
        PositionAdjustment.from_mapping(payload)

    payload = dict(_adjustment().to_mapping())
    payload["requested_delta_quantity"] = "0.50"
    with pytest.raises(ValueError, match="conflicting aliases"):
        PositionAdjustment.from_mapping(payload)

    with pytest.raises(ValueError, match="must be a mapping"):
        PositionAdjustment.from_mapping([])  # type: ignore[arg-type]


def test_adjustment_legacy_aliases_round_trip_and_ignore_extra_metadata() -> None:
    payload = {
        "adjustment_id": "adj-legacy",
        "position_id": "pos-legacy",
        "adjustment_kind": "pyramiding",
        "opens_new_position": False,
        "current_quantity": "1",
        "requested_delta_quantity": "0.25",
        "maximum_delta_quantity": "0.25",
        "projected_total_quantity": "1.25",
        "current_average_entry_price": "100",
        "projected_average_entry_price": "100",
        "current_market_price": "100",
        "current_total_risk": "10",
        "projected_total_risk": "12.5",
        "current_notional": "100",
        "projected_notional": "125",
        "producer_note": "ignored in legacy mode",
    }
    adjustment = PositionAdjustment.from_mapping(payload, legacy_metadata=True)
    assert adjustment.kind == "pyramiding"
    assert adjustment.valuation_price == D("100")
    assert PositionAdjustment.from_mapping(adjustment.to_mapping()) == adjustment


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("adjustment_id", " ", "non-blank"),
        ("position_id", "pos-1 ", "surrounding whitespace"),
        ("schema_version", "2", "schema_version"),
        ("opens_new_position", 1, "existing position"),
        ("opens_new_position", True, "existing position"),
        ("requested_quantity", D("0"), "requested_quantity"),
        ("approved_quantity", D("0.3"), "approved_quantity"),
        ("projected_total_quantity", D("0"), "projected_total_quantity"),
        ("projected_average_price", D("0"), "projected_average_price"),
        ("projected_total_risk", D("-1"), "cannot be negative"),
    ],
)
def test_adjustment_decision_rejects_invalid_contract_fields(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _decision(**{field: value})


def test_adjustment_decision_mapping_is_strict_and_round_trips() -> None:
    decision = _decision()
    assert PositionAdjustmentDecision.from_mapping(decision.to_mapping()) == decision

    payload = dict(decision.to_mapping())
    payload["unknown"] = "value"
    with pytest.raises(ValueError, match="unknown position adjustment decision"):
        PositionAdjustmentDecision.from_mapping(payload)

    payload = dict(decision.to_mapping())
    payload["opens_new_position"] = 0
    with pytest.raises(ValueError, match="boolean"):
        PositionAdjustmentDecision.from_mapping(payload)

    payload = dict(decision.to_mapping())
    payload.pop("schema_version")
    with pytest.raises(ValueError, match="schema_version"):
        PositionAdjustmentDecision.from_mapping(payload)


def test_adjustment_mappings_are_immutable_views() -> None:
    adjustment_mapping = _adjustment().to_mapping()
    decision_mapping = _decision().to_mapping()
    assert not isinstance(adjustment_mapping, MappingProxyType)
    assert adjustment_mapping["adjustment_id"] == "adj-1"
    assert decision_mapping["approved_quantity"] == "0.20"


def test_private_helpers_cover_zero_sell_tie_and_missing_adjustment() -> None:
    assert adjustment_math._floor_to_step(D("0"), D("0.1"), (1, 10)) == 0
    assert adjustment_math._minimum((("a", D("2")), ("b", D("1")), ("c", D("1")))) == (
        D("1"),
        ("b", "c"),
    )
    assert adjustment_math._signed_risk_before_contract(
        TradeSide.SELL, D("100"), D("110"), D("2")
    ) == D("20")
    request = replace(_request(), position_adjustment=None)
    with pytest.raises(RuntimeError, match="position adjustment"):
        adjustment_math.evaluate_position_adjustment(RiskCalculator(), request, D("0.005"))


def test_valuation_price_falls_back_to_current_notional() -> None:
    adjustment = _adjustment(valuation_price=None)
    request = _request(adjustment=adjustment)
    decision = RiskCalculator().evaluate(request)
    assert decision.approved


def test_sell_adjustment_uses_sell_risk_geometry() -> None:
    adjustment = _adjustment(
        current_average_price=D("100"),
        projected_average_price=D("100"),
        current_total_risk=D("10"),
        projected_total_risk=D("12.5"),
    )
    decision = RiskCalculator().evaluate(
        _request(
            adjustment=adjustment,
            side=TradeSide.SELL,
            entry_price="100",
            stop_loss="110",
        )
    )
    assert decision.approved
    assert decision.actual_risk_amount == D("2.5")


def test_geometry_mismatch_and_missing_portfolio_exposure_fail_closed() -> None:
    mismatch = _adjustment(projected_notional=D("126"))
    decision = RiskCalculator().evaluate(_request(adjustment=mismatch))
    assert decision.reject_reason is RiskRejectReason.INVALID_POSITION_ADJUSTMENT
    assert "projected_notional" in decision.message

    missing = PortfolioRiskSnapshot(
        gross_notional=D("99"),
        symbol_notional=D("99"),
        open_risk_amount=D("9"),
        open_positions=10,
    )
    decision = RiskCalculator().evaluate(_request(portfolio=missing))
    assert decision.reject_reason is RiskRejectReason.INVALID_POSITION_ADJUSTMENT
    assert decision.diagnostics["portfolio_gross_notional"] == "99"


def test_non_positive_margin_and_strict_risk_capacity_reject() -> None:
    decision = RiskCalculator().evaluate(_request(available_margin="0"))
    assert decision.reject_reason is RiskRejectReason.LEVERAGE_LIMIT

    calculator = RiskCalculator(RiskSizingConfig(allow_risk_reduction=False))
    adjustment = _adjustment(
        requested_quantity=D("1"),
        maximum_quantity=D("1"),
        projected_quantity=D("2"),
        projected_notional=D("200"),
        projected_total_risk=D("20"),
    )
    decision = calculator.evaluate(
        _request(adjustment=adjustment, requested_risk_fraction="0.0001")
    )
    assert decision.reject_reason is RiskRejectReason.NO_RISK_CAPACITY


def test_projected_capacity_rejections_cover_risk_and_notional_limits() -> None:
    no_risk = PortfolioRiskSnapshot(
        gross_notional=D("100"),
        symbol_notional=D("100"),
        open_risk_amount=D("30"),
        open_positions=10,
    )
    decision = RiskCalculator().evaluate(_request(portfolio=no_risk))
    assert decision.reject_reason is RiskRejectReason.NO_RISK_CAPACITY

    tight_config = RiskSizingConfig(max_trade_notional_multiple=D("0.1"))
    decision = RiskCalculator(tight_config).evaluate(_request())
    assert decision.reject_reason is RiskRejectReason.PROJECTED_POSITION_LIMIT


def test_instrument_projected_quantity_and_notional_caps_bind() -> None:
    quantity_rules = InstrumentRiskRules(
        quantity_step=D("0.01"),
        min_quantity=D("0.01"),
        min_notional=D("1"),
        max_quantity=D("1.10"),
        contract_size=D("1"),
        max_leverage=D("3"),
    )
    quantity_decision = RiskCalculator().evaluate(_request(instrument=quantity_rules))
    assert quantity_decision.approved
    assert quantity_decision.quantity == D("0.10")
    assert "instrument_projected_quantity" in quantity_decision.binding_constraints

    notional_rules = replace(quantity_rules, max_quantity=None, max_notional=D("115"))
    notional_decision = RiskCalculator().evaluate(_request(instrument=notional_rules))
    assert notional_decision.approved
    assert notional_decision.quantity == D("0.15")
    assert "instrument_projected_notional" in notional_decision.binding_constraints


def test_rounding_minimum_quantity_and_minimum_notional_rejections() -> None:
    coarse = InstrumentRiskRules(
        quantity_step=D("1"),
        min_quantity=D("0.01"),
        min_notional=D("1"),
        contract_size=D("1"),
        max_leverage=D("3"),
    )
    decision = RiskCalculator().evaluate(_request(instrument=coarse))
    assert decision.reject_reason is RiskRejectReason.ZERO_QUANTITY_AFTER_ROUNDING

    min_quantity = replace(
        coarse,
        quantity_step=D("0.01"),
        min_quantity=D("0.30"),
    )
    decision = RiskCalculator().evaluate(_request(instrument=min_quantity))
    assert decision.reject_reason is RiskRejectReason.BELOW_MIN_QUANTITY

    min_notional = replace(min_quantity, min_quantity=D("0.01"), min_notional=D("30"))
    decision = RiskCalculator().evaluate(_request(instrument=min_notional))
    assert decision.reject_reason is RiskRejectReason.BELOW_MIN_NOTIONAL


def test_equity_margin_fallback_and_portfolio_risk_binding() -> None:
    portfolio = PortfolioRiskSnapshot(
        gross_notional=D("100"),
        symbol_notional=D("100"),
        open_risk_amount=D("29"),
        open_positions=10,
    )
    decision = RiskCalculator().evaluate(_request(available_margin=None, portfolio=portfolio))
    assert decision.status is RiskDecisionStatus.APPROVED
    assert "portfolio_risk" in decision.binding_constraints
