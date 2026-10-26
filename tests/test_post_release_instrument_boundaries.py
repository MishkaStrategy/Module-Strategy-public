from __future__ import annotations

import math

import pytest

from modular_core.instruments import (
    ExchangeRulesAdapter,
    InstrumentRuleError,
    InstrumentSpec,
)
from modular_core.models import OrderRequest, OrderType, Side


def _rules() -> ExchangeRulesAdapter:
    return ExchangeRulesAdapter(
        InstrumentSpec(
            "BTCUSDT",
            tick_size=0.1,
            quantity_step=0.001,
            min_quantity=0.001,
        )
    )


def test_exchange_rules_adapter_requires_instrument_spec() -> None:
    with pytest.raises(TypeError, match="spec must be InstrumentSpec"):
        ExchangeRulesAdapter(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "1", math.nan, math.inf, 0, -1])
def test_normalize_quantity_wraps_invalid_quantity_as_domain_error(
    value: object,
) -> None:
    with pytest.raises(InstrumentRuleError, match="quantity"):
        _rules().normalize_quantity(value, price=100.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "100", math.nan, math.inf, 0, -1])
def test_normalize_quantity_wraps_invalid_price_as_domain_error(
    value: object,
) -> None:
    with pytest.raises(InstrumentRuleError, match="price"):
        _rules().normalize_quantity(1.0, price=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("side", [True, "buy", object()])
def test_price_normalization_rejects_non_side_values(side: object) -> None:
    with pytest.raises(InstrumentRuleError, match="side must be Side"):
        _rules().normalize_entry_price(
            100.0,
            side,  # type: ignore[arg-type]
            OrderType.LIMIT,
        )


@pytest.mark.parametrize("order_type", [True, "limit", object()])
def test_entry_price_rejects_non_order_type_values(order_type: object) -> None:
    with pytest.raises(InstrumentRuleError, match="order_type must be OrderType"):
        _rules().normalize_entry_price(
            100.0,
            Side.BUY,
            order_type,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [True, "100", math.nan, math.inf, 0, -1])
@pytest.mark.parametrize("method", ["stop", "target"])
def test_protection_normalization_rejects_invalid_entry_price(
    value: object,
    method: str,
) -> None:
    rules = _rules()
    operation = rules.normalize_stop_price if method == "stop" else rules.normalize_target_price

    with pytest.raises(InstrumentRuleError, match="entry_price"):
        operation(
            95.0 if method == "stop" else 105.0,
            entry_price=value,  # type: ignore[arg-type]
            side=Side.BUY,
        )


@pytest.mark.parametrize("method", ["stop", "target"])
def test_protection_normalization_rejects_invalid_side(method: str) -> None:
    rules = _rules()
    operation = rules.normalize_stop_price if method == "stop" else rules.normalize_target_price

    with pytest.raises(InstrumentRuleError, match="side must be Side"):
        operation(
            95.0 if method == "stop" else 105.0,
            entry_price=100.0,
            side="buy",  # type: ignore[arg-type]
        )


def test_normalize_order_requires_order_request() -> None:
    with pytest.raises(TypeError, match="request must be OrderRequest"):
        _rules().normalize_order(object(), reference_price=100.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "100", math.nan, math.inf, 0, -1])
def test_normalize_order_rejects_invalid_reference_price(value: object) -> None:
    request = OrderRequest(
        Side.BUY,
        OrderType.MARKET,
        quantity=1.0,
    )

    with pytest.raises(InstrumentRuleError, match="reference_price"):
        _rules().normalize_order(
            request,
            reference_price=value,  # type: ignore[arg-type]
        )


def test_validate_post_only_requires_order_request() -> None:
    with pytest.raises(TypeError, match="request must be OrderRequest"):
        _rules().validate_post_only(
            object(),  # type: ignore[arg-type]
            reference_price=100.0,
        )


@pytest.mark.parametrize("value", [True, "100", math.nan, math.inf, 0, -1])
def test_validate_post_only_rejects_invalid_reference_price(value: object) -> None:
    request = OrderRequest(
        Side.BUY,
        OrderType.LIMIT,
        quantity=1.0,
        price=99.0,
        post_only=True,
    )

    with pytest.raises(InstrumentRuleError, match="reference_price"):
        _rules().validate_post_only(
            request,
            reference_price=value,  # type: ignore[arg-type]
        )
