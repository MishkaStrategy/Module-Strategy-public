from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core import (
    EngineConfig,
    OrderRequest,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from modular_core.audit import Journal
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec
from modular_core.models import Position


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _broker() -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(),
        Journal(run_id="run-gtd-boundaries"),
        InstrumentSpec(symbol="BTCUSDT"),
    )


def test_order_request_rejects_good_till_time_for_non_gtd() -> None:
    with pytest.raises(ValueError, match="valid only for GTD"):
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=1,
            price=100,
            time_in_force=TimeInForce.GTC,
            good_till_time=NOW + timedelta(hours=1),
        )


def test_broker_rejects_expired_gtd_before_state_mutation() -> None:
    broker = _broker()
    expired = OrderRequest(
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        price=100,
        time_in_force=TimeInForce.GTD,
        good_till_time=NOW,
        client_order_id="client-1",
    )

    assert (
        broker.submit(
            expired,
            source_module="probe",
            created_at=NOW,
            current_index=0,
            mark_price=100,
        )
        is None
    )
    assert broker.orders == []
    assert broker.pending_orders == []

    valid = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=1,
            price=100,
            time_in_force=TimeInForce.GTD,
            good_till_time=NOW + timedelta(hours=1),
            client_order_id="client-1",
        ),
        source_module="probe",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )

    assert valid is not None
    assert valid.id == "ord-00000001"


@pytest.mark.parametrize(
    "overrides",
    [
        {"reduce_only": True, "risk_fraction": 0.01},
        {"reduce_only": True, "stop_loss": 95},
        {"reduce_only": True, "take_profit": 105},
        {"reduce_only": True, "close_position": True, "quantity": 1},
    ],
)
def test_order_request_rejects_ignored_reduce_only_fields(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "side": Side.SELL,
        "order_type": OrderType.MARKET,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="reduce_only|close_position"):
        OrderRequest(**values)  # type: ignore[arg-type]


def test_broker_rejects_entry_position_side_mismatch() -> None:
    broker = _broker()

    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1,
            position_side=PositionSide.SHORT,
        ),
        source_module="probe",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )

    assert order is None
    assert broker.orders == []


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"order_type": OrderType.MARKET, "price": 100}, "execution prices"),
        (
            {
                "order_type": OrderType.MARKET,
                "trigger_price": 100,
            },
            "execution prices",
        ),
        (
            {
                "order_type": OrderType.LIMIT,
                "price": 100,
                "trigger_price": 101,
            },
            "cannot define trigger_price",
        ),
        (
            {
                "order_type": OrderType.STOP_MARKET,
                "trigger_price": 100,
                "limit_price": 99,
            },
            "cannot define limit_price",
        ),
        (
            {
                "order_type": OrderType.STOP_MARKET,
                "price": 99,
                "trigger_price": 100,
            },
            "price and trigger_price cannot disagree",
        ),
    ],
)
def test_order_request_rejects_ignored_or_conflicting_execution_prices(
    kwargs: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {"side": Side.BUY, "quantity": 1}
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        OrderRequest(**values)  # type: ignore[arg-type]


def test_broker_rejects_reduce_only_position_side_mismatch() -> None:
    broker = _broker()
    broker.portfolio.position = Position(
        id="position-1",
        entry_order_id="entry-1",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0,
    )

    order = broker.submit(
        OrderRequest(
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=1,
            reduce_only=True,
            position_side=PositionSide.SHORT,
        ),
        source_module="probe",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )

    assert order is None
    assert broker.orders == []


def test_broker_accepts_consistent_position_side_guards() -> None:
    entry_broker = _broker()
    entry = entry_broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1,
            position_side=PositionSide.LONG,
        ),
        source_module="probe",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )
    assert entry is not None

    exit_broker = _broker()
    exit_broker.portfolio.position = Position(
        id="position-1",
        entry_order_id="entry-1",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0,
    )
    exit_order = exit_broker.submit(
        OrderRequest(
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=1,
            reduce_only=True,
            position_side=PositionSide.LONG,
        ),
        source_module="probe",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )
    assert exit_order is not None
