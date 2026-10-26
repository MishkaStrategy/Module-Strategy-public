from datetime import UTC, datetime, timedelta

import pytest

from modular_core import (
    AtomicJsonStateRepository,
    EventEnvelope,
    JsonLinesEventStore,
    verify_event_stream,
)


def event(sequence: int, *, causation_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        f"evt-{sequence}",
        "run-1",
        sequence,
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=sequence),
        "test",
        "test",
        {"nested": {"values": [sequence]}},
        causation_id=causation_id,
    )


def test_event_payload_is_deeply_immutable() -> None:
    row = event(1)
    with pytest.raises(TypeError):
        row.payload["nested"]["x"] = 1
    assert row.payload["nested"]["values"] == (1,)


def test_json_lines_event_store_round_trip(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    store = JsonLinesEventStore(path)
    store.append(event(1))
    store.append(event(2, causation_id="evt-1"))
    reopened = JsonLinesEventStore(path)
    assert [row.event_id for row in reopened.read()] == ["evt-1", "evt-2"]
    assert verify_event_stream(reopened.read()).valid


def test_replay_verifier_rejects_future_causation() -> None:
    report = verify_event_stream((event(1, causation_id="evt-2"), event(2)))
    assert not report.valid
    assert "unknown_or_future_causation" in report.errors[0]


def test_atomic_json_state_repository_round_trip(tmp_path) -> None:
    repo = AtomicJsonStateRepository(tmp_path)
    repo.save("kernel", {"sequence": 10, "state": {"mode": "paper"}})
    assert repo.load("kernel") == {"sequence": 10, "state": {"mode": "paper"}}
    repo.delete("kernel")
    assert repo.load("kernel") is None
