from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from modular_backtest.scalability import PerformanceConfig, ScalableJournal, StreamingEventStore
from modular_core.features import Feature


def _journal(tmp_path: Path, *, mode: str) -> ScalableJournal:
    return ScalableJournal(
        run_id=f"run-{mode}",
        event_store=StreamingEventStore(tmp_path / f"{mode}.jsonl", retained_window=4),
        performance=PerformanceConfig(mode=mode, max_memory_mb=None),
    )


def test_research_fast_keeps_feature_events_without_growing_causal_index(tmp_path: Path) -> None:
    journal = _journal(tmp_path, mode="research_fast")
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)

    for index in range(100):
        feature = Feature(
            key="test.value",
            source_module="test",
            created_at=timestamp,
            value=index,
            feature_id=f"feat-{index:08d}",
        )
        journal.log_feature(timestamp, feature, "published")

    assert journal.event_count == 100
    assert journal.streaming_store.retained_count == 4
    assert journal.streaming_store.counts_by_type == {"feature.published": 100}
    assert journal.retention_metrics["journal_index_entries"] == 0
    assert journal.last_event_id("feature:feat-00000099") is None
    journal.streaming_store.cleanup()


def test_canonical_audit_preserves_feature_entity_lookup(tmp_path: Path) -> None:
    journal = _journal(tmp_path, mode="canonical_audit")
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    feature = Feature(
        key="test.value",
        source_module="test",
        created_at=timestamp,
        value=1,
        feature_id="feat-canonical",
    )

    event = journal.log_feature(timestamp, feature, "published")

    assert journal.last_event_id("feature:feat-canonical") == event.event_id
    assert journal.retention_metrics["journal_index_entries"] == 1
    journal.streaming_store.cleanup()


def test_research_fast_preserves_setup_and_order_causal_lookup(tmp_path: Path) -> None:
    journal = _journal(tmp_path, mode="research_fast")
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)

    setup = journal.emit_event(
        timestamp=timestamp,
        event_type="setup.created",
        source="test",
        payload={},
        entity_key="setup:setup-1",
    )
    order = journal.emit_event(
        timestamp=timestamp,
        event_type="order.submitted",
        source="test",
        payload={},
        causation_id=journal.last_event_id("setup:setup-1"),
        entity_key="order:order-1",
    )
    fill = journal.emit_event(
        timestamp=timestamp,
        event_type="fill.recorded",
        source="test",
        payload={},
        causation_id=journal.last_event_id("order:order-1"),
        entity_key="fill:fill-1",
    )

    assert order.causation_id == setup.event_id
    assert fill.causation_id == order.event_id
    assert journal.last_event_id("setup:setup-1") == setup.event_id
    assert journal.last_event_id("order:order-1") == order.event_id
    assert journal.retention_metrics["journal_index_entries"] == 3
    journal.streaming_store.cleanup()
