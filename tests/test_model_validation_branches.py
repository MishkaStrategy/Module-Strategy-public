from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_core import (
    CancelRequest,
    Candle,
    ClosePositionRequest,
    Fill,
    OrderRequest,
    OrderType,
    PositionSide,
    PositionUpdateRequest,
    SetupEvent,
    SetupStatus,
    Side,
    Signal,
    TimeInForce,
    Trade,
)
from modular_core.models import Position

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"symbol": ""},
        {"timeframe": ""},
        {"high": 99.0},
        {"low": 101.0},
    ],
)
def test_candle_rejects_invalid_identity_or_geometry(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "timestamp": NOW,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "symbol": "BTCUSDT",
        "timeframe": "5m",
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        Candle(**values)  # type: ignore[arg-type]


def test_candle_close_time_handles_unknown_and_second_timeframes() -> None:
    unknown = Candle(NOW, 100.0, 101.0, 99.0, 100.0, timeframe="base")
    seconds = Candle(NOW, 100.0, 101.0, 99.0, 100.0, timeframe="30s")
    assert unknown.close_time == NOW
    assert seconds.close_time == NOW + timedelta(seconds=30)


@pytest.mark.parametrize("strength", [-0.1, 1.1])
def test_signal_rejects_strength_outside_unit_interval(strength: float) -> None:
    with pytest.raises(ValueError, match="strength"):
        Signal("module", Side.BUY, "reason", strength=strength)


@pytest.mark.parametrize(("module", "setup_id"), [("", "setup"), ("module", "")])
def test_setup_event_requires_module_and_id(module: str, setup_id: str) -> None:
    with pytest.raises(ValueError, match="required"):
        SetupEvent(module, setup_id, SetupStatus.CREATED)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ClosePositionRequest(reason=""),
        lambda: ClosePositionRequest(quantity=0.0),
        lambda: ClosePositionRequest(fraction=0.0),
        lambda: ClosePositionRequest(fraction=1.1),
        lambda: ClosePositionRequest(quantity=1.0, fraction=0.5),
    ],
)
def test_close_position_request_rejects_invalid_contract(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PositionUpdateRequest(0.0, None),
        lambda: PositionUpdateRequest(None, 0.0),
        lambda: PositionUpdateRequest(None, None, reason=""),
    ],
)
def test_position_update_request_rejects_invalid_contract(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, quantity=1.0, risk_fraction=0.1),
        lambda: OrderRequest(Side.BUY, OrderType.LIMIT),
        lambda: OrderRequest(Side.BUY, OrderType.STOP),
        lambda: OrderRequest(
            Side.BUY,
            OrderType.STOP_LIMIT,
            limit_price=100.0,
        ),
        lambda: OrderRequest(
            Side.BUY,
            OrderType.TAKE_PROFIT_LIMIT,
            trigger_price=101.0,
        ),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, post_only=True),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, time_in_force=TimeInForce.GTD),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, risk_fraction=0.0),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, risk_fraction=1.1),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, ttl_bars=0),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, setup_id=" "),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, client_order_id=" "),
        lambda: OrderRequest(Side.BUY, OrderType.MARKET, close_position=True),
        lambda: OrderRequest(
            Side.BUY,
            OrderType.LIMIT,
            price=100.0,
            limit_price=101.0,
        ),
    ],
)
def test_order_request_rejects_invalid_contract_combinations(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_order_request_explicit_prices_cover_precedence_paths() -> None:
    stop = OrderRequest(
        Side.BUY,
        OrderType.STOP_MARKET,
        trigger_price=100.0,
    )
    limit = OrderRequest(
        Side.BUY,
        OrderType.LIMIT,
        price=99.0,
        limit_price=99.0,
    )
    assert stop.effective_trigger_price == 100.0
    assert limit.effective_limit_price == 99.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"order_id": "order", "tag": "tag"},
    ],
)
def test_cancel_request_requires_exactly_one_selector(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CancelRequest(**kwargs)


def test_fill_validates_optional_cumulative_and_remaining_quantities() -> None:
    fill = Fill(
        order_id="order-1",
        timestamp=NOW,
        side=Side.BUY,
        quantity=1.0,
        price=100.0,
        fee=0.1,
        slippage=0.0,
        reason="entry",
        liquidity="maker",
        cumulative_quantity=1.0,
        remaining_quantity=0.0,
    )
    assert fill.cumulative_quantity == 1.0
    assert fill.remaining_quantity == 0.0


def test_position_direction_covers_short_and_entry_fill_update() -> None:
    position = Position(
        id="position-1",
        entry_order_id="order-1",
        side=PositionSide.SHORT,
        quantity=1.0,
        entry_price=100.0,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0.1,
    )
    fill = Fill(
        order_id="order-2",
        timestamp=NOW,
        side=Side.SELL,
        quantity=1.0,
        price=102.0,
        fee=0.1,
        slippage=0.0,
        reason="scale",
        liquidity="maker",
    )
    assert position.direction == -1
    position.add_entry_fill(fill)
    assert position.quantity == 2.0
    assert position.entry_price == 101.0


def _trade(**overrides: object) -> Trade:
    values: dict[str, object] = {
        "side": PositionSide.LONG,
        "quantity": 1.0,
        "entry_time": NOW,
        "exit_time": NOW + timedelta(minutes=5),
        "entry_price": 100.0,
        "exit_price": 101.0,
        "gross_pnl": 1.0,
        "fees": 0.1,
        "net_pnl": 0.9,
        "exit_reason": "target",
    }
    values.update(overrides)
    return Trade(**values)  # type: ignore[arg-type]


def test_trade_optional_risk_and_r_multiple_are_validated() -> None:
    trade = _trade(initial_risk_amount=1.0, r_multiple=0.9, holding_bars=1)
    assert trade.initial_risk_amount == 1.0
    assert trade.r_multiple == 0.9


@pytest.mark.parametrize(
    "overrides",
    [
        {"exit_time": NOW - timedelta(seconds=1)},
        {"holding_bars": -1},
        {"entry_fill_count": 0},
        {"exit_fill_count": 0},
    ],
)
def test_trade_rejects_invalid_chronology_or_fill_counts(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _trade(**overrides)
