from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta

import pytest
from modular_core import (
    Candle,
    MarketView,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionSnapshot,
    Side,
)

START = datetime(2026, 1, 1, tzinfo=UTC)

_FROZEN_CORE_CORRUPTION_TESTS = frozenset(
    {
        "test_invalid_position_snapshot_is_rejected_before_cursor_mutation",
        "test_naive_position_time_is_rejected_before_cursor_mutation",
        "test_malformed_position_fields_fail_before_state_mutation",
        "test_malformed_view_fields_fail_before_state_mutation",
        "test_non_sequence_market_bars_fail_before_state_mutation",
        "test_non_candle_current_value_fails_before_state_mutation",
        "test_position_values_must_fit_public_float_transport_before_state_mutation",
    }
)
_ORIGINAL_REPLACE = dataclasses.replace


def _corrupting_replace(obj: object, /, **changes: object) -> object:
    """Apply raw field corruption after a valid frozen-Core construction.

    Frozen Core 0.4.2 rejects malformed DTOs during dataclass construction. These
    focused tests intentionally exercise Position Exit's second defensive boundary,
    so they must model corruption after the public constructor has succeeded.
    """

    try:
        replacement = _ORIGINAL_REPLACE(obj, **changes)
    except (OverflowError, TypeError, ValueError):
        replacement = copy.copy(obj)
    else:
        if all(getattr(replacement, field) == value for field, value in changes.items()):
            return replacement
        replacement = copy.copy(obj)
    for field, value in changes.items():
        object.__setattr__(replacement, field, value)
    return replacement


@pytest.fixture(autouse=True)
def preserve_frozen_core_corruption_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    test_name = request.node.name.partition("[")[0]
    if test_name not in _FROZEN_CORE_CORRUPTION_TESTS:
        yield
        return

    monkeypatch.setattr(dataclasses, "replace", _corrupting_replace)
    if request.module is not None and hasattr(request.module, "replace"):
        monkeypatch.setattr(request.module, "replace", _corrupting_replace)
    yield


@pytest.fixture
def candle_factory():
    def make(
        index: int = 0,
        *,
        open_: float = 100.0,
        high: float = 101.0,
        low: float = 99.0,
        close: float = 100.0,
        symbol: str = "BTCUSDT",
        timeframe: str = "5m",
    ) -> Candle:
        return Candle(
            START + timedelta(minutes=5 * index),
            open_,
            high,
            low,
            close,
            1.0,
            symbol,
            timeframe,
        )

    return make


@pytest.fixture
def position_factory():
    def make(
        *,
        position_id: str = "position-1",
        side: PositionSide = PositionSide.LONG,
        quantity: float = 4.0,
        entry_price: float = 100.0,
        entry_minutes: int = 0,
        stop_loss: float | None = 95.0,
        take_profit: float | None = 120.0,
        setup_id: str | None = "setup-1",
        tag: str = "test",
        entry_index: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> PositionSnapshot:
        return PositionSnapshot(
            id=position_id,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=START + timedelta(minutes=entry_minutes),
            stop_loss=stop_loss,
            take_profit=take_profit,
            tag=tag,
            unrealized_pnl=0.0,
            setup_id=setup_id,
            entry_index=entry_index,
            metadata={} if metadata is None else metadata,
        )

    return make


@pytest.fixture
def order_factory():
    def make(
        order_id: str,
        *,
        side: Side = Side.SELL,
        order_type: OrderType = OrderType.STOP_MARKET,
        quantity: float = 1.0,
        price: float | None = 95.0,
        reduce_only: bool = True,
        tag: str = "position-exit:stop",
        setup_id: str | None = "setup-1",
        status: OrderStatus = OrderStatus.PENDING,
    ) -> OrderSnapshot:
        return OrderSnapshot(
            id=order_id,
            source_module="execution",
            created_at=START,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_loss=None,
            take_profit=None,
            tag=tag,
            setup_id=setup_id,
            client_order_id=f"client-{order_id}",
            status=status,
            remaining_quantity=quantity,
            reduce_only=reduce_only,
            trigger_price=price,
        )

    return make


@pytest.fixture
def view_factory(candle_factory):
    def make(
        index: int = 0,
        *,
        candle: Candle | None = None,
        position: PositionSnapshot | None = None,
        pending_orders: tuple[OrderSnapshot, ...] = (),
        exit_pending: bool = False,
        data_gap: bool = False,
    ) -> MarketView:
        current = candle if candle is not None else candle_factory(index)
        return MarketView(
            base_bars=(current,),
            timeframe_bars={},
            index=index,
            cash=10_000.0,
            equity=10_000.0,
            position=position,
            pending_orders=pending_orders,
            exit_pending=exit_pending,
            data_gap=data_gap,
        )

    return make
