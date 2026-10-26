from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import DecimalException
from pathlib import Path

from .domain import SignalStatus, TradeSignal, signal_from_dict, signal_to_dict

DEFAULT_MAX_TOTAL_SIGNALS = 100_000
DEFAULT_MAX_PENDING_SIGNALS = 10_000

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


class StateTransitionError(ValueError):
    pass


class StateCapacityError(RuntimeError):
    def __init__(self, resource: str, *, current: int, limit: int) -> None:
        self.resource = resource
        self.current = current
        self.limit = limit
        super().__init__(f"{resource} capacity exhausted: current={current}, limit={limit}")


_ALLOWED = {
    SignalStatus.DETECTED: {
        SignalStatus.VALIDATED,
        SignalStatus.CANCELLED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.VALIDATED: {
        SignalStatus.RENDERING,
        SignalStatus.CANCELLED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.RENDERING: {
        SignalStatus.READY,
        SignalStatus.SEND_FAILED,
        SignalStatus.CANCELLED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.READY: {
        SignalStatus.RENDERING,
        SignalStatus.SENDING,
        SignalStatus.CANCELLED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.SENDING: {
        SignalStatus.RENDERING,
        SignalStatus.SENT,
        SignalStatus.SEND_FAILED,
        SignalStatus.CANCELLED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.SEND_FAILED: {
        SignalStatus.RENDERING,
        SignalStatus.SENDING,
        SignalStatus.CANCELLED,
        SignalStatus.EXPIRED,
    },
}


class SQLiteStateStore:
    """Thread-safe persistent idempotency and lifecycle store.

    New semantic identities are admitted only while both persistent capacity
    limits have room. Existing identities remain idempotently suppressed even
    when a limit is exhausted, and terminal identities are never silently
    evicted because they are the durable duplicate-delivery evidence.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_total_signals: int = DEFAULT_MAX_TOTAL_SIGNALS,
        max_pending_signals: int = DEFAULT_MAX_PENDING_SIGNALS,
    ) -> None:
        self.max_total_signals = _positive_limit("max_total_signals", max_total_signals)
        self.max_pending_signals = _positive_limit("max_pending_signals", max_pending_signals)
        if self.max_pending_signals > self.max_total_signals:
            raise ValueError("max_pending_signals must not exceed max_total_signals")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        pending_values = tuple(status.value for status in _PENDING_STATUSES)
        with self._db:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version(version) VALUES (1);
            INSERT OR IGNORE INTO schema_version(version) VALUES (2);
            CREATE TABLE IF NOT EXISTS signals(
              signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
              status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, error TEXT, retry_at TEXT, message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS cursors(
              symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL,
              PRIMARY KEY(symbol, timeframe)
            );
            CREATE TABLE IF NOT EXISTS notification_preferences(
              chat_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_metadata(
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_counters(
              name TEXT PRIMARY KEY, value INTEGER NOT NULL CHECK(value >= 0)
            );
            """)
            total_signals = int(self._db.execute("SELECT COUNT(*) FROM signals").fetchone()[0])
            pending_signals = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE status IN (?,?,?,?,?,?)",
                    pending_values,
                ).fetchone()[0]
            )
            self._db.executemany(
                "INSERT INTO state_counters(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (
                    ("total_signals", total_signals),
                    ("pending_signals", pending_signals),
                ),
            )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()

    def _counter(self, name: str) -> int:
        row = self._db.execute(
            "SELECT value FROM state_counters WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"missing state counter: {name}")
        return int(row[0])

    def _increment_counter(self, name: str) -> None:
        cursor = self._db.execute(
            "UPDATE state_counters SET value=value+1 WHERE name=?",
            (name,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"missing state counter: {name}")

    def _decrement_counter(self, name: str, amount: int = 1) -> None:
        if amount < 1:
            return
        current = self._counter(name)
        if current < amount:
            raise RuntimeError(
                f"state counter underflow: {name}, current={current}, decrement={amount}"
            )
        self._db.execute(
            "UPDATE state_counters SET value=value-? WHERE name=?",
            (amount, name),
        )

    def register(self, signal: TradeSignal) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(signal_to_dict(signal), sort_keys=True, default=str)
        with self._lock, self._write_transaction():
            duplicate = self._db.execute(
                "SELECT 1 FROM signals WHERE signal_id=?",
                (signal.signal_id,),
            ).fetchone()
            if duplicate is not None:
                return False
            total_signals = self._counter("total_signals")
            if total_signals >= self.max_total_signals:
                raise StateCapacityError(
                    "total_signals",
                    current=total_signals,
                    limit=self.max_total_signals,
                )
            pending_signals = self._counter("pending_signals")
            if pending_signals >= self.max_pending_signals:
                raise StateCapacityError(
                    "pending_signals",
                    current=pending_signals,
                    limit=self.max_pending_signals,
                )
            self._db.execute(
                "INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    signal.signal_id,
                    signal.symbol,
                    signal.strategy_id,
                    SignalStatus.DETECTED.value,
                    payload,
                    now,
                    now,
                    None,
                    None,
                    None,
                ),
            )
            self._increment_counter("total_signals")
            self._increment_counter("pending_signals")
            return True

    def status(self, signal_id: str) -> SignalStatus | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return SignalStatus(row[0]) if row else None

    def pending(self, *, now: datetime, symbol: str | None = None) -> tuple[TradeSignal, ...]:
        statuses = tuple(status.value for status in _PENDING_STATUSES)
        query = "SELECT signal_id,payload FROM signals WHERE status IN (?,?,?,?,?,?) "
        parameters: list[object] = list(statuses)
        query += "AND (retry_at IS NULL OR retry_at<=?) "
        parameters.append(now.astimezone(timezone.utc).isoformat())
        if symbol is not None:
            query += "AND symbol=? "
            parameters.append(symbol)
        query += "ORDER BY created_at LIMIT 100"
        decoded: list[TradeSignal] = []
        invalid_ids: list[str] = []
        with self._lock, self._write_transaction():
            rows = self._db.execute(query, parameters).fetchall()
            for signal_id, payload in rows:
                try:
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError("persisted signal payload must be an object")
                    decoded.append(signal_from_dict(value))
                except (
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                    DecimalException,
                    OverflowError,
                ):
                    invalid_ids.append(str(signal_id))
            transitioned = 0
            if invalid_ids:
                updated_at = datetime.now(timezone.utc).isoformat()
                for signal_id in invalid_ids:
                    cursor = self._db.execute(
                        "UPDATE signals SET status=?,updated_at=?,error=?,retry_at=NULL "
                        "WHERE signal_id=? AND status IN (?,?,?,?,?,?)",
                        (
                            SignalStatus.CANCELLED.value,
                            updated_at,
                            "invalid_persisted_signal_payload",
                            signal_id,
                            *statuses,
                        ),
                    )
                    transitioned += cursor.rowcount
            self._decrement_counter("pending_signals", transitioned)
        return tuple(decoded)

    def transition(
        self,
        signal_id: str,
        status: SignalStatus,
        *,
        error: str | None = None,
        retry_at: datetime | None = None,
        message_id: str | None = None,
    ) -> None:
        with self._lock, self._write_transaction():
            row = self._db.execute(
                "SELECT status FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
            if row is None:
                raise StateTransitionError(f"unknown signal: {signal_id}")
            current = SignalStatus(row[0])
            if current == status:
                return
            if status not in _ALLOWED.get(current, set()):
                raise StateTransitionError(f"invalid transition {current.value} -> {status.value}")
            self._db.execute(
                "UPDATE signals SET status=?,updated_at=?,error=?,retry_at=?,"
                "message_id=COALESCE(?,message_id) WHERE signal_id=?",
                (
                    status.value,
                    datetime.now(timezone.utc).isoformat(),
                    error,
                    retry_at.astimezone(timezone.utc).isoformat() if retry_at else None,
                    message_id,
                    signal_id,
                ),
            )
            if current in _PENDING_STATUSES and status in _TERMINAL_STATUSES:
                self._decrement_counter("pending_signals")

    def last_processed(self, symbol: str, timeframe: str) -> datetime | None:
        with self._lock:
            row = self._db.execute(
                "SELECT timestamp FROM cursors WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_last_processed(self, symbol: str, timeframe: str, timestamp: datetime) -> None:
        if timestamp.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        normalized = timestamp.astimezone(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(symbol,timeframe) "
                "DO UPDATE SET timestamp=excluded.timestamp "
                "WHERE excluded.timestamp > cursors.timestamp",
                (symbol, timeframe, normalized),
            )

    def latest_signal(self, symbol: str | None = None) -> TradeSignal | None:
        query = "SELECT payload FROM signals"
        parameters: tuple[object, ...] = ()
        if symbol is not None:
            query += " WHERE symbol=?"
            parameters = (symbol,)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(query, parameters).fetchone()
        return signal_from_dict(json.loads(row[0])) if row else None

    def get_signal(self, signal_id: str) -> TradeSignal | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return signal_from_dict(json.loads(row[0])) if row else None

    def set_notifications(self, chat_id: str, enabled: bool) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO notification_preferences VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,"
                "updated_at=excluded.updated_at",
                (chat_id, int(enabled), datetime.now(timezone.utc).isoformat()),
            )

    def notifications_enabled(self, chat_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT enabled FROM notification_preferences WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return bool(row[0]) if row else True

    def telegram_update_offset(self) -> int | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM runtime_metadata WHERE key='telegram_update_offset'"
            ).fetchone()
        return int(row[0]) if row else None

    def set_telegram_update_offset(self, offset: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Telegram update offset must be a non-negative integer")
        with self._lock, self._db:
            current = self.telegram_update_offset()
            if current is not None and offset < current:
                return
            self._db.execute(
                "INSERT INTO runtime_metadata VALUES('telegram_update_offset',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (str(offset), datetime.now(timezone.utc).isoformat()),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _positive_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
