from __future__ import annotations

import pytest

from modular_core import (
    ExchangeRulesAdapter,
    InstrumentRuleError,
    InstrumentSpec,
    OrderRequest,
    OrderType,
    Side,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"symbol": ""},
        {"symbol": "BTCUSDT", "tick_size": -0.1},
        {"symbol": "BTCUSDT", "contract_size": 0.0},
        {"symbol": "BTCUSDT", "max_quantity": 0.0},
        {"symbol": "BTCUSDT", "max_notional": 0.0},
    ],
)
def test_instrument_spec_rejects_invalid_constraints(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        InstrumentSpec(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(("quantity", "price"), [(0.0, 100.0), (1.0, 0.0)])
def test_quantity_requires_positive_inputs(quantity: float, price: float) -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT"))
    with pytest.raises(InstrumentRuleError, match="must be positive"):
        rules.normalize_quantity(quantity, price=price)


def test_quantity_precision_and_maximum_are_applied() -> None:
    rules = ExchangeRulesAdapter(
        InstrumentSpec(
            "BTCUSDT",
            quantity_step=0.001,
            quantity_precision=2,
            max_quantity=1.25,
        )
    )
    assert rules.normalize_quantity(2.345, price=100.0) == 1.25


def test_quantity_below_minimum_is_rejected() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", quantity_step=0.1, min_quantity=1.0))
    with pytest.raises(InstrumentRuleError, match="quantity below"):
        rules.normalize_quantity(0.9, price=100.0)


def test_quantity_below_minimum_notional_is_rejected() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", min_notional=100.0))
    with pytest.raises(InstrumentRuleError, match="notional below"):
        rules.normalize_quantity(0.5, price=100.0)


def test_quantity_is_capped_by_maximum_notional() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", quantity_step=0.1, max_notional=100.0))
    assert rules.normalize_quantity(2.0, price=100.0) == 1.0


def test_notional_cap_that_rounds_to_zero_is_rejected() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", quantity_step=1.0, max_notional=1.0))
    with pytest.raises(InstrumentRuleError, match="invalid after normalization"):
        rules.normalize_quantity(2.0, price=100.0)


@pytest.mark.parametrize(
    ("side", "order_type", "expected"),
    [
        (Side.SELL, OrderType.LIMIT, 100.1),
        (Side.BUY, OrderType.MARKET, 100.1),
        (Side.SELL, OrderType.MARKET, 100.0),
    ],
)
def test_entry_price_rounding_covers_side_and_order_modes(
    side: Side,
    order_type: OrderType,
    expected: float,
) -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", tick_size=0.1))
    assert rules.normalize_entry_price(100.09, side, order_type) == expected


@pytest.mark.parametrize(
    ("side", "price", "entry", "message"),
    [
        (Side.BUY, 100.01, 100.0, "long stop"),
        (Side.SELL, 99.99, 100.0, "short stop"),
    ],
)
def test_stop_geometry_rejects_wrong_side(
    side: Side,
    price: float,
    entry: float,
    message: str,
) -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", tick_size=0.1))
    with pytest.raises(InstrumentRuleError, match=message):
        rules.normalize_stop_price(price, entry_price=entry, side=side)


def test_short_stop_rounds_away_from_entry() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", tick_size=0.1))
    assert rules.normalize_stop_price(105.01, entry_price=100.0, side=Side.SELL) == 105.1


@pytest.mark.parametrize(
    ("side", "price", "entry", "message"),
    [
        (Side.BUY, 99.99, 100.0, "long target"),
        (Side.SELL, 100.01, 100.0, "short target"),
    ],
)
def test_target_geometry_rejects_wrong_side(
    side: Side,
    price: float,
    entry: float,
    message: str,
) -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", tick_size=0.1))
    with pytest.raises(InstrumentRuleError, match=message):
        rules.normalize_target_price(price, entry_price=entry, side=side)


def test_short_target_rounds_conservatively() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", tick_size=0.1))
    assert rules.normalize_target_price(94.01, entry_price=100.0, side=Side.SELL) == 94.1


def test_normalize_order_handles_absent_optional_prices() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT"))
    request = OrderRequest(Side.BUY, OrderType.MARKET, quantity=1.0)
    normalized = rules.normalize_order(request, reference_price=100.0)
    assert normalized.price is None
    assert normalized.trigger_price is None
    assert normalized.stop_loss is None
    assert normalized.take_profit is None
    assert normalized.quantity == 1.0


def test_normalize_order_handles_trigger_bracket_and_no_quantity() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", tick_size=0.1))
    request = OrderRequest(
        Side.SELL,
        OrderType.STOP_LIMIT,
        limit_price=99.91,
        trigger_price=99.81,
        stop_loss=105.01,
        take_profit=94.01,
    )
    normalized = rules.normalize_order(request, reference_price=100.0)
    assert normalized.limit_price == 100.0
    assert normalized.trigger_price == 99.9
    assert normalized.stop_loss == 105.1
    assert normalized.take_profit == 94.1
    assert normalized.quantity is None


def test_post_only_validation_covers_disabled_missing_and_marketable_paths() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT"))
    rules.validate_post_only(
        OrderRequest(
            Side.BUY,
            OrderType.LIMIT,
            quantity=1.0,
            limit_price=99.0,
            post_only=False,
        ),
        reference_price=100.0,
    )

    malformed = object.__new__(OrderRequest)
    object.__setattr__(malformed, "post_only", True)
    object.__setattr__(malformed, "side", Side.BUY)
    object.__setattr__(malformed, "order_type", OrderType.LIMIT)
    object.__setattr__(malformed, "limit_price", None)
    object.__setattr__(malformed, "price", None)
    with pytest.raises(InstrumentRuleError, match="requires a limit price"):
        rules.validate_post_only(malformed, reference_price=100.0)

    with pytest.raises(InstrumentRuleError, match="would be marketable"):
        rules.validate_post_only(
            OrderRequest(
                Side.BUY,
                OrderType.LIMIT,
                quantity=1.0,
                limit_price=101.0,
                post_only=True,
            ),
            reference_price=100.0,
        )
    with pytest.raises(InstrumentRuleError, match="would be marketable"):
        rules.validate_post_only(
            OrderRequest(
                Side.SELL,
                OrderType.LIMIT,
                quantity=1.0,
                limit_price=99.0,
                post_only=True,
            ),
            reference_price=100.0,
        )
    rules.validate_post_only(
        OrderRequest(
            Side.BUY,
            OrderType.LIMIT,
            quantity=1.0,
            limit_price=99.0,
            post_only=True,
        ),
        reference_price=100.0,
    )


@pytest.mark.parametrize("price", [0.0, float("inf")])
def test_price_normalization_rejects_nonpositive_or_nonfinite(price: float) -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT"))
    with pytest.raises(InstrumentRuleError):
        rules.normalize_entry_price(price, Side.BUY, OrderType.MARKET)


def test_zero_step_helpers_and_price_precision_are_stable() -> None:
    rules = ExchangeRulesAdapter(InstrumentSpec("BTCUSDT", price_precision=2))
    assert rules.normalize_entry_price(100.126, Side.BUY, OrderType.MARKET) == 100.13
    assert rules._floor_to_step(1.234, 0.0) == 1.234
    assert rules._ceil_to_step(1.234, 0.0) == 1.234
