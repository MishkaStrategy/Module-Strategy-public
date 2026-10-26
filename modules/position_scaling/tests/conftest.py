from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from modular_core import (
    Candle,
    Feature,
    FeatureView,
    MarketView,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionSnapshot,
    Side,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def make_bar(index: int = 0, *, close: float = 100.0, symbol: str = "BTCUSDT") -> Candle:
    return Candle(
        timestamp=BASE_TIME + timedelta(minutes=index),
        open=close,
        high=close + 1,
        low=max(0.0001, close - 1),
        close=close,
        volume=1,
        symbol=symbol,
        timeframe="1m",
    )


def make_position(
    *,
    side: PositionSide = PositionSide.LONG,
    quantity: float = 1.0,
    entry_price: float = 100.0,
    stop_loss: float | None = 95.0,
    pnl: float = 0.0,
    position_id: str = "position-1",
    setup_id: str | None = "setup-1",
    entry_fill_count: int = 1,
) -> PositionSnapshot:
    return PositionSnapshot(
        id=position_id,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_time=BASE_TIME,
        stop_loss=stop_loss,
        take_profit=120.0 if side is PositionSide.LONG else 80.0,
        tag="test",
        unrealized_pnl=pnl,
        setup_id=setup_id,
        entry_fill_count=entry_fill_count,
    )


def feature(
    key: str,
    value: Any,
    *,
    index: int = 0,
    symbol: str = "BTCUSDT",
    source: str = "test",
    feature_id: str | None = None,
) -> Feature:
    bar = make_bar(index, symbol=symbol)
    return Feature(
        key=key,
        source_module=source,
        created_at=bar.close_time,
        value=value,
        symbol=symbol,
        timeframe="1m",
        valid_until=bar.close_time,
        feature_id=feature_id or f"{key}-{index}",
        invalidate_on_gap=False,
    )


def standard_features(index: int = 0, *, setup_valid: bool = True) -> tuple[Feature, ...]:
    return (
        feature("setup.validity", {"valid": setup_valid}, index=index),
        feature("market_regime.state", {"regime": "trend"}, index=index),
        feature("market.atr", {"value": "2"}, index=index),
        feature(
            "instrument.rules",
            {
                "quantity_step": "0.001",
                "min_quantity": "0.001",
                "max_quantity": "100",
                "min_notional": "5",
                "max_notional": "1000000",
                "tick_size": "0.1",
                "contract_size": "1",
                "max_leverage": "10",
            },
            index=index,
        ),
        feature(
            "portfolio.risk_snapshot",
            {
                "gross_notional": "100",
                "symbol_notional": "100",
                "open_risk_amount": "5",
                "open_positions": 1,
            },
            index=index,
        ),
        feature(
            "position_scaling.context",
            {
                "liquidation_distance_fraction": "0.20",
                "margin_usage_fraction": "0.10",
            },
            index=index,
        ),
    )


def make_order(
    *, reduce_only: bool = False, status: OrderStatus = OrderStatus.PENDING
) -> OrderSnapshot:
    return OrderSnapshot(
        id="order-1",
        source_module="test",
        created_at=BASE_TIME,
        side=Side.SELL if reduce_only else Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=0.1,
        price=99.0,
        stop_loss=None,
        take_profit=None,
        tag="test",
        setup_id="setup-1",
        client_order_id="client-1",
        status=status,
        remaining_quantity=0.1,
        reduce_only=reduce_only,
    )


def make_view(
    index: int = 0,
    *,
    close: float = 100.0,
    position: PositionSnapshot | None = None,
    features: Iterable[Feature] = (),
    equity: float = 10000.0,
    cash: float = 9000.0,
    pending_orders: tuple[OrderSnapshot, ...] = (),
    exit_pending: bool = False,
    data_gap: bool = False,
) -> MarketView:
    bar = make_bar(index, close=close)
    return MarketView(
        base_bars=(bar,),
        timeframe_bars={},
        index=index,
        cash=cash,
        equity=equity,
        position=position,
        pending_orders=pending_orders,
        exit_pending=exit_pending,
        data_gap=data_gap,
        features=FeatureView(tuple(features), bar.close_time),
    )


@pytest.fixture
def averaging_parameters() -> Mapping[str, Any]:
    return {
        "enabled": True,
        "allow_averaging_down": True,
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "initial_fraction",
                "quantity_value": "0.25",
                "cooldown_bars": 1,
                "expires_bars": 0,
            }
        ],
    }


@pytest.fixture
def pyramiding_parameters() -> Mapping[str, Any]:
    return {
        "enabled": True,
        "allow_pyramiding": True,
        "rules": [
            {
                "stage": 1,
                "mode": "pyramiding",
                "action": "add",
                "trigger_kind": "r_multiple",
                "trigger_value": "1",
                "quantity_kind": "initial_fraction",
                "quantity_value": "0.25",
                "cooldown_bars": 1,
                "expires_bars": 0,
            }
        ],
    }
