from datetime import UTC, datetime, timedelta

from modular_core import (
    EventEnvelope,
    Feature,
    FeatureStore,
    InMemoryEventStore,
    ReadOnlySeries,
)


def test_feature_snapshot_is_reused_until_store_mutates() -> None:
    store = FeatureStore()
    now = datetime.now(UTC)
    store.publish(Feature("a.value", "a", now, 1))
    first = store.snapshot(now)
    second = store.snapshot(now)
    assert first is second
    assert store.snapshot_builds == 1

    store.publish(Feature("b.value", "b", now, 2))
    third = store.snapshot(now)
    assert third is not first
    assert store.snapshot_builds == 2


def test_event_store_reads_from_sequence_boundary() -> None:
    store = InMemoryEventStore()
    now = datetime.now(UTC)
    for sequence in (10, 20, 30):
        store.append(
            EventEnvelope(
                f"e{sequence}",
                "run",
                sequence,
                now + timedelta(seconds=sequence),
                "test",
                "test",
                {"sequence": sequence},
            )
        )
    assert [event.sequence for event in store.read(after_sequence=10)] == [20, 30]
    assert [event.sequence for event in store.read(after_sequence=25)] == [30]
    assert store.read(after_sequence=30) == ()


def test_read_only_series_window_captures_start_and_end() -> None:
    source = [1, 2, 3, 4]
    window = ReadOnlySeries(source, 3, start=1)

    source.append(5)

    assert len(window) == 2
    assert tuple(window) == (2, 3)
    assert window[0] == 2
    assert window[-1] == 3
    assert window[:] == (2, 3)


def test_event_store_rejected_batch_preserves_existing_stream() -> None:
    store = InMemoryEventStore()
    now = datetime.now(UTC)
    first = EventEnvelope("e1", "run", 1, now, "test", "test", {})
    second = EventEnvelope("e2", "run", 2, now, "test", "test", {})
    duplicate = EventEnvelope("e1", "run", 3, now, "test", "test", {})
    store.append(first)

    import pytest

    with pytest.raises(ValueError, match="duplicate"):
        store.append_many((second, duplicate))

    assert store.read() == (first,)
