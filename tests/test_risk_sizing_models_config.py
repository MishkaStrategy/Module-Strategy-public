from decimal import Decimal

import pytest

from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    RiskDecision,
    RiskDecisionStatus,
    RiskRejectReason,
    RiskRequest,
    RiskSizingConfig,
    TradeSide,
)


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "request_id": "r1",
        "symbol": "BTCUSDT",
        "side": TradeSide.BUY.value,
        "entry_price": "100",
        "stop_loss": "99",
        "equity": "10000",
        "requested_risk_fraction": "0.005",
        "instrument": {
            "quantity_step": "0.001",
            "min_quantity": "0.001",
            "min_notional": "5",
            "tick_size": "0.1",
            "contract_size": "1",
            "max_leverage": "20",
        },
        "portfolio": {
            "gross_notional": "0",
            "symbol_notional": "0",
            "open_risk_amount": "0",
            "open_positions": 0,
        },
    }


def test_config_defaults_are_safe() -> None:
    cfg = RiskSizingConfig()
    assert cfg.default_risk_fraction == Decimal("0.005")
    assert cfg.max_trade_notional_multiple <= cfg.max_portfolio_notional_multiple


@pytest.mark.parametrize("field", ["allow_risk_reduction", "require_price_alignment"])
def test_config_rejects_non_boolean_flags(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        RiskSizingConfig.from_mapping({field: 1})


@pytest.mark.parametrize("field", ["max_positions", "max_processed_requests"])
def test_config_rejects_boolean_integer_fields(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        RiskSizingConfig.from_mapping({field: True})


def test_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown"):
        RiskSizingConfig.from_mapping({"magic": 1})


def test_instrument_rejects_zero_maximums() -> None:
    with pytest.raises(ValueError, match="max_quantity"):
        InstrumentRiskRules(quantity_step=Decimal("0.1"), max_quantity=Decimal("0"))
    with pytest.raises(ValueError, match="max_notional"):
        InstrumentRiskRules(quantity_step=Decimal("0.1"), max_notional=Decimal("0"))


def test_portfolio_open_positions_is_strict_integer() -> None:
    with pytest.raises(ValueError, match="integer"):
        PortfolioRiskSnapshot.from_mapping({"open_positions": "2"})
    with pytest.raises(ValueError, match="integer"):
        PortfolioRiskSnapshot.from_mapping({"open_positions": True})


def test_request_rejects_unknown_nested_fields() -> None:
    payload = valid_payload()
    instrument = payload["instrument"]
    assert isinstance(instrument, dict)
    instrument["exchange_name"] = "binance"
    with pytest.raises(ValueError, match="unknown instrument"):
        RiskRequest.from_mapping(payload)


def test_decision_invariants() -> None:
    with pytest.raises(ValueError, match="positive quantity"):
        RiskDecision("r", "BTCUSDT", RiskDecisionStatus.APPROVED)
    with pytest.raises(ValueError, match="cannot have"):
        RiskDecision(
            "r",
            "BTCUSDT",
            RiskDecisionStatus.APPROVED,
            quantity=Decimal("1"),
            reject_reason=RiskRejectReason.INVALID_REQUEST,
        )


def test_nan_and_infinity_are_rejected() -> None:
    payload = valid_payload()
    payload["entry_price"] = "NaN"
    with pytest.raises(ValueError, match="finite"):
        RiskRequest.from_mapping(payload)
    payload = valid_payload()
    payload["equity"] = "Infinity"
    with pytest.raises(ValueError, match="finite"):
        RiskRequest.from_mapping(payload)


def test_direct_request_requires_typed_side_and_contracts() -> None:
    rules = InstrumentRiskRules(quantity_step=Decimal("0.1"))
    with pytest.raises(ValueError, match="TradeSide"):
        RiskRequest("r", "BTCUSDT", "buy", Decimal("100"), Decimal("99"), Decimal("1000"), rules)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="InstrumentRiskRules"):
        RiskRequest(
            "r",
            "BTCUSDT",
            TradeSide.BUY,
            Decimal("100"),
            Decimal("99"),
            Decimal("1000"),
            object(),
        )  # type: ignore[arg-type]


def test_request_metadata_is_deeply_immutable() -> None:
    request = RiskRequest.from_mapping({**valid_payload(), "metadata": {"nested": [1, {"x": 2}]}})
    with pytest.raises(TypeError):
        request.metadata["new"] = 1  # type: ignore[index]
    nested = request.metadata["nested"]
    assert isinstance(nested, tuple)
    with pytest.raises(TypeError):
        nested[1]["x"] = 3  # type: ignore[index]


def test_request_rejects_blank_setup_id() -> None:
    with pytest.raises(ValueError, match="setup_id"):
        RiskRequest.from_mapping({**valid_payload(), "setup_id": " "})


def test_decision_diagnostics_are_deeply_immutable() -> None:
    decision = RiskDecision(
        "r",
        "BTCUSDT",
        RiskDecisionStatus.REJECTED,
        reject_reason=RiskRejectReason.INVALID_REQUEST,
        diagnostics={"nested": {"x": 1}},
    )
    with pytest.raises(TypeError):
        decision.diagnostics["nested"]["x"] = 2  # type: ignore[index]


def test_decision_rejects_duplicate_binding_constraints() -> None:
    with pytest.raises(ValueError, match="unique"):
        RiskDecision(
            "r",
            "BTCUSDT",
            RiskDecisionStatus.APPROVED,
            quantity=Decimal("1"),
            risk_budget=Decimal("1"),
            binding_constraints=("risk", "risk"),
        )


def test_decimal_resource_bounds() -> None:
    payload = valid_payload()
    payload["equity"] = "1e1001"
    with pytest.raises(ValueError, match="unsupported"):
        RiskRequest.from_mapping(payload)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_risk_fraction": "0"},
        {"max_portfolio_risk_fraction": "1.1", "max_portfolio_notional_multiple": "5"},
        {"max_trade_notional_multiple": "0"},
        {"max_trade_notional_multiple": "6", "max_portfolio_notional_multiple": "5"},
        {"max_symbol_notional_multiple": "6", "max_portfolio_notional_multiple": "5"},
        {"max_positions": 0},
        {"max_processed_requests": 0},
    ],
)
def test_config_invalid_combinations(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RiskSizingConfig.from_mapping(kwargs)


def test_config_decimal_conversion_path() -> None:
    cfg = RiskSizingConfig.from_mapping(
        {
            "default_risk_fraction": "0.006",
            "max_risk_fraction": "0.03",
        }
    )
    assert cfg.default_risk_fraction == Decimal("0.006")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quantity_step": Decimal("0")},
        {"quantity_step": Decimal("0.1"), "min_quantity": Decimal("-1")},
        {"quantity_step": Decimal("0.1"), "contract_size": Decimal("0")},
        {
            "quantity_step": Decimal("0.1"),
            "min_quantity": Decimal("2"),
            "max_quantity": Decimal("1"),
        },
        {
            "quantity_step": Decimal("0.1"),
            "min_notional": Decimal("2"),
            "max_notional": Decimal("1"),
        },
        {"quantity_step": Decimal("0.1"), "tick_size": Decimal("0")},
        {"quantity_step": Decimal("0.1"), "max_leverage": Decimal("0")},
    ],
)
def test_instrument_invalid_rules(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        InstrumentRiskRules(**kwargs)  # type: ignore[arg-type]


def test_instrument_mapping_all_optional_paths() -> None:
    rules = InstrumentRiskRules.from_mapping(
        {
            "quantity_step": "0.01",
            "max_quantity": "10",
            "max_notional": "1000",
            "tick_size": "0.1",
            "max_leverage": "5",
        }
    )
    assert rules.max_quantity == Decimal("10")
    with pytest.raises(ValueError, match="unknown instrument"):
        InstrumentRiskRules.from_mapping({"quantity_step": "1", "x": 1})


def test_portfolio_negative_and_unknown_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        PortfolioRiskSnapshot(gross_notional=Decimal("-1"))
    with pytest.raises(ValueError, match="unknown portfolio"):
        PortfolioRiskSnapshot.from_mapping({"x": 1})


def test_decimal_bool_and_invalid_input_rejected() -> None:
    payload = valid_payload()
    payload["equity"] = True
    with pytest.raises(ValueError, match="numeric"):
        RiskRequest.from_mapping(payload)
    payload = valid_payload()
    payload["equity"] = object()
    with pytest.raises(ValueError, match="valid decimal"):
        RiskRequest.from_mapping(payload)


def test_request_mapping_contract_errors() -> None:
    base = valid_payload()
    variants = [
        {**base, "schema_version": "2"},
        {**base, "side": "hold"},
        {**base, "instrument": []},
        {**base, "portfolio": []},
        {**base, "metadata": []},
        {**base, "available_margin": "-1"},
        {**base, "unknown": 1},
    ]
    for variant in variants:
        with pytest.raises(ValueError):
            RiskRequest.from_mapping(variant)


def test_metadata_types_are_strict_and_serializable() -> None:
    from datetime import datetime, timezone

    payload = {
        **valid_payload(),
        "metadata": {"values": [1, 1.5, Decimal("2"), TradeSide.BUY, datetime.now(timezone.utc)]},
    }
    request = RiskRequest.from_mapping(payload)
    assert isinstance(request.metadata["values"], tuple)
    for bad in (
        {1: "x"},
        {"x": {1, 2}},
        {"x": float("nan")},
        {"x": datetime.now()},
    ):
        with pytest.raises(ValueError):
            RiskRequest.from_mapping({**valid_payload(), "metadata": bad})


def test_decision_full_invariant_matrix() -> None:
    invalids = [
        {
            "request_id": "",
            "symbol": "BTC",
            "status": RiskDecisionStatus.REJECTED,
            "reject_reason": RiskRejectReason.INVALID_REQUEST,
        },
        {
            "request_id": "r",
            "symbol": "",
            "status": RiskDecisionStatus.REJECTED,
            "reject_reason": RiskRejectReason.INVALID_REQUEST,
        },
        {
            "request_id": "r",
            "symbol": "BTC",
            "status": "rejected",
            "reject_reason": RiskRejectReason.INVALID_REQUEST,
        },
        {
            "request_id": "r",
            "symbol": "BTC",
            "status": RiskDecisionStatus.REJECTED,
            "reject_reason": "invalid_request",
        },
        {
            "request_id": "r",
            "symbol": "BTC",
            "status": RiskDecisionStatus.REJECTED,
            "reject_reason": RiskRejectReason.INVALID_REQUEST,
            "quantity": Decimal("1"),
        },
        {
            "request_id": "r",
            "symbol": "BTC",
            "status": RiskDecisionStatus.APPROVED,
            "quantity": Decimal("1"),
            "risk_budget": Decimal("0"),
            "actual_risk_amount": Decimal("1"),
        },
        {
            "request_id": "r",
            "symbol": "BTC",
            "status": RiskDecisionStatus.REJECTED,
            "reject_reason": RiskRejectReason.INVALID_REQUEST,
            "schema_version": "2",
        },
        {
            "request_id": "r",
            "symbol": "BTC",
            "status": RiskDecisionStatus.REJECTED,
            "reject_reason": RiskRejectReason.INVALID_REQUEST,
            "binding_constraints": ("",),
        },
    ]
    for values in invalids:
        with pytest.raises(ValueError):
            RiskDecision(**values)  # type: ignore[arg-type]


def test_decision_to_mapping_complete() -> None:
    decision = RiskDecision(
        "r",
        "BTCUSDT",
        RiskDecisionStatus.APPROVED,
        quantity=Decimal("1"),
        raw_quantity=Decimal("1.1"),
        risk_fraction=Decimal("0.01"),
        risk_budget=Decimal("2"),
        actual_risk_amount=Decimal("1"),
        actual_risk_fraction=Decimal("0.005"),
        notional=Decimal("100"),
        required_leverage=Decimal("0.1"),
        binding_constraints=("risk",),
    )
    mapped = decision.to_mapping()
    assert mapped["schema_version"] == "1"
    assert mapped["approved"] is True
    assert mapped["reject_reason"] is None


def test_direct_config_integer_fields_are_strict() -> None:
    with pytest.raises(ValueError, match="integer"):
        RiskSizingConfig(max_positions=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        RiskSizingConfig(max_processed_requests="10")  # type: ignore[arg-type]


def test_direct_string_identifiers_are_strict() -> None:
    rules = InstrumentRiskRules(quantity_step=Decimal("1"))
    with pytest.raises(ValueError, match="request_id"):
        RiskRequest(1, "BTC", TradeSide.BUY, Decimal("2"), Decimal("1"), Decimal("10"), rules)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="setup_id"):
        RiskRequest(
            "r",
            "BTC",
            TradeSide.BUY,
            Decimal("2"),
            Decimal("1"),
            Decimal("10"),
            rules,
            setup_id=1,
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="decision request_id"):
        RiskDecision(
            1,
            "BTC",
            RiskDecisionStatus.REJECTED,
            reject_reason=RiskRejectReason.INVALID_REQUEST,
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="binding_constraints"):
        RiskDecision(
            "r",
            "BTC",
            RiskDecisionStatus.REJECTED,
            binding_constraints=["x"],
            reject_reason=RiskRejectReason.INVALID_REQUEST,
        )  # type: ignore[arg-type]


def test_request_and_nested_dto_mapping_roundtrip() -> None:
    original = RiskRequest.from_mapping(
        {
            **valid_payload(),
            "setup_id": "setup-1",
            "metadata": {"x": [1, 2]},
        }
    )
    restored = RiskRequest.from_mapping(original.to_mapping())
    assert restored == original
    assert InstrumentRiskRules.from_mapping(original.instrument.to_mapping()) == original.instrument
    assert PortfolioRiskSnapshot.from_mapping(original.portfolio.to_mapping()) == original.portfolio


def test_decision_mapping_roundtrip_for_approved_and_rejected() -> None:
    approved = RiskDecision(
        "r",
        "BTCUSDT",
        RiskDecisionStatus.APPROVED,
        quantity=Decimal("1"),
        raw_quantity=Decimal("1.1"),
        risk_fraction=Decimal("0.01"),
        risk_budget=Decimal("2"),
        actual_risk_amount=Decimal("1"),
        actual_risk_fraction=Decimal("0.005"),
        notional=Decimal("100"),
        required_leverage=Decimal("0.1"),
        binding_constraints=("risk",),
        setup_id="setup-1",
    )
    assert RiskDecision.from_mapping(approved.to_mapping()) == approved
    rejected = RiskDecision(
        "r2",
        "BTCUSDT",
        RiskDecisionStatus.REJECTED,
        reject_reason=RiskRejectReason.NO_RISK_CAPACITY,
        message="no capacity",
    )
    assert RiskDecision.from_mapping(rejected.to_mapping()) == rejected


@pytest.mark.parametrize(
    "update",
    [
        {"unknown": 1},
        {"status": "bad"},
        {"approved": False},
        {"reject_reason": "bad"},
        {"binding_constraints": "risk"},
        {"diagnostics": []},
    ],
)
def test_decision_mapping_rejects_contract_violations(
    update: dict[str, object],
) -> None:
    decision = RiskDecision(
        "r",
        "BTCUSDT",
        RiskDecisionStatus.APPROVED,
        quantity=Decimal("1"),
        risk_budget=Decimal("1"),
    ).to_mapping()
    payload = dict(decision)
    payload.update(update)
    with pytest.raises(ValueError):
        RiskDecision.from_mapping(payload)
