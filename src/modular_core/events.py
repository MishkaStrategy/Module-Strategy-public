from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import stat
from threading import RLock
from typing import Any, Protocol

from .serialization import deep_freeze, strict_json_loads, to_jsonable
from .validation import require_nonblank, utc_datetime


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    run_id: str
    sequence: int
    timestamp: datetime
    event_type: str
    source: str
    payload: Mapping[str, Any]
    schema_version: str = "1"
    correlation_id: str | None = None
    causation_id: str | None = None
    symbol: str | None = None
    entity_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp",
            utc_datetime(self.timestamp, "event timestamp"),
        )
        require_nonblank(self.event_id, "event_id")
        require_nonblank(self.run_id, "run_id")
        require_nonblank(self.event_type, "event_type")
        require_nonblank(self.source, "event source")
        require_nonblank(self.schema_version, "event schema_version")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise TypeError("event sequence must be int")
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        for name in ("correlation_id", "causation_id", "symbol", "entity_key"):
            value = getattr(self, name)
            if value is not None:
                require_nonblank(value, name)
        if self.causation_id == self.event_id:
            raise ValueError("event cannot cause itself")
        if not isinstance(self.payload, Mapping):
            raise TypeError("event payload must be a mapping")
        object.__setattr__(self, "payload", deep_freeze(self.payload))


class EventStore(Protocol):
    def append(self, event: EventEnvelope) -> None: ...

    def append_many(self, events: Sequence[EventEnvelope]) -> None: ...

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._sequences: list[int] = []
        self._ids: set[str] = set()
        self._run_id: str | None = None
        self._lock = RLock()

    def validate_append(self, event: EventEnvelope) -> None:
        if not isinstance(event, EventEnvelope):
            raise TypeError("event must be EventEnvelope")
        with self._lock:
            self._validate_append_unlocked(event)

    def append(self, event: EventEnvelope) -> None:
        self.append_many((event,))

    def validate_append_many(self, events: Sequence[EventEnvelope]) -> tuple[EventEnvelope, ...]:
        batch = _event_batch(events)
        with self._lock:
            self._validate_batch_unlocked(batch)
        return batch

    def append_many(self, events: Sequence[EventEnvelope]) -> None:
        batch = _event_batch(events)
        if not batch:
            return
        with self._lock:
            self._validate_batch_unlocked(batch)
            for event in batch:
                self._append_unlocked(event)

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        _validate_after_sequence(after_sequence)
        with self._lock:
            index = bisect_right(self._sequences, after_sequence)
            return tuple(self._events[index:])

    def __iter__(self) -> Iterator[EventEnvelope]:
        return iter(self.read())

    def _validate_append_unlocked(self, event: EventEnvelope) -> None:
        self._validate_batch_unlocked((event,))

    def _validate_batch_unlocked(self, events: tuple[EventEnvelope, ...]) -> None:
        batch_ids: set[str] = set()
        run_id = self._run_id
        last_sequence = self._events[-1].sequence if self._events else None
        for event in events:
            if event.event_id in self._ids or event.event_id in batch_ids:
                raise ValueError(f"duplicate event_id: {event.event_id}")
            if run_id is not None and event.run_id != run_id:
                raise ValueError(f"mixed run_id: expected {run_id}, got {event.run_id}")
            if last_sequence is not None and event.sequence <= last_sequence:
                raise ValueError("event sequence must be strictly increasing")
            batch_ids.add(event.event_id)
            run_id = event.run_id
            last_sequence = event.sequence

    def _append_unlocked(self, event: EventEnvelope) -> None:
        self._events.append(event)
        self._sequences.append(event.sequence)
        self._ids.add(event.event_id)
        self._run_id = event.run_id


class _EventLineSyntaxError(ValueError):
    pass


class JsonLinesEventStore:
    """Bounded append-only event store with explicit crash-tail recovery."""

    def __init__(
        self,
        path: str | Path,
        *,
        fsync: bool = False,
        max_line_bytes: int | None = 4 * 1024 * 1024,
        max_log_bytes: int | None = 256 * 1024 * 1024,
        recover_truncated_tail: bool = False,
    ) -> None:
        _require_bool(fsync, "fsync")
        _require_bool(recover_truncated_tail, "recover_truncated_tail")
        _validate_optional_positive_int(max_line_bytes, "max_line_bytes")
        _validate_optional_positive_int(max_log_bytes, "max_log_bytes")
        self.max_line_bytes = max_line_bytes
        self.max_log_bytes = max_log_bytes
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync
        self.recover_truncated_tail = recover_truncated_tail
        self.recovered_truncated_tail = False
        self._memory = InMemoryEventStore()
        self._lock = RLock()
        if self.path.exists() or self.path.is_symlink():
            self._load_existing()

    def append(self, event: EventEnvelope) -> None:
        self.append_many((event,))

    def append_many(self, events: Sequence[EventEnvelope]) -> None:
        batch = _event_batch(events)
        if not batch:
            return
        lines: list[bytes] = []
        for event in batch:
            payload = json.dumps(
                to_jsonable(event),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if self.max_line_bytes is not None and len(payload) > self.max_line_bytes:
                raise ValueError("event exceeds max_line_bytes")
            lines.append(payload + b"\n")
        encoded = b"".join(lines)
        with self._lock:
            self._memory.validate_append_many(batch)
            self._append_bytes(encoded)
            self._memory.append_many(batch)

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        _validate_after_sequence(after_sequence)
        with self._lock:
            return self._memory.read(after_sequence=after_sequence)

    def __iter__(self) -> Iterator[EventEnvelope]:
        return iter(self.read())

    def _load_existing(self) -> None:
        flags = os.O_RDWR if self.recover_truncated_tail else os.O_RDONLY
        descriptor, metadata = _open_regular_file(self.path, flags)
        try:
            if self.max_log_bytes is not None and metadata.st_size > self.max_log_bytes:
                raise ValueError("event log exceeds max_log_bytes")
            mode = "r+b" if self.recover_truncated_tail else "rb"
            with os.fdopen(descriptor, mode, closefd=True) as handle:
                descriptor = -1
                line_number = 0
                while True:
                    line_start = handle.tell()
                    read_limit = -1 if self.max_line_bytes is None else self.max_line_bytes + 2
                    raw_line = handle.readline(read_limit)
                    if not raw_line:
                        break
                    line_number += 1
                    line = raw_line.rstrip(b"\r\n")
                    if self.max_line_bytes is not None and len(line) > self.max_line_bytes:
                        raise ValueError(
                            f"invalid event log at line {line_number}: line exceeds size limit"
                        )
                    if not line.strip():
                        continue
                    try:
                        event = _decode_event_line(line, line_number)
                    except _EventLineSyntaxError:
                        if self.recover_truncated_tail and not raw_line.endswith(b"\n"):
                            handle.seek(line_start)
                            handle.truncate()
                            handle.flush()
                            if self.fsync:
                                os.fsync(handle.fileno())
                            self.recovered_truncated_tail = True
                            break
                        raise
                    self._memory.append(event)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _append_bytes(self, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        descriptor, metadata = _open_regular_file(self.path, flags, mode=0o600)
        try:
            if (
                self.max_log_bytes is not None
                and metadata.st_size + len(payload) > self.max_log_bytes
            ):
                raise ValueError("event log exceeds max_log_bytes")
            original_size = metadata.st_size
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("failed to append event log")
                    view = view[written:]
            except Exception:
                os.ftruncate(descriptor, original_size)
                raise
            if self.fsync:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _event_batch(events: Sequence[EventEnvelope]) -> tuple[EventEnvelope, ...]:
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise TypeError("events must be a sequence")
    batch = tuple(events)
    if any(not isinstance(event, EventEnvelope) for event in batch):
        raise TypeError("events must contain EventEnvelope")
    return batch


def _decode_event_line(line: bytes, line_number: int) -> EventEnvelope:
    try:
        row = strict_json_loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _EventLineSyntaxError(f"invalid event log at line {line_number}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"invalid event log at line {line_number}: {exc}") from exc
    try:
        if not isinstance(row, Mapping):
            raise TypeError("event row must be a mapping")
        timestamp = row["timestamp"]
        if not isinstance(timestamp, str):
            raise TypeError("event timestamp must be an ISO string")
        return EventEnvelope(
            event_id=row["event_id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            timestamp=datetime.fromisoformat(timestamp),
            event_type=row["event_type"],
            source=row["source"],
            payload=row.get("payload", {}),
            schema_version=row.get("schema_version", "1"),
            correlation_id=row.get("correlation_id"),
            causation_id=row.get("causation_id"),
            symbol=row.get("symbol"),
            entity_key=row.get("entity_key"),
        )
    except Exception as exc:
        raise ValueError(f"invalid event log at line {line_number}: {exc}") from exc


def _open_regular_file(
    path: Path,
    flags: int,
    *,
    mode: int = 0o600,
) -> tuple[int, os.stat_result]:
    path_metadata: os.stat_result | None = None
    try:
        path_metadata = os.lstat(path)
        if not stat.S_ISREG(path_metadata.st_mode):
            raise ValueError("event log must be a regular file and cannot be a symlink")
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
    safe_flags = flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, safe_flags, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("event log cannot be a symlink") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("event log must be a regular file")
        if path_metadata is not None and (
            metadata.st_dev,
            metadata.st_ino,
        ) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise ValueError("event log changed while opening")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _validate_after_sequence(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("after_sequence must be int")
    if value < 0:
        raise ValueError("after_sequence cannot be negative")


def _validate_optional_positive_int(value: int | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or None")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
