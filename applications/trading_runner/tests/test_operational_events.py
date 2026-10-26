from __future__ import annotations

from datetime import UTC, datetime

import pytest
from modular_core import EventEnvelope

from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.operational_events import (
    OperationalEventQueryError,
    OperationalEventQueryErrorCode,
    OperationalSeverity,
    TradingRunnerOperationalEventQuery,
)

NOW = datetime(2026, 8, 8, 17, 15, tzinfo=UTC)


class MutableEventStore:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    def append(self, event: EventEnvelope) -> None:
        self.events.append(event)

    def append_many(self, events: tuple[EventEnvelope, ...]) -> None:
        self.events.extend(events)

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        return tuple(event for event in self.events if event.sequence > after_sequence)


def append(
    journal: RuntimeEventJournal,
    event_type: str,
    payload: dict[str, object],
) -> EventEnvelope:
    return journal.append(
        timestamp=NOW,
        event_type=event_type,
        aggregate_id="runner-1",
        payload=payload,
    )


def test_runtime_failure_drops_raw_error_and_private_path() -> None:
    journal = RuntimeEventJournal(run_id="run-private-safe")
    append(
        journal,
        "runtime.failed",
        {
            "error_type": "BootstrapError",
            "error": "/Users/private/runtime.db token=TOPSECRET",
        },
    )
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    page = query.query()

    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type == "runtime.failed"
    assert event.severity is OperationalSeverity.ERROR
    assert event.state == "failed"
    assert dict(event.payload) == {"error_type": "BootstrapError"}
    text = repr(event).lower()
    assert "topsecret" not in text
    assert "/users/" not in text
    assert "runtime.db" not in text


def test_kill_switch_events_drop_free_form_reason() -> None:
    journal = RuntimeEventJournal(run_id="run-kill")
    append(
        journal,
        "kill_switch.activated",
        {
            "reason": "/home/private/token=TOPSECRET",
            "scope": "global",
            "cancellation_pending": False,
        },
    )
    append(
        journal,
        "kill_switch.cancellation_retry",
        {
            "reason": "browser supplied secret TOPSECRET",
            "succeeded": True,
        },
    )
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    page = query.query()

    assert [dict(event.payload) for event in page.events] == [
        {"scope": "global", "cancellation_pending": False},
        {"succeeded": True},
    ]
    assert all("topsecret" not in repr(event).lower() for event in page.events)
    assert all("reason" not in event.payload for event in page.events)


def test_unsupported_raw_owner_event_is_omitted_and_cursor_advances() -> None:
    journal = RuntimeEventJournal(run_id="run-unsupported")
    append(
        journal,
        "notification.failed",
        {
            "url": "https://api.telegram.org/botTOPSECRET/sendMessage",
            "path": "/Users/private/log.txt",
            "stack": "raw stack trace",
        },
    )
    append(journal, "runtime.stopped", {})
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    first = query.query(limit=1)

    assert first.scanned_count == 2
    assert first.omitted_count == 1
    assert [event.event_type for event in first.events] == ["runtime.stopped"]
    assert first.next_cursor == first.events[0].cursor
    assert first.has_more is False
    assert "topsecret" not in repr(first).lower()
    assert "/users/" not in repr(first).lower()


def test_operator_lifecycle_projection_is_strict_scalar_allowlist() -> None:
    journal = RuntimeEventJournal(run_id="run-operator")
    append(
        journal,
        "operator.lifecycle",
        {
            "operation": "ACTIVATE_KILL_SWITCH",
            "resource": "runner-1",
            "request_id": "kill-001",
            "operator": "control-plane:local-operator",
            "result": "SUCCEEDED",
            "previous_state": "running",
            "new_state": "kill_switch_active",
            "config_hash": "a" * 64,
            "replayed": False,
            "secret": "TOPSECRET",
            "private_path": "/Users/private/runtime.db",
            "nested": {"raw": "payload"},
        },
    )
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    event = query.query().events[0]

    assert dict(event.payload) == {
        "operation": "ACTIVATE_KILL_SWITCH",
        "request_id": "kill-001",
        "operator": "control-plane:local-operator",
        "result": "SUCCEEDED",
        "previous_state": "running",
        "new_state": "kill_switch_active",
        "config_hash": "a" * 64,
        "replayed": False,
    }
    assert event.state == "kill_switch_active"
    assert event.severity is OperationalSeverity.INFO
    assert all(
        value is None or isinstance(value, (str, int, float, bool))
        for value in event.payload.values()
    )
    assert "topsecret" not in repr(event).lower()
    assert "/users/" not in repr(event).lower()


def test_pagination_cursor_is_source_bound_and_stateless() -> None:
    journal = RuntimeEventJournal(run_id="run-page-a")
    append(journal, "runtime.configuring", {"config_hash": "b" * 64})
    append(journal, "runtime.started", {"state": "running"})
    append(journal, "runtime.stopping", {})
    append(journal, "runtime.stopped", {})
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    first = query.query(limit=2)
    second = query.query(cursor=first.next_cursor, limit=2)

    assert [event.event_type for event in first.events] == [
        "runtime.configuring",
        "runtime.started",
    ]
    assert first.has_more is True
    assert [event.event_type for event in second.events] == [
        "runtime.stopping",
        "runtime.stopped",
    ]
    assert second.has_more is False
    assert second.next_cursor == second.events[-1].cursor
    assert first.next_cursor != second.next_cursor

    other = TradingRunnerOperationalEventQuery(
        RuntimeEventJournal(run_id="run-page-b"),
        resource="runner-1",
    )
    with pytest.raises(OperationalEventQueryError) as wrong_stream:
        other.query(cursor=first.next_cursor)
    assert wrong_stream.value.code is OperationalEventQueryErrorCode.INVALID_CURSOR


def test_malformed_ahead_cursor_and_limit_fail_closed() -> None:
    journal = RuntimeEventJournal(run_id="run-cursor")
    append(journal, "runtime.stopped", {})
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")
    base_cursor = query.query().next_cursor
    prefix = base_cursor.rsplit(":", 1)[0]

    for cursor in ("", "../private.log", "trr-v1:TOPSECRET:1", f"{prefix}:999999"):
        with pytest.raises(OperationalEventQueryError) as raised:
            query.query(cursor=cursor)
        assert raised.value.code is OperationalEventQueryErrorCode.INVALID_CURSOR
        assert "topsecret" not in str(raised.value).lower()
        assert "private.log" not in str(raised.value).lower()

    for limit in (0, 201, True, -1):
        with pytest.raises(OperationalEventQueryError) as raised:
            query.query(limit=limit)  # type: ignore[arg-type]
        assert raised.value.code is OperationalEventQueryErrorCode.INVALID_REQUEST


def test_unsafe_resource_is_rejected_without_echo() -> None:
    journal = RuntimeEventJournal(run_id="run-resource")

    with pytest.raises(OperationalEventQueryError) as raised:
        TradingRunnerOperationalEventQuery(
            journal,
            resource="/Users/private/runtime-token-TOPSECRET",
        )

    assert raised.value.code is OperationalEventQueryErrorCode.INVALID_REQUEST
    text = str(raised.value).lower()
    assert "topsecret" not in text
    assert "/users/" not in text


class FailingStore:
    def append(self, event: EventEnvelope) -> None:
        raise AssertionError("append is not expected")

    def append_many(self, events: tuple[EventEnvelope, ...]) -> None:
        raise AssertionError("append_many is not expected")

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        raise OSError("/Users/private/events.db token=TOPSECRET")


def test_unavailable_owner_store_suppresses_private_failure() -> None:
    journal = RuntimeEventJournal(run_id="run-store")
    append(journal, "runtime.stopped", {})
    journal.store = FailingStore()
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    with pytest.raises(OperationalEventQueryError) as raised:
        query.query()

    assert raised.value.code is OperationalEventQueryErrorCode.SOURCE_UNAVAILABLE
    assert raised.value.__cause__ is None
    text = str(raised.value).lower()
    assert "topsecret" not in text
    assert "/users/" not in text


def test_tampered_hash_chain_is_source_unavailable() -> None:
    store = MutableEventStore()
    journal = RuntimeEventJournal(run_id="run-tamper", store=store)
    original = append(journal, "runtime.started", {"state": "running"})
    store.events[0] = EventEnvelope(
        event_id=original.event_id,
        run_id=original.run_id,
        sequence=original.sequence,
        timestamp=original.timestamp,
        event_type=original.event_type,
        source=original.source,
        payload={**original.payload, "state": "failed"},
        schema_version=original.schema_version,
    )
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    with pytest.raises(OperationalEventQueryError) as raised:
        query.query()

    assert raised.value.code is OperationalEventQueryErrorCode.SOURCE_UNAVAILABLE


def test_retention_metadata_matches_owner_without_synthetic_truncation() -> None:
    journal = RuntimeEventJournal(run_id="run-retention")
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    page = query.query()

    assert page.events == ()
    assert page.next_cursor.endswith(":0")
    assert page.scanned_count == 0
    assert page.omitted_count == 0
    assert page.retention_limit == 1_000_000
    assert page.truncated is False
    assert page.has_more is False
