from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from modular_backtest.scalability.buffers import BoundedRecordBuffer
from modular_backtest.scalability.config import PerformanceConfig
from modular_backtest.scalability.events import StreamingEventStore
from modular_backtest.scalability.journal import ScalableJournal
from modular_core.features import Feature
from modular_core.models import Side, Signal


def test_bounded_record_buffer_keeps_tail_with_sequence_semantics() -> None:
    buffer = BoundedRecordBuffer[int](2)
    buffer.append(1)
    buffer.append(2)
    buffer.append(3)

    assert buffer == [2, 3]
    assert buffer[0] == 2
    assert buffer[:] == [2, 3]
    assert buffer.to_list() == [2, 3]


def test_feature_and_signal_audit_remain_streamed_while_ram_tail_is_bounded(
    tmp_path: Path,
) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl", retained_window=2)
    journal = ScalableJournal(
        run_id="run-test",
        event_store=store,
        performance=PerformanceConfig(max_memory_mb=None, retained_event_window=2),
    )
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(3):
        journal.log_feature(
            timestamp,
            Feature(
                feature_id=f"feature-{index}",
                key="producer.trend",
                value=index,
                created_at=timestamp,
                source_module="producer",
                symbol="BTCUSDT",
            ),
            "published",
        )
        journal.log_signal(
            timestamp,
            Signal(
                side=Side.BUY,
                module="producer",
                reason=f"signal-{index}",
            ),
            symbol="BTCUSDT",
        )

    assert len(journal.features) == 2
    assert len(journal.signals) == 2
    assert store.counts_by_type == {
        "feature.published": 3,
        "signal.emitted": 3,
    }
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 6
    store.cleanup()
