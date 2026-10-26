from __future__ import annotations

import errno
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_backtest.scalability import PerformanceConfig, ScalableJournal, StreamingEventStore
from modular_core.events import EventEnvelope


def _event(sequence: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{sequence:010d}",
        run_id="run-publication",
        sequence=sequence,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="test.event",
        source="test",
        payload={"sequence": sequence},
    )


def test_event_stream_finalization_reuses_inode_and_cleanup_preserves_result(
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool" / "events.jsonl"
    destination = tmp_path / "result" / "events.jsonl"
    store = StreamingEventStore(spool)
    store.append_many((_event(1), _event(2)))
    source_inode = spool.stat().st_ino

    store.finalize_to(destination)

    assert destination.stat().st_ino == source_inode
    assert destination.read_text(encoding="utf-8") == spool.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="event store is closed"):
        store.append(_event(3))

    store.cleanup()

    assert not spool.exists()
    assert destination.exists()
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 2


def test_event_stream_finalization_fails_closed_across_filesystems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool" / "events.jsonl"
    destination = tmp_path / "result" / "events.jsonl"
    store = StreamingEventStore(spool)
    store.append(_event(1))

    def reject_cross_device_link(source: object, target: object) -> None:
        del source, target
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", reject_cross_device_link)

    with pytest.raises(OSError, match="same filesystem"):
        store.finalize_to(destination)

    assert spool.exists()
    assert not destination.exists()
    with pytest.raises(ValueError, match="event store is closed"):
        store.append(_event(2))
    store.cleanup()


def test_scalable_export_hashes_artifacts_without_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StreamingEventStore(tmp_path / "spool" / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-publication",
        event_store=store,
        performance=PerformanceConfig(max_memory_mb=None),
    )
    journal.emit_event(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="test.event",
        source="test",
        payload={"sequence": 1},
    )

    def reject_materialized_file_read(path: Path) -> bytes:
        raise AssertionError(f"Path.read_bytes() is forbidden for scalable artifacts: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject_materialized_file_read)

    output = journal.export_directory(tmp_path / "result")

    assert (output / "events.jsonl").is_file()
    checksum_text = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "events.jsonl" in checksum_text
    store.cleanup()
