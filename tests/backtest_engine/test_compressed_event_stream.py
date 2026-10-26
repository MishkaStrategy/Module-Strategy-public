from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_backtest.scalability import PerformanceConfig, ScalableJournal, StreamingEventStore
from modular_core.events import EventEnvelope


def _event(sequence: int, *, run_id: str = "run-compressed") -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{sequence:010d}",
        run_id=run_id,
        sequence=sequence,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="feature.published",
        source="feature-module",
        payload={
            "feature": {
                "feature_id": f"feature-{sequence:010d}",
                "symbol": "BTCUSDT",
                "timeframe": "15m",
                "source_module": "feature-module",
                "kind": "fvg.active_zone",
                "values": {
                    "direction": "bullish",
                    "proximal": "50000.00000000",
                    "distal": "49950.00000000",
                    "formed_at": "2024-01-01T00:00:00+00:00",
                },
            }
        },
        symbol="BTCUSDT",
        entity_key=f"feature:feature-{sequence:010d}",
    )


def _read_gzip_rows(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def test_gzip_stream_round_trips_every_event_and_keeps_registry_headroom(
    tmp_path: Path,
) -> None:
    store = StreamingEventStore(tmp_path / "events.spool")
    store.enable_compression("gzip")
    for sequence in range(1, 10_001):
        store.append(_event(sequence))
    store.close()

    rows = _read_gzip_rows(store.path)
    assert len(rows) == 10_000
    assert rows[0]["event_id"] == "evt-0000000001"
    assert rows[-1]["event_id"] == "evt-0000010000"
    assert [int(row["sequence"]) for row in rows] == list(range(1, 10_001))
    assert store.logical_bytes_written > 0
    # The Registry package contract caps a single artifact at 16 GiB. The failed
    # physical run produced a triple-digit-GiB plain event stream, so a 4x gate
    # was not meaningful protection. Representative feature traffic must retain
    # at least 16x physical headroom while remaining lossless.
    assert store.bytes_written < store.logical_bytes_written // 16


def test_gzip_append_tracks_physical_bytes_without_per_event_fstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StreamingEventStore(tmp_path / "counted.spool", compression="gzip")
    real_fstat = os.fstat
    fstat_calls = 0

    def unexpected_fstat(descriptor: int) -> os.stat_result:
        del descriptor
        nonlocal fstat_calls
        fstat_calls += 1
        raise AssertionError("gzip append must not stat the spool per event")

    monkeypatch.setattr(os, "fstat", unexpected_fstat)
    store.append(_event(1))
    store.append(_event(2))

    assert fstat_calls == 0
    assert store.bytes_written > 0
    assert store.bytes_written == store.path.stat().st_size

    monkeypatch.setattr(os, "fstat", real_fstat)
    store.close()
    assert store.bytes_written == store.path.stat().st_size


def test_semantic_digest_is_independent_of_runtime_restart_member_boundary(
    tmp_path: Path,
) -> None:
    continuous = StreamingEventStore(tmp_path / "continuous.gz", compression="gzip")
    continuous.append(_event(1))
    continuous.append(_event(2))
    continuous.close()

    restarted = StreamingEventStore(tmp_path / "restarted.gz", compression="gzip")
    restarted.append(_event(1))
    restarted.flush()
    metadata = {
        "retained_window": 2_000,
        "retained": restarted.read(),
        "count": restarted.count,
        "last_sequence": restarted.last_sequence,
        "run_id": "run-compressed",
        "counts_by_type": restarted.counts_by_type,
        "bytes_written": restarted.bytes_written,
        "logical_bytes_written": restarted.logical_bytes_written,
        "semantic_sha256": restarted.semantic_sha256,
        "compression": restarted.compression,
    }
    restarted.close()
    metadata["bytes_written"] = restarted.bytes_written
    resumed = StreamingEventStore.restore_existing(restarted.path, **metadata)
    resumed.append(_event(2))
    resumed.close()

    assert continuous.semantic_sha256 == resumed.semantic_sha256
    assert continuous.logical_bytes_written == resumed.logical_bytes_written
    assert _read_gzip_rows(continuous.path) == _read_gzip_rows(resumed.path)
    # Physical gzip member boundaries are deliberately not part of semantic identity.
    assert continuous.path.read_bytes() != resumed.path.read_bytes()


def test_failed_append_cannot_be_published_after_store_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StreamingEventStore(tmp_path / "poisoned.spool", compression="gzip")

    def fail_write(payload: bytes) -> None:
        del payload
        raise OSError("simulated event stream write failure")

    monkeypatch.setattr(store, "_write_payload", fail_write)
    with pytest.raises(OSError, match="simulated event stream write failure"):
        store.append(_event(1))

    # Resource cleanup may close a poisoned store, but no later copy/hard-link
    # publication is allowed to turn its uncertain bytes into accepted evidence.
    store.close()
    copied = tmp_path / "copied.jsonl.gz"
    finalized = tmp_path / "finalized.jsonl.gz"
    with pytest.raises(OSError, match="unusable after a failed append"):
        store.copy_to(copied)
    with pytest.raises(OSError, match="unusable after a failed append"):
        store.finalize_to(finalized)
    assert not copied.exists()
    assert not finalized.exists()


def test_research_fast_exports_gzip_v3_while_canonical_audit_stays_plain(
    tmp_path: Path,
) -> None:
    fast_store = StreamingEventStore(tmp_path / "fast" / "events.spool")
    fast = ScalableJournal(
        run_id="run-compressed",
        event_store=fast_store,
        performance=PerformanceConfig(mode="research_fast", max_memory_mb=None),
    )
    fast.emit_event(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="run.started",
        source="kernel",
        payload={},
    )
    fast_output = fast.export_directory(tmp_path / "fast-result")
    fast_metadata = json.loads((fast_output / "journal.json").read_text(encoding="utf-8"))

    assert fast_store.compression == "gzip"
    assert fast_metadata["schema_version"] == "3"
    assert fast_metadata["event_stream"]["path"] == "events.jsonl.gz"
    assert fast_metadata["event_stream"]["encoding"] == "gzip"
    assert _read_gzip_rows(fast_output / "events.jsonl.gz")[0]["event_type"] == "run.started"
    assert not (fast_output / "events.jsonl").exists()

    audit_store = StreamingEventStore(tmp_path / "audit" / "events.jsonl")
    audit = ScalableJournal(
        run_id="run-audit",
        event_store=audit_store,
        performance=PerformanceConfig(mode="canonical_audit", max_memory_mb=None),
    )
    audit.emit_event(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="run.started",
        source="kernel",
        payload={},
    )
    audit_output = audit.export_directory(tmp_path / "audit-result")
    audit_metadata = json.loads((audit_output / "journal.json").read_text(encoding="utf-8"))

    assert audit_store.compression is None
    assert audit_metadata["schema_version"] == "2"
    assert audit_metadata["event_stream"]["path"] == "events.jsonl"
    assert audit_metadata["event_stream"]["encoding"] == "identity"
    assert (audit_output / "events.jsonl").is_file()
