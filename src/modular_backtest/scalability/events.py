from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import shutil
from collections import Counter, deque
from collections.abc import Iterator, Sequence
from pathlib import Path
from threading import RLock
from typing import BinaryIO

from modular_core.events import EventEnvelope
from modular_core.serialization import to_jsonable


_EVENT_STREAM_SEMANTIC_DIGEST_V1 = b"modular-event-stream-semantic-v1"
_GZIP_COMPRESSLEVEL = 1


def _initial_semantic_digest() -> bytes:
    return hashlib.sha256(_EVENT_STREAM_SEMANTIC_DIGEST_V1).digest()


def _advance_semantic_digest(previous: bytes, payload: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(previous)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
    return digest.digest()


def _iter_stored_records(path: Path, compression: str | None) -> Iterator[bytes]:
    if compression == "gzip":
        with gzip.open(path, "rb") as handle:
            yield from handle
        return
    with path.open("rb") as handle:
        yield from handle


class _CountingBinaryWriter:
    """Unbuffered binary writer that tracks bytes without a stat syscall per append."""

    def __init__(self, raw: BinaryIO, *, initial_bytes: int) -> None:
        self._raw = raw
        self.bytes_written = initial_bytes

    def write(self, payload: bytes) -> int:
        view = memoryview(payload)
        total = 0
        while view:
            written = self._raw.write(view)
            if written is None or written < 1:
                raise OSError("failed to append compressed event stream")
            total += written
            self.bytes_written += written
            view = view[written:]
        return total

    def flush(self) -> None:
        self._raw.flush()

    def close(self) -> None:
        self._raw.close()

    def fileno(self) -> int:
        return self._raw.fileno()

    def tell(self) -> int:
        return self._raw.tell()

    def writable(self) -> bool:
        return self._raw.writable()

    @property
    def closed(self) -> bool:
        return self._raw.closed


class StreamingEventStore:
    """Append-only event sink with a bounded in-memory tail.

    The default representation remains plain canonical JSONL. ``compression="gzip"``
    stores the same canonical JSONL records losslessly in deterministic gzip members.
    An explicit ``flush`` finishes and fsyncs the current member before snapshot
    hashing; the next append lazily opens a new member. This keeps snapshot bytes
    immutable while preserving append-after-restore semantics without per-event fsync.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        retained_window: int = 2_000,
        compression: str | None = None,
    ) -> None:
        if not isinstance(retained_window, int) or isinstance(retained_window, bool):
            raise TypeError("retained_window must be int")
        if retained_window < 1:
            raise ValueError("retained_window must be positive")
        if compression not in {None, "gzip"}:
            raise ValueError("compression must be null or gzip")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
        self._descriptor = os.open(self.path, flags, 0o600)
        self._compression = compression
        self._gzip_raw: _CountingBinaryWriter | None = None
        self._gzip: gzip.GzipFile | None = None
        self._retained: deque[EventEnvelope] = deque(maxlen=retained_window)
        self._count = 0
        self._last_sequence = 0
        self._run_id: str | None = None
        self._counts_by_type: Counter[str] = Counter()
        self._bytes_written = 0
        self._logical_bytes_written = 0
        self._semantic_digest = _initial_semantic_digest()
        self._closed = False
        self._poisoned = False
        self._lock = RLock()

    @property
    def count(self) -> int:
        return self._count

    @property
    def retained_count(self) -> int:
        return len(self._retained)

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def bytes_written(self) -> int:
        """Physical bytes currently occupied by the durable spool."""

        return self._bytes_written

    @property
    def logical_bytes_written(self) -> int:
        """Canonical uncompressed JSONL bytes represented by the spool."""

        self._ensure_runtime_fields()
        return self._logical_bytes_written

    @property
    def semantic_sha256(self) -> str:
        """Restart-boundary-independent digest of canonical event records."""

        self._ensure_runtime_fields()
        return self._semantic_digest.hex()

    @property
    def compression(self) -> str | None:
        self._ensure_runtime_fields()
        return self._compression

    @property
    def encoding(self) -> str:
        return "gzip" if self.compression == "gzip" else "identity"

    @property
    def artifact_name(self) -> str:
        return "events.jsonl.gz" if self.compression == "gzip" else "events.jsonl"

    @property
    def counts_by_type(self) -> dict[str, int]:
        return dict(sorted(self._counts_by_type.items()))

    def enable_compression(self, compression: str) -> None:
        """Enable gzip before the first event without changing legacy store defaults."""

        if compression != "gzip":
            raise ValueError("compression must be gzip")
        with self._lock:
            self._require_open()
            if self._compression == compression:
                return
            if self._compression is not None:
                raise ValueError("event stream compression is already configured")
            if self._count or self._logical_bytes_written or self._bytes_written:
                raise ValueError("event stream compression must be selected before the first event")
            self._compression = compression

    def append(self, event: EventEnvelope) -> None:
        self.append_many((event,))

    def append_many(self, events: Sequence[EventEnvelope]) -> None:
        batch = tuple(events)
        if any(not isinstance(event, EventEnvelope) for event in batch):
            raise TypeError("events must contain EventEnvelope")
        if not batch:
            return
        with self._lock:
            self._require_open()
            expected_sequence = self._last_sequence
            run_id = self._run_id
            rendered: list[bytes] = []
            for event in batch:
                expected_sequence += 1
                if event.sequence != expected_sequence:
                    raise ValueError(
                        f"event sequence must be contiguous: expected {expected_sequence}, "
                        f"got {event.sequence}"
                    )
                expected_id = f"evt-{event.sequence:010d}"
                if event.event_id != expected_id:
                    raise ValueError(
                        f"event_id must match deterministic sequence: expected {expected_id}"
                    )
                if run_id is not None and event.run_id != run_id:
                    raise ValueError("event store cannot mix run_id values")
                run_id = event.run_id
                rendered.append(
                    json.dumps(
                        to_jsonable(event),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
            payload = b"".join(rendered)
            original_size = self._bytes_written
            try:
                self._write_payload(payload)
            except Exception:
                try:
                    os.ftruncate(self._descriptor, original_size)
                finally:
                    # A compressor cannot be rewound safely after a partial write.
                    # Fail closed rather than allowing a second append to diverge.
                    self._poisoned = True
                raise
            self._logical_bytes_written += len(payload)
            for record in rendered:
                self._semantic_digest = _advance_semantic_digest(self._semantic_digest, record)
            for event in batch:
                self._retained.append(event)
                self._counts_by_type[event.event_type] += 1
                self._count += 1
                self._last_sequence = event.sequence
                self._run_id = event.run_id

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool):
            raise TypeError("after_sequence must be int")
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        with self._lock:
            return tuple(event for event in self._retained if event.sequence > after_sequence)

    def flush(self) -> None:
        """Durably finish the current gzip member so snapshot bytes cannot change later."""

        with self._lock:
            if self._closed:
                return
            self._require_open()
            self._finish_gzip_member()
            os.fsync(self._descriptor)
            self._bytes_written = os.fstat(self._descriptor).st_size

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._ensure_runtime_fields()
                self._finish_gzip_member()
                os.fsync(self._descriptor)
                self._bytes_written = os.fstat(self._descriptor).st_size
            finally:
                os.close(self._descriptor)
                self._closed = True

    def copy_to(self, destination: Path) -> None:
        with self._lock:
            self._require_publishable()
            self.flush()
            shutil.copyfile(self.path, destination)
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())

    def finalize_to(self, destination: Path) -> None:
        """Finalize the stream into ``destination`` without duplicating its data blocks."""

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._require_publishable()
            self.close()
            try:
                os.link(self.path, target)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise OSError(
                        errno.EXDEV,
                        "event stream finalization requires source and destination on the same filesystem",
                        str(target),
                    ) from exc
                raise
            with target.open("rb") as handle:
                os.fsync(handle.fileno())

    def cleanup(self) -> None:
        self.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.path.parent.rmdir()
        except OSError:
            pass

    @classmethod
    def restore_existing(
        cls,
        path: Path,
        *,
        retained_window: int,
        retained: tuple[EventEnvelope, ...],
        count: int,
        last_sequence: int,
        run_id: str | None,
        counts_by_type: dict[str, int],
        bytes_written: int,
        logical_bytes_written: int,
        semantic_sha256: str,
        compression: str | None,
    ) -> StreamingEventStore:
        if compression not in {None, "gzip"}:
            raise ValueError("compression must be null or gzip")
        if not path.is_file() or path.stat().st_size != bytes_written:
            raise ValueError("event stream changed before runtime restore")
        if len(semantic_sha256) != 64:
            raise ValueError("event stream semantic digest is invalid")
        store = cls.__new__(cls)
        store.path = path
        store._descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
        store._compression = compression
        store._gzip_raw = None
        store._gzip = None
        store._retained = deque(retained, maxlen=retained_window)
        store._count = count
        store._last_sequence = last_sequence
        store._run_id = run_id
        store._counts_by_type = Counter(counts_by_type)
        store._bytes_written = bytes_written
        store._logical_bytes_written = logical_bytes_written
        store._semantic_digest = bytes.fromhex(semantic_sha256)
        store._closed = False
        store._poisoned = False
        store._lock = RLock()
        return store

    def _write_payload(self, payload: bytes) -> None:
        if self._compression == "gzip":
            if self._gzip is None:
                self._open_gzip_member()
            gzip_writer = self._gzip
            gzip_raw = self._gzip_raw
            if gzip_writer is None or gzip_raw is None:
                raise RuntimeError("gzip event writer was not initialized")
            gzip_writer.write(payload)
            self._bytes_written = gzip_raw.bytes_written
            return
        view = memoryview(payload)
        while view:
            written = os.write(self._descriptor, view)
            if written < 1:
                raise OSError("failed to append event stream")
            view = view[written:]
        self._bytes_written += len(payload)

    def _open_gzip_member(self) -> None:
        raw = os.fdopen(os.dup(self._descriptor), "ab", buffering=0)
        counting_raw = _CountingBinaryWriter(raw, initial_bytes=self._bytes_written)
        self._gzip_raw = counting_raw
        self._gzip = gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=_GZIP_COMPRESSLEVEL,
            fileobj=counting_raw,
            mtime=0,
        )
        self._bytes_written = counting_raw.bytes_written

    def _finish_gzip_member(self) -> None:
        gzip_writer = getattr(self, "_gzip", None)
        gzip_raw = getattr(self, "_gzip_raw", None)
        try:
            if gzip_writer is not None:
                gzip_writer.close()
                self._gzip = None
        finally:
            if gzip_raw is not None:
                self._bytes_written = gzip_raw.bytes_written
                gzip_raw.close()
                self._gzip_raw = None

    def _ensure_runtime_fields(self) -> None:
        """Recover fields omitted by the v1 runtime-snapshot pickle reducer."""

        if hasattr(self, "_compression"):
            return
        self._gzip_raw = None
        self._gzip = None
        self._poisoned = False
        with self.path.open("rb") as probe:
            magic = probe.read(2)
        self._compression = "gzip" if magic == b"\x1f\x8b" else None
        semantic = _initial_semantic_digest()
        logical_bytes = 0
        try:
            for record in _iter_stored_records(self.path, self._compression):
                logical_bytes += len(record)
                semantic = _advance_semantic_digest(semantic, record)
        except (OSError, EOFError) as exc:
            raise ValueError("restored event stream is not readable") from exc
        self._logical_bytes_written = logical_bytes
        self._semantic_digest = semantic

    def _require_publishable(self) -> None:
        self._ensure_runtime_fields()
        if self._poisoned:
            raise OSError("event store is unusable after a failed append")

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("event store is closed")
        self._require_publishable()
