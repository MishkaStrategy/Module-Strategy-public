from datetime import datetime, timezone
from pathlib import Path

import pytest

from modular_core.audit import Journal
from modular_core.events import EventEnvelope, InMemoryEventStore, JsonLinesEventStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _emit(journal: Journal, *, entity_key: str | None = None):
    return journal.emit_event(
        timestamp=NOW,
        event_type="test.event",
        source="tests",
        payload={"ok": True},
        causation_id=(journal.last_event_id(entity_key) if entity_key is not None else None),
        entity_key=entity_key,
    )


def test_journal_restart_continues_sequence_and_causation() -> None:
    store = InMemoryEventStore()
    first = _emit(Journal(run_id="run-1", event_store=store), entity_key="order:1")
    resumed = Journal(run_id="run-1", event_store=store)
    second = _emit(resumed, entity_key="order:1")
    assert (first.sequence, second.sequence) == (1, 2)
    assert second.event_id == "evt-0000000002"
    assert second.causation_id == first.event_id
    assert tuple(event.event_id for event in store.read()) == (
        "evt-0000000001",
        "evt-0000000002",
    )


def test_jsonl_reopen_preserves_entity_cursor_and_no_duplicate_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    first = _emit(
        Journal(run_id="run-1", event_store=JsonLinesEventStore(path, fsync=True)),
        entity_key="setup:s1",
    )
    reopened = JsonLinesEventStore(path)
    second = _emit(Journal(run_id="run-1", event_store=reopened), entity_key="setup:s1")
    assert second.sequence == 2
    assert second.causation_id == first.event_id
    assert [event.entity_key for event in reopened.read()] == ["setup:s1", "setup:s1"]


def test_old_jsonl_rows_without_entity_key_remain_readable(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event_id":"legacy-1","event_type":"legacy","payload":{},'
        '"run_id":"run-1","schema_version":"1","sequence":1,'
        '"source":"tests","timestamp":"2026-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    store = JsonLinesEventStore(path)
    assert store.read()[0].entity_key is None
    assert Journal(run_id="run-1", event_store=store).events[0].event_id == "legacy-1"


def test_journal_rejects_mismatched_or_gapped_stream() -> None:
    mismatch = InMemoryEventStore()
    _emit(Journal(run_id="run-1", event_store=mismatch))
    with pytest.raises(ValueError, match="run_id mismatch"):
        Journal(run_id="run-2", event_store=mismatch)

    gapped = InMemoryEventStore()
    gapped.append(EventEnvelope("event-1", "run-1", 1, NOW, "test", "tests", {}))
    gapped.append(EventEnvelope("event-3", "run-1", 3, NOW, "test", "tests", {}))
    with pytest.raises(ValueError, match="sequence_gap"):
        Journal(run_id="run-1", event_store=gapped)


def test_journal_rejects_next_deterministic_id_collision() -> None:
    store = InMemoryEventStore()
    store.append(EventEnvelope("evt-0000000002", "run-1", 1, NOW, "test", "tests", {}))
    with pytest.raises(ValueError, match="collides"):
        Journal(run_id="run-1", event_store=store)


def test_entity_key_validation_is_strict() -> None:
    with pytest.raises(ValueError, match="entity_key"):
        EventEnvelope("event-1", "run-1", 1, NOW, "test", "tests", {}, entity_key=" ")
    with pytest.raises(ValueError, match="entity_key"):
        Journal(run_id="run-1").last_event_id(" ")
