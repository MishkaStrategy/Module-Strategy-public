from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from modular_core.events import EventEnvelope, InMemoryEventStore, JsonLinesEventStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(sequence: int = 1, *, event_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or f"event-{sequence}",
        run_id="run-1",
        sequence=sequence,
        timestamp=NOW,
        event_type="test.event",
        source="tests",
        payload={"sequence": sequence},
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("fsync", 1),
        ("max_line_bytes", True),
        ("max_line_bytes", 1.5),
        ("max_log_bytes", False),
    ],
)
def test_event_store_rejects_invalid_operational_types(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    kwargs = {field_name: value}
    with pytest.raises(TypeError):
        JsonLinesEventStore(tmp_path / "events.jsonl", **kwargs)  # type: ignore[arg-type]


def test_event_store_reopen_does_not_coerce_sequence_strings(tmp_path: Path) -> None:
    row = {
        "event_id": "event-1",
        "run_id": "run-1",
        "sequence": "1",
        "timestamp": NOW.isoformat(),
        "event_type": "test.event",
        "source": "tests",
        "payload": {},
    }
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sequence must be int"):
        JsonLinesEventStore(path)


def test_event_store_rejects_symlink_on_reopen(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    alias = tmp_path / "events.jsonl"
    try:
        alias.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlink"):
        JsonLinesEventStore(alias)


def test_event_store_streaming_reopen_enforces_line_limit(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"x" * 65 + b"\n")
    with pytest.raises(ValueError, match="line exceeds size limit"):
        JsonLinesEventStore(path, max_line_bytes=64)


def test_event_store_reopen_enforces_total_log_limit(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"{}\n" * 20)
    with pytest.raises(ValueError, match="max_log_bytes"):
        JsonLinesEventStore(path, max_log_bytes=32)


def test_rejected_append_does_not_advance_in_memory_state(tmp_path: Path) -> None:
    store = JsonLinesEventStore(
        tmp_path / "events.jsonl",
        max_line_bytes=1024,
        max_log_bytes=10,
    )
    with pytest.raises(ValueError, match="max_log_bytes"):
        store.append(_event())
    assert store.read() == ()


def test_successful_append_reopens_with_exact_identity(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = JsonLinesEventStore(path, fsync=True)
    first = _event(1)
    second = _event(2)
    store.append(first)
    store.append(second)

    reopened = JsonLinesEventStore(path)
    assert reopened.read() == (first, second)
    assert reopened.read(after_sequence=1) == (second,)


@pytest.mark.parametrize(
    ("value", "expected_exception"),
    [
        (True, TypeError),
        (-1, ValueError),
        (1.5, TypeError),
    ],
)
def test_event_store_read_validates_after_sequence(
    value: object,
    expected_exception: type[Exception],
) -> None:
    store = InMemoryEventStore()
    store.append(_event())
    with pytest.raises(expected_exception):
        store.read(after_sequence=value)  # type: ignore[arg-type]
