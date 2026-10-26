from __future__ import annotations

import hashlib
import pickle
from collections.abc import Sequence
from typing import cast

from modular_core.engine import BacktestResult
from modular_core.events import EventEnvelope

from . import _recovery_impl as _impl
from .scalability.engine import ScalableRunSession
from .scalability.events import StreamingEventStore


# Keep the established recovery API and pickle import paths stable while isolating
# the compressed-event compatibility contract below. The implementation module is
# the exact pre-contract recovery implementation moved without source changes.
ReplayRecoveryError = _impl.ReplayRecoveryError
ReplayCheckpoint = _impl.ReplayCheckpoint
ReplayRun = _impl.ReplayRun
RuntimeSnapshot = _impl.RuntimeSnapshot
EngineFactory = _impl.EngineFactory
ScalableEngineFactory = _impl.ScalableEngineFactory
RUNTIME_SNAPSHOT_SCHEMA_VERSION = _impl.RUNTIME_SNAPSHOT_SCHEMA_VERSION

_digest = _impl._digest
_file_sha256 = _impl._file_sha256
_snapshot_integrity = _impl._snapshot_integrity
_restore_mapping_proxy = _impl._restore_mapping_proxy
_reduce_mapping_proxy = _impl._reduce_mapping_proxy
_RESTORE_EVENT_STREAM_PATH = _impl._RESTORE_EVENT_STREAM_PATH
_serialize_runtime = _impl._serialize_runtime
_ORIGINAL_BUILD_RUNTIME_SNAPSHOT = _impl._build_runtime_snapshot
_load_runtime_snapshot = _impl._load_runtime_snapshot
_equity_digest = _impl._equity_digest
_ReplaySession = _impl._ReplaySession
_update_result_digest_value = _impl._update_result_digest_value
_update_result_digest_sequence = _impl._update_result_digest_sequence

# Preserve durable references created by older runtime_snapshot_v1 payloads.
for _public_type in (ReplayRecoveryError, ReplayCheckpoint, ReplayRun, RuntimeSnapshot):
    _public_type.__module__ = __name__


_LEGACY_RESTORE_STREAMING_EVENT_STORE = _impl._restore_streaming_event_store
_EVENT_STORE_SNAPSHOT_METADATA_VERSION = 1


def _build_runtime_snapshot(session: ScalableRunSession) -> RuntimeSnapshot:
    """Build through the public module so existing monkeypatch hooks remain valid."""

    return _ORIGINAL_BUILD_RUNTIME_SNAPSHOT(session)


def _build_runtime_snapshot_proxy(session: ScalableRunSession) -> RuntimeSnapshot:
    """Resolve the public builder dynamically for implementation-module callers."""

    return _build_runtime_snapshot(session)


def _restore_streaming_event_store(
    retained_window: int,
    retained: tuple[EventEnvelope, ...],
    count: int,
    last_sequence: int,
    run_id: str | None,
    counts_by_type: dict[str, int],
    bytes_written: int,
    metadata_version: int | None = None,
    compression: str | None = None,
    logical_bytes_written: int | None = None,
    semantic_sha256: str | None = None,
) -> StreamingEventStore:
    """Restore old and new runtime_snapshot_v1 event-store reducers.

    Legacy payloads carry only physical stream metadata, so they retain the old
    lazy reconstruction path. New payloads persist the semantic gzip metadata in
    the already integrity-protected snapshot payload and therefore avoid an O(n)
    decompression pass before continuation.
    """

    if metadata_version is None:
        return _LEGACY_RESTORE_STREAMING_EVENT_STORE(
            retained_window,
            retained,
            count,
            last_sequence,
            run_id,
            counts_by_type,
            bytes_written,
        )
    if metadata_version != _EVENT_STORE_SNAPSHOT_METADATA_VERSION:
        raise pickle.UnpicklingError(
            f"unsupported event store snapshot metadata version: {metadata_version}"
        )
    if logical_bytes_written is None or semantic_sha256 is None:
        raise pickle.UnpicklingError("event store snapshot semantic metadata is incomplete")
    path = _RESTORE_EVENT_STREAM_PATH.get()
    if path is None:
        raise pickle.UnpicklingError("event stream restore context is missing")
    try:
        return StreamingEventStore.restore_existing(
            path,
            retained_window=retained_window,
            retained=retained,
            count=count,
            last_sequence=last_sequence,
            run_id=run_id,
            counts_by_type=counts_by_type,
            bytes_written=bytes_written,
            logical_bytes_written=logical_bytes_written,
            semantic_sha256=semantic_sha256,
            compression=compression,
        )
    except (TypeError, ValueError) as exc:
        raise pickle.UnpicklingError("event store snapshot semantic metadata is invalid") from exc


def _reduce_streaming_event_store(
    store: StreamingEventStore,
) -> tuple[object, tuple[object, ...]]:
    store.flush()
    retained_window = store._retained.maxlen
    if retained_window is None:
        raise RuntimeError("streaming event store retained window is not bounded")
    return (
        _restore_streaming_event_store,
        (
            retained_window,
            tuple(store._retained),
            store._count,
            store._last_sequence,
            store._run_id,
            dict(store._counts_by_type),
            store._bytes_written,
            _EVENT_STORE_SNAPSHOT_METADATA_VERSION,
            store.compression,
            store.logical_bytes_written,
            store.semantic_sha256,
        ),
    )


def _result_digest(result: BacktestResult) -> str:
    """Hash exact semantic result evidence independent of gzip member boundaries."""

    digest = hashlib.sha256()
    for name, value in (
        ("metrics", result.metrics),
        ("bars_processed", result.bars_processed),
        ("modules", result.modules),
        ("run_manifest", result.run_manifest),
        ("module_manifests", result.module_manifests),
        ("checkpoint", result.checkpoint),
    ):
        _update_result_digest_value(digest, name, value)

    streaming_store = getattr(result.journal, "streaming_store", None)
    if isinstance(streaming_store, StreamingEventStore):
        streaming_store.flush()
        _update_result_digest_value(
            digest,
            "event_stream",
            {
                "count": streaming_store.count,
                "logical_bytes": streaming_store.logical_bytes_written,
                "semantic_sha256": streaming_store.semantic_sha256,
            },
        )
    else:
        _update_result_digest_sequence(digest, "events", result.journal.events)

    for name in (
        "orders",
        "fills",
        "trades",
        "equity",
        "setups",
        "setup_records",
        "features",
        "signals",
        "skips",
        "diagnostics",
        "errors",
    ):
        values = cast(Sequence[object], getattr(result.journal, name))
        _update_result_digest_sequence(digest, name, values)
    return digest.hexdigest()


# Functions defined in the implementation resolve helpers through their module
# globals. Install the versioned event-store contract before exposing entrypoints.
_impl._build_runtime_snapshot = _build_runtime_snapshot_proxy
_impl._restore_streaming_event_store = _restore_streaming_event_store
_impl._reduce_streaming_event_store = _reduce_streaming_event_store
_impl._result_digest = _result_digest
_impl._RuntimePickler.dispatch_table[StreamingEventStore] = _reduce_streaming_event_store

_RuntimePickler = _impl._RuntimePickler
run_with_runtime_snapshot_restart = _impl.run_with_runtime_snapshot_restart
run_with_replay_restart = _impl.run_with_replay_restart


def __getattr__(name: str) -> object:
    """Keep compatibility for private recovery helpers not part of the public API."""

    return getattr(_impl, name)
