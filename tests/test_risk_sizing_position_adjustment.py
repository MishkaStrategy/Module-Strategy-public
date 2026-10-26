from decimal import Decimal

import pytest

from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    PositionAdjustment,
    RiskCalculator,
    RiskRejectReason,
    RiskRequest,
    TradeSide,
)


def make_request(
    *,
    contract_size: str = "1",
    equity: str = "200000",
    requested_risk_fraction: str = "0.005",
    requested_quantity: str = "0.25",
    projected_average: str = "59800",
    current_risk: str = "3000",
    projected_risk: str = "3500",
    current_notional: str = "60000",
    projected_notional: str = "73750",
) -> RiskRequest:
    quantity = Decimal(requested_quantity)
    adjustment = PositionAdjustment(
        adjustment_id="adj-1",
        position_id="pos-1",
        kind="position_increase",
        opens_new_position=False,
        current_quantity=Decimal("1"),
        requested_quantity=quantity,
        maximum_quantity=quantity,
        projected_quantity=Decimal("1") + quantity,
        current_average_price=Decimal("60000"),
        projected_average_price=Decimal(projected_average),
        valuation_price=Decimal("60000"),
        current_total_risk=Decimal(current_risk),
        projected_total_risk=Decimal(projected_risk),
        current_notional=Decimal(current_notional),
        projected_notional=Decimal(projected_notional),
    )
    return RiskRequest(
        request_id="risk-1",
        symbol="BTCUSDT",
        side=TradeSide.BUY,
        entry_price=Decimal("59000"),
        stop_loss=Decimal("57000"),
        equity=Decimal(equity),
        instrument=InstrumentRiskRules(
            quantity_step=Decimal("0.01"),
            contract_size=Decimal(contract_size),
            max_leverage=Decimal("3"),
        ),
        portfolio=PortfolioRiskSnapshot(
            gross_notional=Decimal(current_notional),
            symbol_notional=Decimal(current_notional),
            open_risk_amount=Decimal(current_risk),
            open_positions=10,
        ),
        requested_risk_fraction=Decimal(requested_risk_fraction),
        available_margin=Decimal(equity),
        position_adjustment=adjustment,
    )


def test_full_existing_position_increase_does_not_consume_max_positions() -> None:
    decision = RiskCalculator().evaluate(make_request())
    assert decision.approved
    assert decision.quantity == Decimal("0.25")
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.approved_quantity == Decimal("0.25")
    assert decision.position_adjustment.projected_total_quantity == Decimal("1.25")
    assert decision.position_adjustment.projected_average_price == Decimal("59800")
    assert decision.position_adjustment.projected_notional == Decimal("73750")
    assert decision.position_adjustment.projected_total_risk == Decimal("3500")
    assert decision.position_adjustment.opens_new_position is False


def test_partial_approval_is_delta_quantity() -> None:
    decision = RiskCalculator().evaluate(make_request(requested_risk_fraction="0.001"))
    assert decision.approved
    assert decision.quantity == Decimal("0.10")
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.approved_quantity == Decimal("0.10")
    assert decision.position_adjustment.projected_total_quantity == Decimal("1.10")
    assert decision.position_adjustment.projected_average_price == Decimal(
        "59909.090909090909090909090909090909090909090909091"
    )


def test_derivative_contract_size_applies_to_projected_geometry() -> None:
    decision = RiskCalculator().evaluate(
        make_request(
            contract_size="10",
            equity="2000000",
            current_risk="30000",
            projected_risk="35000",
            current_notional="600000",
            projected_notional="737500",
        )
    )
    assert decision.approved
    assert decision.quantity == Decimal("0.25")
    assert decision.position_adjustment is not None
    assert decision.position_adjustment.projected_notional == Decimal("737500")
    assert decision.position_adjustment.projected_total_risk == Decimal("35000")


def test_tampered_projected_geometry_fails_closed() -> None:
    decision = RiskCalculator().evaluate(make_request(projected_notional="73751"))
    assert not decision.approved
    assert decision.reject_reason is RiskRejectReason.INVALID_POSITION_ADJUSTMENT
    assert "projected_notional" in decision.message


def test_top_level_and_legacy_adjustment_evidence_must_agree() -> None:
    payload = dict(make_request().to_mapping())
    payload["metadata"] = {
        "adjustment_id": "different",
        "position_id": "pos-1",
        "adjustment_kind": "position_increase",
        "opens_new_position": False,
        "current_quantity": "1",
        "requested_delta_quantity": "0.25",
        "maximum_delta_quantity": "0.25",
        "projected_total_quantity": "1.25",
        "current_average_price": "60000",
        "projected_average_price": "59800",
        "current_total_risk": "3000",
        "projected_total_risk": "3500",
        "current_notional": "60000",
        "projected_notional": "73750",
    }
    with pytest.raises(ValueError, match="disagree"):
        RiskRequest.from_mapping(payload)


def test_legacy_metadata_is_typed_and_enforced() -> None:
    payload = dict(make_request().to_mapping())
    adjustment = dict(payload.pop("position_adjustment"))
    adjustment.pop("valuation_price")
    payload["metadata"] = {
        "adjustment_id": adjustment["adjustment_id"],
        "position_id": adjustment["position_id"],
        "adjustment_kind": adjustment["kind"],
        "opens_new_position": False,
        "current_quantity": adjustment["current_quantity"],
        "requested_delta_quantity": adjustment["requested_quantity"],
        "maximum_delta_quantity": adjustment["maximum_quantity"],
        "projected_total_quantity": adjustment["projected_quantity"],
        "current_average_price": adjustment["current_average_price"],
        "projected_average_price": adjustment["projected_average_price"],
        "current_total_risk": adjustment["current_total_risk"],
        "projected_total_risk": adjustment["projected_total_risk"],
        "current_notional": adjustment["current_notional"],
        "projected_notional": adjustment["projected_notional"],
    }
    parsed = RiskRequest.from_mapping(payload)
    assert parsed.position_adjustment is not None
    assert parsed.position_adjustment.adjustment_id == "adj-1"
    assert RiskCalculator().evaluate(parsed).approved
