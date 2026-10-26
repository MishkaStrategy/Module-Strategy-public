from __future__ import annotations

from datetime import datetime, timezone

import pytest
from modular_core import (
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

from paper_execution import (
    IntentRole,
    LifecycleState,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    ReconciliationMode,
)
from paper_execution.snapshot_identity import build_snapshot_tag


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        return self.metadata


class SnapshotVenue:
    def __init__(self, snapshots):
        self.snapshots = tuple(snapshots)

    def pending_snapshots(self):
        return self.snapshots

    def submit(self, request, **kwargs):
        raise AssertionError("submit must not be called during reconciliation")

    def cancel(self, request, **kwargs):
        raise AssertionError("cancel must not be called during reconciliation")

    def process_bar(self, bar, index):
        raise AssertionError("process_bar is not part of reconciliation")

    def update_position(self, request, **kwargs):
        raise AssertionError("position repair is forbidden")


class RejectingStore:
    def read(self, *, after_sequence=0):
        return ()

    def append(self, event):
        raise OSError("disk full")

    def append_many(self, events):
        raise OSError("disk full")


def _snapshot(
    *,
    client_id: str = "pevr-entry-0-aaaaaaaaaaaa",
    core_id: str = "core-1",
    source: str = "paper_execution_venue_reconciliation",
    instrument: str | None = "BTCUSDT",
    portfolio_id: str = "portfolio-1",
    role: str | None = "entry",
    status: OrderStatus = OrderStatus.PENDING,
    tag_override: str | None = None,
) -> OrderSnapshot:
    tag = ""
    if instrument is not None and role is not None:
        try:
            tag = build_snapshot_tag(
                portfolio_id=portfolio_id,
                instrument=instrument,
                role=IntentRole(role),
            )
        except ValueError:
            tag = f"pevr1:{role}:invalid"
    if tag_override is not None:
        tag = tag_override
    return OrderSnapshot(
        id=core_id,
        source_module=source,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag=tag,
        setup_id=None,
        client_order_id=client_id,
        status=status,
        filled_quantity=0.0,
        remaining_quantity=1.0,
        average_fill_price=None,
        fees_paid=0.0,
        time_in_force=TimeInForce.GTC,
    )


def _service(metadata, snapshots, *, store=None):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(
            reconciliation_mode=ReconciliationMode.SAFE_AUTO_REPAIR,
        ),
        venue=SnapshotVenue(snapshots),
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="safe-repair",
        event_store=store,
    )


def test_unique_owned_orphan_is_repaired_then_reconciled(metadata, now) -> None:
    snapshot = _snapshot()
    service = _service(metadata, (snapshot,))

    report = service.reconcile(
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
        timestamp=now,
    )

    assert report.findings == ()
    assert not report.blocked
    assert set(service.orders) == {snapshot.client_order_id}
    aggregate = service.orders[snapshot.client_order_id]
    assert aggregate.core_order_id == snapshot.id
    assert aggregate.intent_role == "entry"
    assert service.lifecycle is LifecycleState.READY
    assert ("portfolio-1", "BTCUSDT") not in service.blocked_scopes
    assert any(
        event.event_type == "reconciliation.order_projection_repaired"
        for event in service.journal.store.read()
    )


def test_repair_requires_explicit_simulation_timestamp(metadata) -> None:
    service = _service(metadata, (_snapshot(),))
    report = service.reconcile(
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
    )

    assert report.blocked
    assert service.orders == {}
    assert service.lifecycle is LifecycleState.BLOCKED


@pytest.mark.parametrize(
    "snapshot",
    (
        _snapshot(source="manual"),
        _snapshot(instrument=None),
        _snapshot(instrument="ETHUSDT"),
        _snapshot(portfolio_id="portfolio-other"),
        _snapshot(role=None),
        _snapshot(role="cancel"),
        _snapshot(client_id="wrong-prefix-aaaaaaaaaaaa"),
        _snapshot(status=OrderStatus.CANCELLED),
        _snapshot(tag_override="pevr1:entry:forged"),
    ),
)
def test_missing_ownership_or_state_proof_never_repairs(
    metadata,
    now,
    snapshot,
) -> None:
    service = _service(metadata, (snapshot,))
    report = service.reconcile(
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
        timestamp=now,
    )

    assert service.orders == {}
    assert not any(
        event.event_type == "reconciliation.order_projection_repaired"
        for event in service.journal.store.read()
    )
    if snapshot.client_order_id.startswith("pevr-"):
        assert report.blocked


def test_multiple_owned_orphans_are_not_batch_repaired(metadata, now) -> None:
    service = _service(
        metadata,
        (
            _snapshot(),
            _snapshot(
                client_id="pevr-entry-0-bbbbbbbbbbbb",
                core_id="core-2",
            ),
        ),
    )
    report = service.reconcile(
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
        timestamp=now,
    )

    assert report.blocked
    assert service.orders == {}


def test_journal_failure_rolls_back_projection(metadata, now) -> None:
    service = _service(
        metadata,
        (_snapshot(),),
        store=RejectingStore(),
    )

    with pytest.raises(OSError, match="disk full"):
        service.reconcile(
            portfolio_id="portfolio-1",
            instrument="BTCUSDT",
            timestamp=now,
        )
    assert service.orders == {}
    assert ("portfolio-1", "BTCUSDT") in service.blocked_scopes
