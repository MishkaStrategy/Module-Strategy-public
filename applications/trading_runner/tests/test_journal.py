from __future__ import annotations

from datetime import UTC, datetime

import pytest
from modular_core import EventEnvelope, InMemoryEventStore

from modular_trading_runner.errors import JournalIntegrityError
from modular_trading_runner.journal import RuntimeEventJournal

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class MutableEventStore:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    def append(self, event: EventEnvelope) -> None:
        self.append_many((event,))

    def append_many(self, events: tuple[EventEnvelope, ...]) -> None:
        self.events.extend(events)

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        return tuple(event for event in self.events if event.sequence > after_sequence)


def test_runtime_journal_hash_chain_filter_and_attestation() -> None:
    store = InMemoryEventStore()
    journal = RuntimeEventJournal(run_id="run-1", store=store)
    first = journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={"state": "running"},
    )
    second = journal.append(
        timestamp=NOW,
        event_type="market.accepted",
        aggregate_id="BTCUSDT",
        payload={"event_id": "market-1"},
        correlation_id="market-1",
        symbol="BTCUSDT",
    )
    assert first.sequence == 1
    assert second.sequence == 2
    assert journal.read("market.accepted") == (second,)
    assert journal.verify_chain() is True
    journal.verify_attestation(journal.attestation())
    recovered = RuntimeEventJournal(run_id="run-1", store=store)
    assert recovered.sequence == 2
    assert recovered.previous_hash == second.payload["event_hash"]


def test_checkpoint_attestation_allows_chained_factual_recovery_extension() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={},
    )
    checkpoint_attestation = journal.attestation()
    journal.append(
        timestamp=NOW,
        event_type="factual_event.processing_started",
        aggregate_id="portfolio-1:BTCUSDT",
        symbol="BTCUSDT",
        payload={"event_id": "factual-1"},
    )
    journal.verify_attestation(checkpoint_attestation)


def test_checkpoint_attestation_allows_nonstateful_notification_extensions() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={},
    )
    checkpoint_attestation = journal.attestation()
    journal.append(
        timestamp=NOW,
        event_type="notification.failed",
        aggregate_id="notification-dispatcher",
        payload={"event_id": "notification-1"},
    )
    journal.append(
        timestamp=NOW,
        event_type="notification.dropped",
        aggregate_id="notification-dispatcher",
        payload={"event_id": "notification-2"},
    )
    journal.verify_attestation(checkpoint_attestation)


def test_checkpoint_attestation_allows_market_cycle_completion_extension() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={},
    )
    checkpoint_attestation = journal.attestation()
    journal.append(
        timestamp=NOW,
        event_type="market_cycle.completed",
        aggregate_id="market-1",
        payload={
            "event_id": "market-1",
            "content_hash": "a" * 64,
            "plan_id": None,
            "plan_hash": None,
            "no_plan": True,
        },
    )
    journal.verify_attestation(checkpoint_attestation)


def test_checkpoint_attestation_allows_clean_restart_lifecycle_tail() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    journal.append(
        timestamp=NOW,
        event_type="runtime.stopping",
        aggregate_id="runner-1",
        payload={},
    )
    checkpoint_attestation = journal.attestation()
    journal.append(
        timestamp=NOW,
        event_type="runtime.stopped",
        aggregate_id="runner-1",
        payload={},
    )
    journal.append(
        timestamp=NOW,
        event_type="runtime.configuring",
        aggregate_id="runner-1",
        payload={},
    )
    journal.verify_attestation(checkpoint_attestation)


def test_checkpoint_attestation_rejects_unsafe_extension() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={},
    )
    checkpoint_attestation = journal.attestation()
    journal.append(
        timestamp=NOW,
        event_type="market.processed",
        aggregate_id="BTCUSDT",
        symbol="BTCUSDT",
        payload={"event_id": "market-2"},
    )
    with pytest.raises(JournalIntegrityError, match="unsafe extension"):
        journal.verify_attestation(checkpoint_attestation)


def test_journal_rejects_reserved_payload_fields() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    with pytest.raises(ValueError, match="reserved field"):
        journal.append(
            timestamp=NOW,
            event_type="runtime.started",
            aggregate_id="runner-1",
            payload={"event_hash": "forged"},
        )


def test_existing_tampered_journal_fails_closed() -> None:
    store = MutableEventStore()
    journal = RuntimeEventJournal(run_id="run-1", store=store)
    event = journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={"state": "running"},
    )
    store.events[0] = EventEnvelope(
        event_id=event.event_id,
        run_id=event.run_id,
        sequence=event.sequence,
        timestamp=event.timestamp,
        event_type=event.event_type,
        source=event.source,
        payload={**event.payload, "state": "failed"},
        schema_version=event.schema_version,
    )
    with pytest.raises(JournalIntegrityError, match="hash chain"):
        RuntimeEventJournal(run_id="run-1", store=store)


def test_journal_rejects_timestamp_regression() -> None:
    journal = RuntimeEventJournal(run_id="run-1")
    journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={},
    )
    with pytest.raises(JournalIntegrityError, match="precedes"):
        journal.append(
            timestamp=datetime(2026, 7, 26, 11, 59, tzinfo=UTC),
            event_type="runtime.failed",
            aggregate_id="runner-1",
            payload={},
        )
