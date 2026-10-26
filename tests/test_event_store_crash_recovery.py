from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from modular_core.events import EventEnvelope, JsonLinesEventStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(sequence: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"event-{sequence}",
        run_id="run-1",
        sequence=sequence,
        timestamp=NOW,
        event_type="test.event",
        source="tests",
        payload={"sequence": sequence},
    )


def test_truncated_final_record_is_fail_closed_by_default(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = JsonLinesEventStore(path)
    store.append(_event(1))
    with path.open("ab") as handle:
        handle.write(b'{"event_id":')

    with pytest.raises(ValueError, match="invalid event log"):
        JsonLinesEventStore(path)


def test_explicit_tail_recovery_truncates_only_invalid_unterminated_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    store = JsonLinesEventStore(path, fsync=True)
    first = _event(1)
    store.append(first)
    valid_size = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b'{"event_id":')

    recovered = JsonLinesEventStore(
        path,
        fsync=True,
        recover_truncated_tail=True,
    )

    assert recovered.read() == (first,)
    assert recovered.recovered_truncated_tail is True
    assert path.stat().st_size == valid_size

    second = _event(2)
    recovered.append(second)
    strict_reopen = JsonLinesEventStore(path)
    assert strict_reopen.read() == (first, second)


def test_recovery_does_not_hide_corrupted_terminated_record(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = JsonLinesEventStore(path)
    store.append(_event(1))
    with path.open("ab") as handle:
        handle.write(b'{"event_id":}\n')

    with pytest.raises(ValueError, match="invalid event log"):
        JsonLinesEventStore(path, recover_truncated_tail=True)


def test_recovery_does_not_hide_internal_corruption(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first = _event(1)
    second = _event(2)
    rows = [
        json.dumps(
            {
                "event_id": first.event_id,
                "run_id": first.run_id,
                "sequence": first.sequence,
                "timestamp": first.timestamp.isoformat(),
                "event_type": first.event_type,
                "source": first.source,
                "payload": dict(first.payload),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n",
        b'{"event_id":}\n',
        json.dumps(
            {
                "event_id": second.event_id,
                "run_id": second.run_id,
                "sequence": second.sequence,
                "timestamp": second.timestamp.isoformat(),
                "event_type": second.event_type,
                "source": second.source,
                "payload": dict(second.payload),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n",
    ]
    path.write_bytes(b"".join(rows))

    with pytest.raises(ValueError, match="line 2"):
        JsonLinesEventStore(path, recover_truncated_tail=True)


def test_valid_final_record_without_newline_is_not_treated_as_crash_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    event = _event(1)
    payload = {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type,
        "source": event.source,
        "payload": dict(event.payload),
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    reopened = JsonLinesEventStore(path, recover_truncated_tail=True)
    assert reopened.read() == (event,)
    assert reopened.recovered_truncated_tail is False


def test_recovery_flag_requires_real_boolean(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="recover_truncated_tail"):
        JsonLinesEventStore(
            tmp_path / "events.jsonl",
            recover_truncated_tail=1,  # type: ignore[arg-type]
        )
