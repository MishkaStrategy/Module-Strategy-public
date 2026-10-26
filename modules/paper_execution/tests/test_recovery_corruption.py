from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from modular_core import EventEnvelope
from modular_core.events import InMemoryEventStore

from paper_execution import (
    PaperExecutionConfig,
    PaperExecutionCoordinator,
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


class StaticEventStore:
    def __init__(self, events):
        self.events = tuple(events)

    def read(self, *, after_sequence=0):
        return tuple(event for event in self.events if event.sequence > after_sequence)

    def append(self, event):
        self.events = (*self.events, event)

    def append_many(self, events):
        self.events = (*self.events, *events)


def _rehash_state(state):
    from paper_execution._utils import canonical_json, sha256_text

    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))
    return state


def test_restore_rejects_unknown_nested_fields_even_with_valid_state_hash(
    plan, versions, now, broker, metadata
) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "unknown-field")
    source.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    state = deepcopy(dict(source.snapshot_state()))
    state["plans"][0]["unexpected"] = "value"
    _rehash_state(state)
    target = _coordinator(broker, metadata, store, "unknown-field")
    with pytest.raises(ValueError, match="unknown fields"):
        target.restore_state(state)


def test_restore_rejects_duplicate_processed_event_ids(
    plan, versions, now, broker, metadata
) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "duplicate-events")
    source.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    state = deepcopy(dict(source.snapshot_state()))
    state["orders"][0]["processed_event_ids"] = ["event-1", "event-1"]
    _rehash_state(state)
    target = _coordinator(broker, metadata, store, "duplicate-events")
    with pytest.raises(ValueError, match="duplicate processed"):
        target.restore_state(state)


def test_restore_rejects_negative_event_sequence(plan, versions, now, broker, metadata) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "negative-sequence")
    source.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    state = deepcopy(dict(source.snapshot_state()))
    state["event_sequence"] = -1
    _rehash_state(state)
    target = _coordinator(broker, metadata, store, "negative-sequence")
    with pytest.raises(ValueError, match="cannot be negative"):
        target.restore_state(state)


def test_restore_rejects_impossible_filled_order(plan, versions, now, broker, metadata) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "impossible-fill")
    source.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    state = deepcopy(dict(source.snapshot_state()))
    state["orders"][0]["state"] = "filled"
    _rehash_state(state)
    target = _coordinator(broker, metadata, store, "impossible-fill")
    with pytest.raises(ValueError, match="full cumulative"):
        target.restore_state(state)


def test_restore_rejects_configuration_mismatch(plan, versions, now, broker, metadata) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "config-restore")
    source.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    target = PaperExecutionCoordinator(
        config=PaperExecutionConfig(maximum_open_orders=99),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="config-restore",
        event_store=store,
    )
    with pytest.raises(ValueError, match="configuration hash mismatch"):
        target.restore_state(source.snapshot_state())


def test_existing_journal_sequence_gap_is_rejected(now) -> None:
    valid = ExecutionEventJournal("sequence-gap")
    event = valid.append(
        timestamp=now,
        event_type="plan.accepted",
        aggregate_id="plan",
        payload={"value": 1},
    )
    gapped = EventEnvelope(
        event_id=event.event_id,
        run_id=event.run_id,
        sequence=2,
        timestamp=event.timestamp,
        event_type=event.event_type,
        source=event.source,
        payload=event.payload,
        schema_version=event.schema_version,
    )
    with pytest.raises(ValueError, match="journal is invalid"):
        ExecutionEventJournal("sequence-gap", StaticEventStore((gapped,)))
