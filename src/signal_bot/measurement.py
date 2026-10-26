from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .domain import SignalStatus, TradeSignal, signal_to_dict

_PENDING_STATUSES = (
    SignalStatus.DETECTED,
    SignalStatus.VALIDATED,
    SignalStatus.RENDERING,
    SignalStatus.READY,
    SignalStatus.SENDING,
    SignalStatus.SEND_FAILED,
)
_TERMINAL_STATUSES = (
    SignalStatus.SENT,
    SignalStatus.CANCELLED,
    SignalStatus.EXPIRED,
)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    samples: int
    minimum_ns: int
    p50_ns: int
    p95_ns: int
    p99_ns: int
    maximum_ns: int

    @classmethod
    def from_samples(cls, samples_ns: Sequence[int]) -> LatencySummary:
        if not samples_ns:
            raise ValueError("at least one latency sample is required")
        ordered = sorted(samples_ns)
        if ordered[0] < 0:
            raise ValueError("latency samples must be non-negative")
        return cls(
            samples=len(ordered),
            minimum_ns=ordered[0],
            p50_ns=_nearest_rank(ordered, 0.50),
            p95_ns=_nearest_rank(ordered, 0.95),
            p99_ns=_nearest_rank(ordered, 0.99),
            maximum_ns=ordered[-1],
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "samples": self.samples,
            "minimum_ns": self.minimum_ns,
            "p50_ns": self.p50_ns,
            "p95_ns": self.p95_ns,
            "p99_ns": self.p99_ns,
            "maximum_ns": self.maximum_ns,
        }


@dataclass(frozen=True, slots=True)
class SQLiteStateSnapshot:
    captured_at: datetime
    total_signals: int
    pending_signals: int
    terminal_signals: int
    retry_scheduled: int
    cursor_count: int
    status_counts: Mapping[str, int]
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    oldest_pending_age_seconds: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_counts", MappingProxyType(dict(self.status_counts)))

    @property
    def persisted_bytes(self) -> int:
        return self.database_bytes + self.wal_bytes + self.shm_bytes

    def to_mapping(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at.isoformat(),
            "total_signals": self.total_signals,
            "pending_signals": self.pending_signals,
            "terminal_signals": self.terminal_signals,
            "retry_scheduled": self.retry_scheduled,
            "cursor_count": self.cursor_count,
            "status_counts": dict(self.status_counts),
            "database_bytes": self.database_bytes,
            "wal_bytes": self.wal_bytes,
            "shm_bytes": self.shm_bytes,
            "persisted_bytes": self.persisted_bytes,
            "oldest_pending_age_seconds": self.oldest_pending_age_seconds,
        }


def canonical_signal_payload(signal: TradeSignal) -> bytes:
    """Return deterministic UTF-8 JSON for measurement and digesting.

    This function is deliberately strict. Unsupported metadata values fail
    instead of being converted with ``default=str`` because that conversion can
    hide schema drift and produce environment-dependent representations.
    """

    return json.dumps(
        signal_to_dict(signal),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def snapshot_sqlite_state(
    path: str | Path,
    *,
    captured_at: datetime | None = None,
) -> SQLiteStateSnapshot:
    """Read a payload-free snapshot of the persistent Signal Bot state.

    Only aggregate counts and file sizes are returned. Signal payloads, chat
    identifiers, message identifiers and strategy metadata are never selected.
    """

    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    now = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        status_counts = {
            str(status): int(count)
            for status, count in connection.execute(
                "SELECT status, COUNT(*) FROM signals GROUP BY status ORDER BY status"
            )
        }
        total_signals = sum(status_counts.values())
        pending_signals = sum(status_counts.get(status.value, 0) for status in _PENDING_STATUSES)
        terminal_signals = sum(status_counts.get(status.value, 0) for status in _TERMINAL_STATUSES)
        retry_scheduled = int(
            connection.execute(
                "SELECT COUNT(*) FROM signals WHERE retry_at IS NOT NULL"
            ).fetchone()[0]
        )
        cursor_count = int(connection.execute("SELECT COUNT(*) FROM cursors").fetchone()[0])
        oldest_row = connection.execute(
            "SELECT MIN(created_at) FROM signals WHERE status IN (?,?,?,?,?,?)",
            tuple(status.value for status in _PENDING_STATUSES),
        ).fetchone()
    finally:
        connection.close()

    oldest_pending_age_seconds: float | None = None
    if oldest_row and oldest_row[0] is not None:
        created_at = datetime.fromisoformat(str(oldest_row[0])).astimezone(timezone.utc)
        oldest_pending_age_seconds = max(0.0, (now - created_at).total_seconds())

    wal_path = Path(f"{database}-wal")
    shm_path = Path(f"{database}-shm")
    return SQLiteStateSnapshot(
        captured_at=now,
        total_signals=total_signals,
        pending_signals=pending_signals,
        terminal_signals=terminal_signals,
        retry_scheduled=retry_scheduled,
        cursor_count=cursor_count,
        status_counts=status_counts,
        database_bytes=database.stat().st_size,
        wal_bytes=wal_path.stat().st_size if wal_path.exists() else 0,
        shm_bytes=shm_path.stat().st_size if shm_path.exists() else 0,
        oldest_pending_age_seconds=oldest_pending_age_seconds,
    )


def _nearest_rank(ordered: Sequence[int], quantile: float) -> int:
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]
