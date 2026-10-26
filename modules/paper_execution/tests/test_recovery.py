from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from modular_core import EventEnvelope
from modular_core.checkpoints import capture_module_states, restore_module_states
from modular_core.events import InMemoryEventStore

from paper_execution import (
    ExecutableTradePlan,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    ReconciliationBlocked,
)
from paper_execution.journal import ExecutionEventJournal


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _coordinator(broker, metadata, store, run_id="recovery-run"):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )


def test_snapshot_restore_requires_reconciliation_and_preserves_idempotency(
    plan, versions, now, broker, metadata
) -> None:
    store = InMemoryEventStore()
    original = _coordinator(broker, metadata, store)
    first = original.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    state = original.snapshot_state()

    recovered = _coordinator(broker, metadata, store)
    recovered.restore_state(state)
    assert recovered.recovery_pending

    repeated_while_blocked = recovered.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert repeated_while_blocked == first
    assert len(broker.orders) == 1
    assert recovered.recovery_pending

    unseen = ExecutableTradePlan.create(
        plan_id="plan-unseen-after-restore",
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=plan.intents,
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
        input_hashes=plan.input_hashes,
    )
    with pytest.raises(ReconciliationBlocked, match="recovery reconciliation"):
        recovered.accept_plan(
            unseen,
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )

    report = recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert not report.blocked
    assert recovered.recovery_pending
    position_findings = recovered.reconcile_position_state(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        expected=None,
        observed=broker.portfolio.position_snapshot(100000),
    )
    assert position_findings == ()
    assert not recovered.recovery_pending
    repeated = recovered.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert repeated == first
    assert len(broker.orders) == 1


def test_core_module_state_contract_round_trip(plan, versions, now, broker, metadata) -> None:
    store = InMemoryEventStore()
    original = _coordinator(broker, metadata, store, "core-checkpoint")
    original.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    module_states = capture_module_states((original,))
    assert module_states[0].module == original.name
    recovered = _coordinator(broker, metadata, store, "core-checkpoint")
    restore_module_states((recovered,), module_states)
    assert recovered.snapshot_state() == original.snapshot_state()


def test_tampered_state_is_rejected_transactionally(plan, versions, now, broker, metadata) -> None:
    store = InMemoryEventStore()
    original = _coordinator(broker, metadata, store, "tamper")
    original.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    tampered = deepcopy(dict(original.snapshot_state()))
    tampered["event_sequence"] = 999
    target = _coordinator(broker, metadata, store, "tamper")
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="state hash"):
        target.restore_state(tampered)
    assert target.snapshot_state() == before


def test_restore_rejects_mismatched_event_store(plan, versions, now, broker, metadata) -> None:
    original_store = InMemoryEventStore()
    original = _coordinator(broker, metadata, original_store, "journal-mismatch")
    original.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    target = _coordinator(broker, metadata, InMemoryEventStore(), "journal-mismatch")
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="journal attestation"):
        target.restore_state(original.snapshot_state())
    assert target.snapshot_state() == before


class StaticEventStore:
    def __init__(self, events):
        self.events = tuple(events)

    def read(self, *, after_sequence=0):
        return tuple(event for event in self.events if event.sequence > after_sequence)

    def append(self, event):
        self.events = (*self.events, event)

    def append_many(self, events):
        self.events = (*self.events, *events)


def test_malformed_existing_journal_is_rejected(now) -> None:
    malformed = EventEnvelope(
        event_id="bad-event",
        run_id="bad-run",
        sequence=1,
        timestamp=now,
        event_type="bad",
        source="paper_execution",
        payload={"unexpected": "payload"},
        schema_version="1",
    )
    with pytest.raises(ValueError, match="journal is invalid"):
        ExecutionEventJournal("bad-run", StaticEventStore((malformed,)))


class RejectingEventStore:
    def read(self, *, after_sequence=0):
        return ()

    def append(self, event):
        raise OSError("disk full")

    def append_many(self, events):
        raise OSError("disk full")


def test_failed_append_rolls_back_journal_sequence(now) -> None:
    journal = ExecutionEventJournal("append-failure", RejectingEventStore())
    with pytest.raises(OSError, match="disk full"):
        journal.append(
            timestamp=now,
            event_type="plan.received",
            aggregate_id="plan",
            payload={"value": 1},
        )
    assert journal.sequence == 0
    assert journal.previous_hash == ""


def _rehash_state(state):
    from paper_execution._utils import canonical_json, sha256_text

    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))
    return state


def test_journal_serialization_failure_does_not_consume_sequence(now) -> None:
    journal = ExecutionEventJournal("bad-payload")
    with pytest.raises(TypeError, match="unsupported canonical"):
        journal.append(
            timestamp=now,
            event_type="bad.payload",
            aggregate_id="plan",
            payload={"value": object()},
        )
    assert journal.sequence == 0
    assert journal.store.read() == ()
