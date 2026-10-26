from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from modular_backtest.recovery import (
    RUNTIME_SNAPSHOT_SCHEMA_VERSION,
    ReplayRecoveryError,
    RuntimeSnapshot,
    _RESTORE_EVENT_STREAM_PATH,
    _file_sha256,
    _reduce_streaming_event_store,
    _restore_streaming_event_store,
    _result_digest,
    _snapshot_integrity,
)
from modular_backtest.scalability import StreamingEventStore
from modular_core.engine import KERNEL_VERSION, BacktestResult
from modular_core.events import EventEnvelope


def _event(sequence: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{sequence:010d}",
        run_id="runtime-snapshot-gzip",
        sequence=sequence,
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        event_type="feature.published",
        source="snapshot-test",
        payload={"sequence": sequence, "payload": "repeatable-value" * 20},
        symbol="BTCUSDT",
        entity_key=f"feature:{sequence}",
    )


def _rows(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def _restore_from_v1_reducer(store: StreamingEventStore) -> StreamingEventStore:
    restore_function, restore_args = _reduce_streaming_event_store(store)
    assert restore_function is _restore_streaming_event_store
    assert restore_args[7] == 1
    assert restore_args[8] == "gzip"
    assert restore_args[9] == store.logical_bytes_written
    assert restore_args[10] == store.semantic_sha256
    store.close()
    token = _RESTORE_EVENT_STREAM_PATH.set(store.path)
    try:
        return _restore_streaming_event_store(*restore_args)
    finally:
        _RESTORE_EVENT_STREAM_PATH.reset(token)


def _fake_result(store: StreamingEventStore) -> BacktestResult:
    journal = SimpleNamespace(
        streaming_store=store,
        events=(),
        orders=(),
        fills=(),
        trades=(),
        equity=(),
        setups=(),
        setup_records=(),
        features=(),
        signals=(),
        skips=(),
        diagnostics=(),
        errors=(),
    )
    return cast(
        BacktestResult,
        SimpleNamespace(
            metrics={},
            bars_processed=2,
            modules=(),
            run_manifest=None,
            module_manifests=(),
            checkpoint=None,
            journal=journal,
        ),
    )


def test_runtime_snapshot_v1_reducer_restores_gzip_stream_and_continues_semantically(
    tmp_path: Path,
) -> None:
    continuous = StreamingEventStore(tmp_path / "continuous.gz", compression="gzip")
    continuous.append_many((_event(1), _event(2)))
    continuous.close()

    snapshotted = StreamingEventStore(tmp_path / "snapshotted.gz", compression="gzip")
    snapshotted.append(_event(1))
    restored = _restore_from_v1_reducer(snapshotted)

    # runtime_snapshot_v1 remains readable, but new snapshots carry semantic
    # metadata so continuation does not have to rescan the accumulated gzip stream.
    assert restored.compression == "gzip"
    assert restored.encoding == "gzip"
    assert len(restored.semantic_sha256) == 64

    restored.append(_event(2))
    restored.close()

    assert _rows(restored.path) == _rows(continuous.path)
    assert restored.semantic_sha256 == continuous.semantic_sha256
    assert restored.logical_bytes_written == continuous.logical_bytes_written
    assert restored.bytes_written < restored.logical_bytes_written


def test_result_digest_ignores_restart_dependent_gzip_member_boundaries(tmp_path: Path) -> None:
    continuous = StreamingEventStore(tmp_path / "digest-continuous.gz", compression="gzip")
    continuous.append_many((_event(1), _event(2)))

    restarted = StreamingEventStore(tmp_path / "digest-restarted.gz", compression="gzip")
    restarted.append(_event(1))
    restarted.flush()
    restarted.append(_event(2))

    continuous_digest = _result_digest(_fake_result(continuous))
    restarted_digest = _result_digest(_fake_result(restarted))

    assert continuous.path.read_bytes() != restarted.path.read_bytes()
    assert continuous.semantic_sha256 == restarted.semantic_sha256
    assert continuous.logical_bytes_written == restarted.logical_bytes_written
    assert continuous_digest == restarted_digest

    continuous.close()
    restarted.close()


def test_runtime_snapshot_physical_sha_rejects_same_size_gzip_corruption(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "corrupt.gz", compression="gzip")
    store.append_many(tuple(_event(sequence) for sequence in range(1, 101)))
    store.close()

    payload_path = tmp_path / "runtime-snapshot-v1.pkl"
    payload_path.write_bytes(b"integrity-protected-payload")
    payload_sha256 = _file_sha256(payload_path)
    event_sha256 = _file_sha256(store.path)
    event_bytes = store.path.stat().st_size
    snapshot = RuntimeSnapshot(
        schema_version=RUNTIME_SNAPSHOT_SCHEMA_VERSION,
        kernel_version=KERNEL_VERSION,
        payload_path=payload_path,
        payload_sha256=payload_sha256,
        payload_bytes=payload_path.stat().st_size,
        event_stream_path=store.path,
        event_stream_sha256=event_sha256,
        event_stream_bytes=event_bytes,
        integrity_sha256=_snapshot_integrity(
            schema_version=RUNTIME_SNAPSHOT_SCHEMA_VERSION,
            kernel_version=KERNEL_VERSION,
            payload_sha256=payload_sha256,
            event_stream_sha256=event_sha256,
            event_stream_bytes=event_bytes,
        ),
    )

    encoded = bytearray(store.path.read_bytes())
    assert len(encoded) > 32
    encoded[len(encoded) // 2] ^= 0xFF
    store.path.write_bytes(encoded)
    assert store.path.stat().st_size == event_bytes

    with pytest.raises(ReplayRecoveryError, match="event stream integrity verification failed"):
        snapshot.verify()


def test_legacy_v1_reducer_still_detects_corrupt_gzip_when_semantic_metadata_is_absent(
    tmp_path: Path,
) -> None:
    store = StreamingEventStore(tmp_path / "legacy-corrupt.gz", compression="gzip")
    store.append_many(tuple(_event(sequence) for sequence in range(1, 101)))
    _, restore_args = _reduce_streaming_event_store(store)
    legacy_restore_args = restore_args[:7]
    store.close()

    encoded = bytearray(store.path.read_bytes())
    encoded[len(encoded) // 2] ^= 0xFF
    store.path.write_bytes(encoded)

    token = _RESTORE_EVENT_STREAM_PATH.set(store.path)
    try:
        restored = _restore_streaming_event_store(*legacy_restore_args)
    finally:
        _RESTORE_EVENT_STREAM_PATH.reset(token)

    with pytest.raises(ValueError, match="not readable"):
        restored.close()
