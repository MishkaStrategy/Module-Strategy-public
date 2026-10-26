from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from modular_core import EventEnvelope

from paper_execution._utils import canonical_json, sha256_text
from paper_execution.journal import ExecutionEventJournal


class StaticEventStore:
    def __init__(self, events=()):
        self.events = tuple(events)

    def read(self, *, after_sequence=0):
        return tuple(event for event in self.events if event.sequence > after_sequence)

    def append(self, event):
        self.events = (*self.events, event)

    def append_many(self, events):
        self.events = (*self.events, *events)


def test_v2_hash_detects_timestamp_and_context_tampering(now) -> None:
    store = StaticEventStore()
    journal = ExecutionEventJournal("v2-tamper", store)
    event = journal.append(
        timestamp=now,
        event_type="order.fill",
        aggregate_id="order-1",
        payload={"quantity": "1"},
        correlation_id="plan-1",
        causation_id="submission-1",
        symbol="BTCUSDT",
    )
    assert event.schema_version == "2"
    assert journal.verify_chain()

    for tampered in (
        replace(event, timestamp=now + timedelta(seconds=1)),
        replace(event, correlation_id="plan-2"),
        replace(event, causation_id="submission-2"),
        replace(event, symbol="ETHUSDT"),
    ):
        store.events = (tampered,)
        assert not journal.verify_chain()
    store.events = (event,)
    assert journal.verify_chain()


def test_v2_structured_hash_has_no_delimiter_boundary_alias(now) -> None:
    common = {
        "sequence": 1,
        "timestamp": now,
        "source": "paper_execution",
        "payload_hash": "a" * 64,
        "previous_hash": "",
        "correlation_id": None,
        "causation_id": None,
        "symbol": None,
    }
    first = ExecutionEventJournal._event_hash_v2(
        run_id="a|b",
        event_type="c",
        aggregate_id="d",
        **common,
    )
    second = ExecutionEventJournal._event_hash_v2(
        run_id="a",
        event_type="b|c",
        aggregate_id="d",
        **common,
    )
    assert first != second


def test_reserved_payload_fields_are_rejected_without_consuming_sequence(
    now,
) -> None:
    store = StaticEventStore()
    journal = ExecutionEventJournal("reserved", store)

    for key in (
        "aggregate_id",
        "payload_hash",
        "previous_hash",
        "event_hash",
    ):
        with pytest.raises(ValueError, match="reserved"):
            journal.append(
                timestamp=now,
                event_type="bad.payload",
                aggregate_id="aggregate",
                payload={key: "attacker-controlled"},
            )
        assert journal.sequence == 0
        assert journal.previous_hash == ""
        assert store.events == ()


def test_legacy_v1_chain_can_continue_with_v2_event(now) -> None:
    run_id = "mixed-chain"
    user_payload = {"value": 1}
    payload_hash = sha256_text(canonical_json(user_payload))
    event_hash = ExecutionEventJournal._event_hash_v1(
        run_id=run_id,
        sequence=1,
        event_type="legacy.event",
        aggregate_id="legacy",
        payload_hash=payload_hash,
        previous_hash="",
    )
    legacy = EventEnvelope(
        event_id=f"pevr-{1:012d}-{event_hash[:16]}",
        run_id=run_id,
        sequence=1,
        timestamp=now,
        event_type="legacy.event",
        source="paper_execution",
        schema_version="1",
        payload={
            **user_payload,
            "aggregate_id": "legacy",
            "payload_hash": payload_hash,
            "previous_hash": "",
            "event_hash": event_hash,
        },
    )
    store = StaticEventStore((legacy,))
    journal = ExecutionEventJournal(run_id, store)
    current = journal.append(
        timestamp=now + timedelta(seconds=1),
        event_type="current.event",
        aggregate_id="current",
        payload={"value": 2},
    )

    assert current.schema_version == "2"
    assert current.sequence == 2
    assert journal.verify_chain()


def test_payload_and_event_limits_are_checked_before_append(
    monkeypatch,
    now,
) -> None:
    import paper_execution.journal as journal_module

    monkeypatch.setattr(journal_module, "MAX_JOURNAL_PAYLOAD_ITEMS", 1)
    journal = ExecutionEventJournal("payload-limit", StaticEventStore())
    with pytest.raises(ValueError, match="payload exceeds item"):
        journal.append(
            timestamp=now,
            event_type="too.large",
            aggregate_id="aggregate",
            payload={"a": 1, "b": 2},
        )
    assert journal.sequence == 0

    monkeypatch.setattr(journal_module, "MAX_JOURNAL_EVENTS", 0)
    with pytest.raises(ValueError, match="event limit"):
        journal.append(
            timestamp=now,
            event_type="too.many",
            aggregate_id="aggregate",
            payload={},
        )
    assert journal.sequence == 0


def test_identifier_validation_is_typed_and_bounded(now) -> None:
    with pytest.raises(ValueError, match="run_id"):
        ExecutionEventJournal(" ")

    journal = ExecutionEventJournal("identifiers")
    for field, value in (
        ("event_type", ""),
        ("aggregate_id", ""),
        ("correlation_id", "x" * 257),
        ("causation_id", "x" * 257),
        ("symbol", "x" * 257),
    ):
        kwargs = {
            "timestamp": now,
            "event_type": "event",
            "aggregate_id": "aggregate",
            "payload": {},
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match=field):
            journal.append(**kwargs)
        assert journal.sequence == 0
