from __future__ import annotations

from decimal import Decimal

import pytest
from modular_core.events import InMemoryEventStore

from paper_execution import (
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperReasonCode,
    ReconciliationBlocked,
)
from paper_execution.journal import ExecutionEventJournal


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


class ObservingVenue:
    def __init__(self, broker, store) -> None:
        self.broker = broker
        self.store = store
        self.events_seen_at_submit: tuple[str, ...] | None = None

    def submit(self, request, **kwargs):
        self.events_seen_at_submit = tuple(event.event_type for event in self.store.read())
        return self.broker.submit(request, **kwargs)

    def cancel(self, request, **kwargs):
        return self.broker.cancel(request, **kwargs)

    def pending_snapshots(self):
        return self.broker.pending_snapshots()

    def process_bar(self, bar, index):
        return self.broker.process_bar(bar, index)

    def update_position(self, request, **kwargs):
        return self.broker.update_position(request, **kwargs)


class RejectingVenue(ObservingVenue):
    def submit(self, request, **kwargs):
        self.events_seen_at_submit = tuple(event.event_type for event in self.store.read())
        return None


class RejectAcceptedStore(InMemoryEventStore):
    def append(self, event) -> None:
        if event.event_type == "plan.accepted":
            raise OSError("accepted commit unavailable")
        super().append(event)


def _coordinator(*, venue, metadata, store, run_id="intent-boundary"):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=venue,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )


def _event_types(store) -> tuple[str, ...]:
    return tuple(event.event_type for event in store.read())


def test_write_ahead_intent_precedes_venue_and_acceptance(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    venue = ObservingVenue(broker, store)
    service = _coordinator(venue=venue, metadata=metadata, store=store)

    result = service.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert result.accepted
    assert venue.events_seen_at_submit == ("execution.intent_recorded",)
    assert _event_types(store) == (
        "execution.intent_recorded",
        "order.submitted",
        "plan.accepted",
    )
    intent = tuple(store.read())[0]
    assert intent.payload["plan_id"] == plan.plan_id
    assert intent.payload["content_hash"] == plan.content_hash
    assert intent.payload["portfolio_id"] == plan.portfolio_id
    assert intent.payload["instrument"] == plan.instrument
    assert [dict(row) for row in intent.payload["intents"]] == [
        {
            "intent_id": plan.intents[0].intent_id,
            "role": plan.intents[0].role.value,
            "client_order_id": result.client_order_ids[0],
            "cancel_client_order_id": None,
        }
    ]


def test_failed_venue_execution_never_records_plan_accepted(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    venue = RejectingVenue(broker, store)
    service = _coordinator(venue=venue, metadata=metadata, store=store)

    result = service.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert not result.accepted
    assert result.reason_code is PaperReasonCode.ORDER_REJECTED
    assert venue.events_seen_at_submit == ("execution.intent_recorded",)
    assert _event_types(store) == (
        "execution.intent_recorded",
        "execution.failed",
    )
    assert "plan.accepted" not in _event_types(store)


def test_failed_acceptance_commit_compensates_and_blocks_scope(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = RejectAcceptedStore()
    venue = ObservingVenue(broker, store)
    service = _coordinator(venue=venue, metadata=metadata, store=store)

    result = service.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert not result.accepted
    assert result.reason_code is PaperReasonCode.RECONCILIATION_BLOCKED
    assert broker.pending_snapshots() == ()
    assert (plan.portfolio_id, plan.instrument) in service.blocked_scopes
    assert _event_types(store) == (
        "execution.intent_recorded",
        "order.submitted",
        "order.rollback_canceled",
        "execution.failed",
    )


def test_existing_execution_intent_requires_recovery_before_new_plan(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    journal = ExecutionEventJournal("intent-recovery", store)
    journal.append(
        timestamp=now,
        event_type="execution.intent_recorded",
        aggregate_id=plan.plan_id,
        payload={
            "plan_id": plan.plan_id,
            "content_hash": plan.content_hash,
            "portfolio_id": plan.portfolio_id,
            "instrument": plan.instrument,
            "intents": [],
        },
        correlation_id=plan.plan_id,
        symbol=plan.instrument,
    )

    recovered = _coordinator(
        venue=broker,
        metadata=metadata,
        store=store,
        run_id="intent-recovery",
    )

    assert recovered.recovery_pending
    assert (plan.portfolio_id, plan.instrument) in recovered.blocked_scopes
    with pytest.raises(ReconciliationBlocked, match="recovery reconciliation"):
        recovered.accept_plan(
            plan,
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )


def test_terminalized_intent_still_requires_snapshot_recovery(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    original = _coordinator(
        venue=broker,
        metadata=metadata,
        store=store,
        run_id="terminal-intent-recovery",
    )
    result = original.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert result.accepted

    recovered = _coordinator(
        venue=broker,
        metadata=metadata,
        store=store,
        run_id="terminal-intent-recovery",
    )

    assert recovered.recovery_pending
    assert (plan.portfolio_id, plan.instrument) in recovered.blocked_scopes
    with pytest.raises(ReconciliationBlocked, match="recovery reconciliation"):
        recovered.accept_plan(
            plan,
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )
