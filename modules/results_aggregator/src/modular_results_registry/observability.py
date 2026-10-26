from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from .canonical import canonical_sha256
from .errors import ValidationError
from .safe_io import open_append_text_nofollow

_SENSITIVE_KEYS = frozenset({"api_key", "apikey", "authorization", "password", "secret", "token"})


@dataclass(frozen=True, slots=True)
class StructuredLogEvent:
    timestamp_utc: str
    level: str
    operation: str
    status: str
    reason_code: str
    correlation_id: str
    duration_seconds: float = 0.0
    row_count: int = 0
    byte_count: int = 0
    entity_ids: Mapping[str, str] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)
    event_hash: str = ""

    def __post_init__(self) -> None:
        try:
            parsed = datetime.fromisoformat(self.timestamp_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("log timestamp must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationError("log timestamp must be timezone-aware")
        if not all(
            (self.level, self.operation, self.status, self.reason_code, self.correlation_id)
        ):
            raise ValidationError("structured log identity fields are required")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValidationError("log duration must be finite and non-negative")
        if self.row_count < 0 or self.byte_count < 0:
            raise ValidationError("log counts cannot be negative")
        clean_entities = {str(key): str(value) for key, value in self.entity_ids.items() if value}
        clean_details = _redact_mapping(self.details)
        object.__setattr__(self, "entity_ids", clean_entities)
        object.__setattr__(self, "details", clean_details)
        payload = {key: value for key, value in asdict(self).items() if key != "event_hash"}
        expected = canonical_sha256(payload)
        if self.event_hash and self.event_hash != expected:
            raise ValidationError("structured log event hash mismatch")
        object.__setattr__(self, "event_hash", expected)


class JsonLineLogger:
    """Thread-safe append-only JSONL logger with optional fsync."""

    def __init__(self, stream: TextIO, *, fsync: bool = False) -> None:
        self._stream = stream
        self._fsync = fsync
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str | Path, *, fsync: bool = False) -> "JsonLineLogger":
        stream = open_append_text_nofollow(path)
        return cls(stream, fsync=fsync)

    def emit(self, event: StructuredLogEvent) -> None:
        line = json.dumps(asdict(event), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if "\n" in line or "\r" in line:
            raise ValidationError("structured log line contains a newline")
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
            if self._fsync:
                os.fsync(self._stream.fileno())

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "JsonLineLogger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def build_log_event(
    *,
    level: str,
    operation: str,
    status: str,
    reason_code: str,
    correlation_id: str,
    duration_seconds: float = 0.0,
    row_count: int = 0,
    byte_count: int = 0,
    entity_ids: Mapping[str, str] | None = None,
    details: Mapping[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> StructuredLogEvent:
    now = timestamp or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValidationError("log timestamp must be timezone-aware")
    timestamp_utc = (
        now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    return StructuredLogEvent(
        timestamp_utc=timestamp_utc,
        level=level,
        operation=operation,
        status=status,
        reason_code=reason_code,
        correlation_id=correlation_id,
        duration_seconds=duration_seconds,
        row_count=row_count,
        byte_count=byte_count,
        entity_ids=entity_ids or {},
        details=details or {},
    )


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name.lower() in _SENSITIVE_KEYS:
            result[name] = "[REDACTED]"
        elif isinstance(item, Mapping):
            result[name] = _redact_mapping(item)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = item
        else:
            result[name] = repr(item)
    return result
