from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    PositionAdjustment,
    PositionAdjustmentDecision,
    RiskCalculator,
    RiskRejectReason,
    RiskRequest,
    RiskSizingConfig,
    TradeSide,
)
from strategy_modules.risk_sizing._canonical import (
    canonicalize,
    fingerprint,
    manifest_number,
)
from strategy_modules.risk_sizing.adjustment import _first, _required_identifier
from strategy_modules.risk_sizing import adjustment_calculator


class Probe(Enum):
    VALUE = "value"


def _d(value: str | int) -> Decimal:
    return Decimal(str(value))


def make_adjustment(
    *,
    side: TradeSide = TradeSide.BUY,
    entry_price: str = "90",
    stop_loss: str = "80",
    contract_size: str = "1",
    current_quantity: str = "1",
    requested_quantity: str = "0.25",
    maximum_quantity: str | None = None,
    current_average_price: str = "100",
    valuation_price: str | None = "100",
    **overrides: Any,
) -> PositionAdjustment:
    current_quantity_value = _d(current_quantity)
    requested_quantity_value = _d(requested_quantity)
    maximum_quantity_value = _d(
        requested_quantity if maximum_quantity is None else maximum_quantity
    )
    entry = _d(entry_price)
    stop = _d(stop_loss)
    current_average = _d(current_average_price)
    contract = _d(contract_size)
    projected_quantity = current_quantity_value + requested_quantity_value
    projected_average = (
        current_average * current_quantity_value + entry * requested_quantity_value
    ) / projected_quantity
    current_signed_risk = (
        (current_average - stop) * current_quantity_value
        if side is TradeSide.BUY
        else (stop - current_average) * current_quantity_value
    )
    added_signed_risk = (
        (entry - stop) * requested_quantity_value
        if side is TradeSide.BUY
        else (stop - entry) * requested_quantity_value
    )
    valuation = None if valuation_price is None else _d(valuation_price)
    current_notional_price = current_average if valuation is None else valuation
    values: dict[str, Any] = {
        "adjustment_id": "adj-coverage",
        "position_id": "pos-coverage",
        "kind": "position_increase",
        "opens_new_position": False,
        "current_quantity": current_quantity_value,
        "requested_quantity": requested_quantity_value,
        "maximum_quantity": maximum_quantity_value,
        "projected_quantity": projected_quantity,
        "current_average_price": current_average,
        "projected_average_price": projected_average,
        "valuation_price": valuation,
        "current_total_risk": max(Decimal("0"), current_signed_risk) * contract,
        "projected_total_risk": max(Decimal("0"), current_signed_risk + added_signed_risk)
        * contract,
        "current_notional": current_quantity_value * current_notional_price * contract,
        "projected_notional": projected_quantity * entry * contract,
    }
    values.update(overrides)
    return PositionAdjustment(**values)


_NO_ADJUSTMENT = object()


def make_request(
    *,
    adjustment: PositionAdjustment | None | object = _NO_ADJUSTMENT,
    side: TradeSide = TradeSide.BUY,
    entry_price: str = "90",
    stop_loss: str = "80",
    equity: str = "1000",
    contract_size: str = "1",
    requested_quantity: str = "0.25",
    quantity_step: str = "0.01",
    min_quantity: str = "0.01",
    min_notional: str = "1",
    max_quantity: str | None = None,
    max_notional: str | None = None,
    max_leverage: str = "3",
    gross_notional: str | None = None,
    symbol_notional: str | None = None,
    open_risk_amount: str | None = None,
    available_margin: str | None = "1000",
    requested_risk_fraction: str = "0.01",
) -> RiskRequest:
    if adjustment is _NO_ADJUSTMENT:
        adjustment_value: PositionAdjustment | None = make_adjustment(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            contract_size=contract_size,
            requested_quantity=requested_quantity,
        )
    else:
        adjustment_value = adjustment  # type: ignore[assignment]
    current_notional = (
        Decimal("0") if adjustment_value is None else adjustment_value.current_notional
    )
    current_risk = Decimal("0") if adjustment_value is None else adjustment_value.current_total_risk
    instrument = InstrumentRiskRules(
        quantity_step=_d(quantity_step),
        min_quantity=_d(min_quantity),
        min_notional=_d(min_notional),
        tick_size=Decimal("0.1"),
        contract_size=_d(contract_size),
        max_quantity=None if max_quantity is None else _d(max_quantity),
        max_notional=None if max_notional is None else _d(max_notional),
        max_leverage=_d(max_leverage),
    )
    return RiskRequest(
        request_id="risk-coverage",
        symbol="BTCUSDT",
        side=side,
        entry_price=_d(entry_price),
        stop_loss=_d(stop_loss),
        equity=_d(equity),
        instrument=instrument,
        portfolio=PortfolioRiskSnapshot(
            gross_notional=(current_notional if gross_notional is None else _d(gross_notional)),
            symbol_notional=(current_notional if symbol_notional is None else _d(symbol_notional)),
            open_risk_amount=(current_risk if open_risk_amount is None else _d(open_risk_amount)),
            open_positions=10 if adjustment_value is not None else 0,
        ),
        requested_risk_fraction=_d(requested_risk_fraction),
        available_margin=(None if available_margin is None else _d(available_margin)),
        position_adjustment=adjustment_value,
    )


def make_decision(**overrides: Any) -> PositionAdjustmentDecision:
    values: dict[str, Any] = {
        "adjustment_id": "adj-coverage",
        "position_id": "pos-coverage",
        "requested_quantity": Decimal("0.25"),
        "approved_quantity": Decimal("0.1"),
        "projected_total_quantity": Decimal("1.1"),
        "projected_average_price": Decimal("99"),
        "projected_notional": Decimal("99"),
        "projected_leverage": Decimal("0.099"),
        "projected_total_risk": Decimal("21"),
    }
    values.update(overrides)
    return PositionAdjustmentDecision(**values)


def test_canonicalization_rejects_ambiguous_and_non_finite_values() -> None:
    assert canonicalize(None) is None
    assert canonicalize(1.5) == {"$float": "1.5"}
    assert canonicalize(Decimal("1.25")) == {"$decimal": "1.25"}
    aware = datetime(2026, 7, 21, tzinfo=UTC)
    assert canonicalize(aware) == {"$datetime": aware.isoformat()}
    assert canonicalize(Probe.VALUE)["$enum"].endswith(":value")
    assert canonicalize([Decimal("1"), (2,)]) == [
        {"$decimal": "1"},
        [2],
    ]
    with pytest.raises(ValueError, match="non-finite float"):
        canonicalize(float("inf"))
    with pytest.raises(ValueError, match="non-finite Decimal"):
        canonicalize(Decimal("NaN"))
    with pytest.raises(ValueError, match="timezone-aware"):
        canonicalize(datetime(2026, 7, 21))
    with pytest.raises(ValueError, match="keys must be strings"):
        canonicalize({1: "bad"})
    with pytest.raises(ValueError, match="unsupported value type"):
        canonicalize(object())
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})
    with pytest.raises(ValueError, match="precision"):
        manifest_number(Decimal("0.123456789012345678901"), "probe")


@pytest.mark.parametrize("value", [None, "", " ", 7])
def test_required_identifier_rejects_blank_or_non_string(value: object) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        _required_identifier(value, "identifier")


def test_required_identifier_rejects_surrounding_whitespace() -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        _required_identifier(" identifier ", "identifier")


def test_alias_helper_requires_one_consistent_value() -> None:
    with pytest.raises(ValueError, match="missing"):
        _first({}, "requested_quantity", "desired_quantity")
    with pytest.raises(ValueError, match="conflicting aliases"):
        _first(
            {"requested_quantity": "1", "desired_quantity": "2"},
            "requested_quantity",
            "desired_quantity",
        )
    assert (
        _first(
            {"requested_quantity": "1", "desired_quantity": "1"},
            "requested_quantity",
            "desired_quantity",
        )
        == "1"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": "2"}, "schema_version"),
        ({"kind": "unsupported"}, "kind"),
        ({"opens_new_position": "false"}, "boolean"),
        ({"opens_new_position": True}, "cannot open"),
        ({"current_quantity": Decimal("-1")}, "negative"),
        ({"valuation_price": Decimal("0")}, "valuation_price"),
        ({"current_notional": Decimal("0")}, "must be positive"),
        ({"maximum_quantity": Decimal("0.1")}, "maximum_quantity"),
        ({"projected_quantity": Decimal("2")}, "projected_quantity"),
    ],
)
def test_position_adjustment_direct_validation(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_adjustment(**overrides)


def test_position_adjustment_mapping_validation_and_legacy_aliases() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        PositionAdjustment.from_mapping([])  # type: ignore[arg-type]

    mapping = dict(make_adjustment().to_mapping())
    with pytest.raises(ValueError, match="unknown"):
        PositionAdjustment.from_mapping({**mapping, "unknown": 1})
    with pytest.raises(ValueError, match="boolean"):
        PositionAdjustment.from_mapping({**mapping, "opens_new_position": 1})

    without_valuation = dict(mapping)
    without_valuation.pop("valuation_price")
    with pytest.raises(ValueError, match="valuation_price"):
        PositionAdjustment.from_mapping(without_valuation)

    with pytest.raises(ValueError, match="conflicting aliases"):
        PositionAdjustment.from_mapping({**mapping, "requested_delta_quantity": "0.5"})

    legacy = dict(without_valuation)
    legacy["unknown_legacy_evidence"] = "ignored"
    parsed_legacy = PositionAdjustment.from_mapping(
        legacy,
        legacy_metadata=True,
    )
    assert parsed_legacy.valuation_price is None

    aliases = dict(mapping)
    aliases["adjustment_kind"] = aliases.pop("kind")
    aliases["requested_delta_quantity"] = aliases.pop("requested_quantity")
    aliases["maximum_delta_quantity"] = aliases.pop("maximum_quantity")
    aliases["projected_total_quantity"] = aliases.pop("projected_quantity")
    aliases["current_average_entry_price"] = aliases.pop("current_average_price")
    aliases["projected_average_entry_price"] = aliases.pop("projected_average_price")
    aliases["current_market_price"] = aliases.pop("valuation_price")
    parsed_aliases = PositionAdjustment.from_mapping(aliases)
    assert parsed_aliases == make_adjustment()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": "2"}, "schema_version"),
        ({"opens_new_position": True}, "existing position"),
        ({"opens_new_position": "false"}, "existing position"),
        ({"approved_quantity": Decimal("-1")}, "negative"),
        ({"requested_quantity": Decimal("0")}, "must be positive"),
        ({"approved_quantity": Decimal("0.3")}, "cannot exceed"),
        ({"projected_total_quantity": Decimal("0")}, "must be positive"),
        ({"projected_average_price": Decimal("0")}, "must be positive"),
    ],
)
def test_position_adjustment_decision_direct_validation(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_decision(**overrides)


def test_position_adjustment_decision_mapping_contract() -> None:
    decision = make_decision()
    assert PositionAdjustmentDecision.from_mapping(decision.to_mapping()) == decision
    with pytest.raises(ValueError, match="unknown"):
        PositionAdjustmentDecision.from_mapping({**decision.to_mapping(), "unknown": 1})
    with pytest.raises(ValueError, match="boolean"):
        PositionAdjustmentDecision.from_mapping({**decision.to_mapping(), "opens_new_position": 1})
    without_schema = dict(decision.to_mapping())
    without_schema.pop("schema_version")
    with pytest.raises(ValueError, match="schema_version"):
        PositionAdjustmentDecision.from_mapping(without_schema)


def test_adjustment_calculator_private_fail_closed_branches() -> None:
    assert adjustment_calculator._floor_to_step(Decimal("-1"), Decimal("0.1"), (1, 10)) == Decimal(
        "0"
    )
    assert adjustment_calculator._signed_risk_before_contract(
        TradeSide.SELL,
        Decimal("100"),
        Decimal("110"),
        Decimal("2"),
    ) == Decimal("20")
    assert adjustment_calculator._projected_total_risk(
        side=TradeSide.BUY,
        current_average_price=Decimal("100"),
        current_quantity=Decimal("1"),
        entry_price=Decimal("105"),
        approved_quantity=Decimal("1"),
        stop_loss=Decimal("110"),
        contract_size=Decimal("10"),
    ) == Decimal("0")

    no_valuation = make_adjustment(valuation_price=None)
    assert adjustment_calculator._valuation_price(
        no_valuation,
        Decimal("1"),
    ) == Decimal("100")

    no_adjustment = make_request(adjustment=None)
    with pytest.raises(RuntimeError, match="position adjustment"):
        adjustment_calculator._decision_geometry(no_adjustment, Decimal("0"))
    with pytest.raises(RuntimeError, match="position adjustment"):
        adjustment_calculator._capacity_rows(
            request=no_adjustment,
            config=RiskSizingConfig(),
            current_notional=Decimal("0"),
            additional_risk_budget=Decimal("1"),
            current_risk=Decimal("0"),
            leverage_limit=Decimal("1"),
            margin_base=Decimal("1000"),
        )
    with pytest.raises(RuntimeError, match="position adjustment"):
        adjustment_calculator.evaluate_position_adjustment(
            RiskCalculator(),
            no_adjustment,
            Decimal("0.01"),
        )
    assert (
        adjustment_calculator._geometry_error(
            no_adjustment,
            projected_quantity=Decimal("0"),
            projected_average=Decimal("0"),
            current_notional=Decimal("0"),
            projected_notional=Decimal("0"),
            current_risk=Decimal("0"),
            projected_risk=Decimal("0"),
        )
        == "position adjustment evidence is missing"
    )

    request = make_request()
    zero = adjustment_calculator._decision_geometry(request, Decimal("0"))
    assert zero.approved_quantity == Decimal("0")
    assert zero.projected_notional == request.position_adjustment.current_notional
    assert zero.projected_average_price == Decimal("100")
    assert (
        adjustment_calculator._risk_capacity(
            adjustment=request.position_adjustment,
            side=TradeSide.BUY,
            entry_price=Decimal("80"),
            stop_loss=Decimal("80"),
            contract_size=Decimal("1"),
            current_risk=Decimal("20"),
            additional_risk_budget=Decimal("1"),
        )
        == request.position_adjustment.requested_quantity
    )


def test_capacity_rows_include_optional_instrument_limits() -> None:
    request = make_request(max_quantity="2", max_notional="500")
    rows = dict(
        adjustment_calculator._capacity_rows(
            request=request,
            config=RiskSizingConfig(),
            current_notional=request.position_adjustment.current_notional,
            additional_risk_budget=Decimal("10"),
            current_risk=request.position_adjustment.current_total_risk,
            leverage_limit=Decimal("3"),
            margin_base=Decimal("1000"),
        )
    )
    assert "instrument_projected_quantity" in rows
    assert "instrument_projected_notional" in rows


def test_adjustment_rejects_zero_margin() -> None:
    decision = RiskCalculator().evaluate(make_request(available_margin="0"))
    assert decision.reject_reason is RiskRejectReason.LEVERAGE_LIMIT


def test_adjustment_rejects_when_strict_risk_budget_is_too_small() -> None:
    config = RiskSizingConfig(
        max_portfolio_risk_fraction=Decimal("0.021"),
        allow_risk_reduction=False,
    )
    decision = RiskCalculator(config).evaluate(make_request(open_risk_amount="20.5"))
    assert decision.reject_reason is RiskRejectReason.NO_RISK_CAPACITY


def test_adjustment_rejects_exhausted_risk_capacity() -> None:
    config = RiskSizingConfig(max_portfolio_risk_fraction=Decimal("0.02"))
    decision = RiskCalculator(config).evaluate(make_request(open_risk_amount="20"))
    assert decision.reject_reason is RiskRejectReason.NO_RISK_CAPACITY


def test_adjustment_rejects_exhausted_projected_notional_capacity() -> None:
    config = RiskSizingConfig(max_trade_notional_multiple=Decimal("0.09"))
    decision = RiskCalculator(config).evaluate(make_request())
    assert decision.reject_reason is RiskRejectReason.PROJECTED_POSITION_LIMIT


def test_adjustment_zero_quantity_after_rounding() -> None:
    adjustment = make_adjustment(requested_quantity="0.005", current_average_price="90")
    decision = RiskCalculator().evaluate(
        make_request(
            adjustment=adjustment,
            requested_quantity="0.005",
            quantity_step="0.01",
            min_quantity="0.001",
        )
    )
    assert decision.reject_reason is RiskRejectReason.ZERO_QUANTITY_AFTER_ROUNDING


def test_adjustment_below_minimum_quantity() -> None:
    adjustment = make_adjustment(requested_quantity="0.05", current_average_price="90")
    decision = RiskCalculator().evaluate(
        make_request(
            adjustment=adjustment,
            requested_quantity="0.05",
            min_quantity="0.1",
        )
    )
    assert decision.reject_reason is RiskRejectReason.BELOW_MIN_QUANTITY


def test_adjustment_below_minimum_notional() -> None:
    adjustment = make_adjustment(requested_quantity="0.05", current_average_price="90")
    decision = RiskCalculator().evaluate(
        make_request(
            adjustment=adjustment,
            requested_quantity="0.05",
            min_notional="10",
        )
    )
    assert decision.reject_reason is RiskRejectReason.BELOW_MIN_NOTIONAL


def test_post_rounding_risk_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = adjustment_calculator._decision_geometry

    def inflated_risk(
        request: RiskRequest,
        approved_quantity: Decimal,
    ) -> PositionAdjustmentDecision:
        decision = original(request, approved_quantity)
        return replace(decision, projected_total_risk=Decimal("1000"))

    monkeypatch.setattr(
        adjustment_calculator,
        "_decision_geometry",
        inflated_risk,
    )
    decision = RiskCalculator().evaluate(make_request())
    assert decision.reject_reason is RiskRejectReason.NO_RISK_CAPACITY


def test_post_rounding_leverage_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = adjustment_calculator._decision_geometry

    def inflated_leverage(
        request: RiskRequest,
        approved_quantity: Decimal,
    ) -> PositionAdjustmentDecision:
        decision = original(request, approved_quantity)
        return replace(decision, projected_leverage=Decimal("99"))

    monkeypatch.setattr(
        adjustment_calculator,
        "_decision_geometry",
        inflated_leverage,
    )
    decision = RiskCalculator().evaluate(make_request())
    assert decision.reject_reason is RiskRejectReason.LEVERAGE_LIMIT


def test_adjustment_records_reduced_portfolio_risk_budget() -> None:
    config = RiskSizingConfig(max_portfolio_risk_fraction=Decimal("0.025"))
    decision = RiskCalculator(config).evaluate(make_request(open_risk_amount="20"))
    assert decision.approved
    assert "portfolio_risk" in decision.binding_constraints
