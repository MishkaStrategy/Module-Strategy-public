from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.operational_events import (
    OperationalEventQueryError,
    OperationalEventQueryErrorCode,
    OperationalSeverity,
    TradingRunnerOperationalEventQuery,
)

NOW = datetime(2026, 8, 8, 17, 30, tzinfo=UTC)


def append(journal: RuntimeEventJournal, event_type: str, payload: dict[str, object]) -> None:
    journal.append(
        timestamp=NOW,
        event_type=event_type,
        aggregate_id="runner-1",
        payload=payload,
    )


def test_event_type_and_severity_filters_preserve_owner_cursor_semantics() -> None:
    journal = RuntimeEventJournal(run_id="run-filter")
    append(journal, "runtime.started", {"state": "running"})
    append(
        journal,
        "kill_switch.activated",
        {"scope": "global", "cancellation_pending": False, "reason": "not projected"},
    )
    append(
        journal,
        "runtime.failed",
        {"error_type": "BootstrapError", "error": "/Users/private/TOPSECRET"},
    )
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    warning = query.query(severities=(OperationalSeverity.WARNING,))
    failed = query.query(event_types=("runtime.failed",))

    assert [event.event_type for event in warning.events] == ["kill_switch.activated"]
    assert warning.scanned_count == 3
    assert warning.omitted_count == 2
    assert warning.has_more is False
    assert [event.event_type for event in failed.events] == ["runtime.failed"]
    assert failed.scanned_count == 3
    assert failed.omitted_count == 2
    assert failed.next_cursor.endswith(":3")
    assert "topsecret" not in repr(failed).lower()
    assert "/users/" not in repr(failed).lower()


def test_filter_validation_is_typed_and_bounded() -> None:
    query = TradingRunnerOperationalEventQuery(
        RuntimeEventJournal(run_id="run-filter-validation"),
        resource="runner-1",
    )

    invalid_calls = (
        lambda: query.query(event_types=()),
        lambda: query.query(event_types=("runtime.started", "runtime.started")),
        lambda: query.query(event_types=("unknown.raw.event",)),
        lambda: query.query(severities=()),
        lambda: query.query(
            severities=("ERROR",),  # type: ignore[arg-type]
        ),
        lambda: query.query(
            severities=(OperationalSeverity.ERROR, OperationalSeverity.ERROR),
        ),
    )
    for call in invalid_calls:
        with pytest.raises(OperationalEventQueryError) as raised:
            call()
        assert raised.value.code is OperationalEventQueryErrorCode.INVALID_REQUEST


def test_scan_budget_bounds_sparse_filtered_pages() -> None:
    journal = RuntimeEventJournal(run_id="run-scan-budget")
    for index in range(1_001):
        append(
            journal,
            "notification.failed",
            {
                "event_id": f"notification-{index}",
                "raw": "/Users/private/token=TOPSECRET",
            },
        )
    append(journal, "runtime.stopped", {})
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")

    first = query.query(event_types=("runtime.stopped",), limit=1)
    second = query.query(
        cursor=first.next_cursor,
        event_types=("runtime.stopped",),
        limit=1,
    )

    assert first.events == ()
    assert first.scanned_count == 1_000
    assert first.omitted_count == 1_000
    assert first.has_more is True
    assert first.next_cursor.endswith(":1000")
    assert [event.event_type for event in second.events] == ["runtime.stopped"]
    assert second.scanned_count == 2
    assert second.omitted_count == 1
    assert second.has_more is False
    assert "topsecret" not in repr(first).lower()
    assert "/users/" not in repr(first).lower()
