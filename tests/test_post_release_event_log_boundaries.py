from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from modular_core.events import JsonLinesEventStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(sequence: object = 1) -> dict[str, object]:
    return {
        "event_id": f"event-{sequence}",
        "run_id": "run-1",
        "sequence": sequence,
        "timestamp": NOW.isoformat(),
        "event_type": "test.event",
        "source": "tests",
        "payload": {},
    }


def _encoded(row: dict[str, object]) -> bytes:
    return json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (
            b'{"event_id":"event-1","run_id":"run-1","sequence":1,'
            b'"timestamp":"2026-01-01T00:00:00+00:00",'
            b'"event_type":"test.event","source":"tests",'
            b'"payload":{},"payload":{"value":1}}\n',
            "duplicate JSON key",
        ),
        (
            b'{"event_id":"event-1","run_id":"run-1","sequence":1,'
            b'"timestamp":"2026-01-01T00:00:00+00:00",'
            b'"event_type":"test.event","source":"tests",'
            b'"payload":{"value":NaN}}\n',
            "non-finite JSON constant",
        ),
    ],
)
def test_event_log_rejects_ambiguous_json(
    tmp_path: Path,
    line: bytes,
    message: str,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(line)

    with pytest.raises(ValueError, match=message):
        JsonLinesEventStore(path)


def test_truncated_tail_recovery_removes_only_incomplete_json(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    committed = _encoded(_row()) + b"\n"
    path.write_bytes(committed + b'{"event_id":')

    store = JsonLinesEventStore(path, recover_truncated_tail=True)

    assert store.recovered_truncated_tail
    assert len(store.read()) == 1
    assert path.read_bytes() == committed


def test_truncated_tail_recovery_does_not_hide_semantic_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    committed = _encoded(_row()) + b"\n"
    corrupt = _encoded(_row("2"))
    original = committed + corrupt
    path.write_bytes(original)

    with pytest.raises(ValueError, match="event sequence must be int"):
        JsonLinesEventStore(path, recover_truncated_tail=True)

    assert path.read_bytes() == original


def test_truncated_tail_recovery_does_not_hide_duplicate_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    line = (
        b'{"event_id":"event-1","event_id":"event-2",'
        b'"run_id":"run-1","sequence":1,'
        b'"timestamp":"2026-01-01T00:00:00+00:00",'
        b'"event_type":"test.event","source":"tests","payload":{}}'
    )
    path.write_bytes(line)

    with pytest.raises(ValueError, match="duplicate JSON key"):
        JsonLinesEventStore(path, recover_truncated_tail=True)

    assert path.read_bytes() == line
