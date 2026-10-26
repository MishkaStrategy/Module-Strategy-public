from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from modular_core import (
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

from paper_execution.enums import PaperOrderState
from paper_execution.reconciliation import ReconciliationEngine
from paper_execution.state_machine import OrderAggregate


def _venue(
    *,
    core_id: str = "core-1",
    client_id: str | None = "client-1",
    source: str = "paper_execution_venue_reconciliation",
    status: OrderStatus = OrderStatus.PENDING,
    quantity: float = 1.0,
    filled: float = 0.0,
) -> OrderSnapshot:
    return OrderSnapshot(
        id=core_id,
        source_module=source,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id=client_id,
        status=status,
        filled_quantity=filled,
        remaining_quantity=quantity - filled,
        average_fill_price=(100.0 if filled else None),
        fees_paid=filled * 0.01,
        time_in_force=TimeInForce.GTC,
    )


def _local(
    *,
    client_id: str = "client-1",
    core_id: str = "core-1",
    state: PaperOrderState = PaperOrderState.OPEN,
    requested: str = "1",
    filled: str = "0",
) -> OrderAggregate:
    return OrderAggregate(
        client_order_id=client_id,
        core_order_id=core_id,
        requested_quantity=Decimal(requested),
        state=state,
        filled_quantity=Decimal(filled),
        average_fill_price=(Decimal("100") if Decimal(filled) else None),
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
        intent_role="entry",
    )


def _categories(report) -> set[str]:
    return {finding.category for finding in report.findings}


def test_duplicate_venue_client_and_core_ids_are_blocking() -> None:
    first = _venue()
    duplicate_client = replace(first, id="core-2")
    duplicate_core = replace(first, client_order_id="client-2")

    report = ReconciliationEngine().compare(
        local_orders={},
        venue_orders=(first, duplicate_client, duplicate_core),
    )
    assert report.blocked
    assert _categories(report) >= {
        "duplicate_venue_client_order_id",
        "duplicate_venue_core_order_id",
    }
    assert all(
        not finding.automatic_resolution_eligible
        for finding in report.findings
        if finding.category.startswith("duplicate_venue")
    )


def test_terminal_local_order_is_not_auto_repairable_orphan() -> None:
    local = _local(state=PaperOrderState.CANCELED)
    report = ReconciliationEngine().compare(
        local_orders={"client-1": local},
        venue_orders=(_venue(),),
    )

    assert _categories(report) == {"terminal_local_order_still_pending"}
    finding = report.findings[0]
    assert finding.blocking
    assert not finding.automatic_resolution_eligible


def test_core_id_quantity_fill_and_state_mismatches_are_reported() -> None:
    local = _local(
        core_id="core-local",
        state=PaperOrderState.PARTIALLY_FILLED,
        requested="1",
        filled="0.4",
    )
    venue = _venue(
        core_id="core-venue",
        status=OrderStatus.PENDING,
        quantity=2.0,
        filled=0.5,
    )
    report = ReconciliationEngine().compare(
        local_orders={"client-1": local},
        venue_orders=(venue,),
    )

    assert report.blocked
    assert _categories(report) >= {
        "core_order_id_mismatch",
        "order_quantity_mismatch",
        "order_fill_mismatch",
        "order_state_mismatch",
    }


def test_terminal_snapshot_cannot_appear_in_pending_view() -> None:
    report = ReconciliationEngine().compare(
        local_orders={"client-1": _local()},
        venue_orders=(_venue(status=OrderStatus.CANCELLED),),
    )
    assert "terminal_order_in_pending_snapshot" in _categories(report)
    assert report.blocked


def test_owned_order_without_client_id_is_blocking() -> None:
    report = ReconciliationEngine().compare(
        local_orders={},
        venue_orders=(_venue(client_id=None),),
    )
    assert _categories(report) == {"owned_venue_order_missing_client_id"}
    assert report.blocked


def test_unique_owned_orphan_is_only_safe_auto_repair_candidate() -> None:
    owned = ReconciliationEngine().compare(
        local_orders={},
        venue_orders=(_venue(),),
    )
    assert _categories(owned) == {"orphan_venue_order"}
    assert owned.findings[0].automatic_resolution_eligible

    foreign = ReconciliationEngine().compare(
        local_orders={},
        venue_orders=(_venue(source="manual"),),
    )
    assert _categories(foreign) == {"unknown_external_order"}
    assert not foreign.findings[0].automatic_resolution_eligible
    assert not foreign.blocked


def test_local_mapping_key_mismatch_and_duplicate_core_id_are_blocking() -> None:
    first = _local(client_id="client-1", core_id="core-shared")
    second = _local(client_id="client-2", core_id="core-shared")
    report = ReconciliationEngine().compare(
        local_orders={"wrong-key": first, "client-2": second},
        venue_orders=(),
    )
    assert _categories(report) >= {
        "local_order_identity_mismatch",
        "duplicate_local_core_order_id",
    }
    assert report.blocked
