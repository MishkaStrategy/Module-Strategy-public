from __future__ import annotations

from decimal import Decimal

from modular_core import OrderSnapshot, OrderStatus, OrderType, Side

from paper_execution import (
    PaperOrderState,
    ReconciliationEngine,
    ReconciliationMode,
)
from paper_execution.state_machine import OrderAggregate


def venue_order(now, *, client_id="client-1", filled=0.0, remaining=1.0):
    return OrderSnapshot(
        id="ord-1",
        source_module="paper_execution_venue_reconciliation",
        created_at=now,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id=client_id,
        status=OrderStatus.PENDING,
        filled_quantity=filled,
        remaining_quantity=remaining,
    )


def test_exact_reconciliation_has_no_findings(now) -> None:
    internal = {"client-1": OrderAggregate("client-1", "ord-1", Decimal("1"), PaperOrderState.OPEN)}
    report = ReconciliationEngine().reconcile_orders(
        internal=internal,
        venue_orders=(venue_order(now),),
        mode=ReconciliationMode.STRICT_BLOCK,
    )
    assert not report.findings
    assert not report.blocked


def test_missing_venue_order_blocks(now) -> None:
    internal = {"client-1": OrderAggregate("client-1", "ord-1", Decimal("1"), PaperOrderState.OPEN)}
    report = ReconciliationEngine().reconcile_orders(
        internal=internal,
        venue_orders=(),
        mode=ReconciliationMode.STRICT_BLOCK,
    )
    assert report.blocked
    assert report.findings[0].category == "internal_order_missing_at_venue"


def test_orphan_venue_order_blocks(now) -> None:
    report = ReconciliationEngine().reconcile_orders(
        internal={},
        venue_orders=(venue_order(now),),
        mode=ReconciliationMode.STRICT_BLOCK,
    )
    assert report.blocked
    assert report.findings[0].category == "venue_order_missing_internally"


def test_fill_mismatch_blocks(now) -> None:
    aggregate = OrderAggregate("client-1", "ord-1", Decimal("1"), PaperOrderState.PARTIALLY_FILLED)
    aggregate.filled_quantity = Decimal("0.4")
    report = ReconciliationEngine().reconcile_orders(
        internal={"client-1": aggregate},
        venue_orders=(venue_order(now, filled=0.5, remaining=0.5),),
        mode=ReconciliationMode.STRICT_BLOCK,
    )
    assert report.blocked
    assert report.findings[0].category == "filled_quantity_mismatch"
