from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .operator_contract import (
    MAX_REQUESTS,
    OPERATION,
    OPERATOR_PATTERN,
    REQUEST_ID_PATTERN,
    RequestRecord,
    RequestStatus,
    SignalBotAuditEntry,
    SignalBotControlError,
    SignalBotControlErrorCode,
    TestNotificationRequest,
)

_AUDIT_RESULTS = frozenset(
    {
        "ACCEPTED",
        "SUCCEEDED",
        "REPLAYED",
        *(code.value for code in SignalBotControlErrorCode),
    }
)


class SignalBotControlLedger:
    """Signal Bot-owned durable idempotency/audit ledger in the owner SQLite DB."""

    def __init__(self, path: str | Path, *, max_requests: int = MAX_REQUESTS) -> None:
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        self.max_requests = max_requests
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_bot_operator_requests(
                  request_id TEXT PRIMARY KEY,
                  operator TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  status TEXT NOT NULL,
                  error_code TEXT,
                  created_at TEXT NOT NULL,
                  completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS signal_bot_operator_audit(
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  request_id TEXT NOT NULL,
                  operator TEXT NOT NULL,
                  result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_bot_operator_status(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  reachable INTEGER,
                  last_success_at TEXT,
                  last_error_type TEXT
                );
                INSERT OR IGNORE INTO signal_bot_operator_status(
                  singleton, reachable, last_success_at, last_error_type
                ) VALUES(1, NULL, NULL, NULL);
                """
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

    def claim(self, request: TestNotificationRequest, now: datetime) -> RequestRecord | None:
        timestamp = _utc(now)
        pending_error: SignalBotControlError | None = None
        replay: RequestRecord | None = None
        with self._lock, self._write_transaction():
            row = self._db.execute(
                "SELECT operator,operation,status,error_code,created_at,completed_at "
                "FROM signal_bot_operator_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                count = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM signal_bot_operator_requests"
                    ).fetchone()[0]
                )
                if count >= self.max_requests:
                    self._append_audit(
                        timestamp,
                        request,
                        SignalBotControlErrorCode.OWNER_FAILURE.value,
                    )
                    pending_error = SignalBotControlError(
                        SignalBotControlErrorCode.OWNER_FAILURE,
                        "test-notification idempotency capacity is exhausted",
                        request_id=request.request_id,
                    )
                else:
                    self._db.execute(
                        "INSERT INTO signal_bot_operator_requests VALUES(?,?,?,?,?,?,?)",
                        (
                            request.request_id,
                            request.operator.subject,
                            OPERATION,
                            RequestStatus.IN_PROGRESS.value,
                            None,
                            timestamp.isoformat(),
                            None,
                        ),
                    )
                    self._append_audit(timestamp, request, "ACCEPTED")
            else:
                operator, operation, *_ = row
                if operator != request.operator.subject or operation != OPERATION:
                    self._append_audit(
                        timestamp,
                        request,
                        SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT.value,
                    )
                    pending_error = SignalBotControlError(
                        SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT,
                        "request_id was already used for a different operator operation",
                        request_id=request.request_id,
                    )
                else:
                    replay = _decode_record(request.request_id, row)
                    if replay.status is RequestStatus.IN_PROGRESS:
                        self._append_audit(
                            timestamp,
                            request,
                            SignalBotControlErrorCode.RECOVERY_REQUIRED.value,
                        )
                        pending_error = SignalBotControlError(
                            SignalBotControlErrorCode.RECOVERY_REQUIRED,
                            "prior test-notification outcome is uncertain",
                            request_id=request.request_id,
                        )
                    else:
                        result = (
                            "REPLAYED"
                            if replay.status is RequestStatus.SUCCEEDED
                            else replay.error_code.value
                            if replay.error_code is not None
                            else SignalBotControlErrorCode.OWNER_FAILURE.value
                        )
                        self._append_audit(timestamp, request, result)
        if pending_error is not None:
            raise pending_error from None
        return replay

    def complete_success(self, request: TestNotificationRequest, now: datetime) -> datetime:
        completed_at = _utc(now)
        with self._lock, self._write_transaction():
            cursor = self._db.execute(
                "UPDATE signal_bot_operator_requests SET status=?,error_code=NULL,completed_at=? "
                "WHERE request_id=? AND operator=? AND operation=? AND status=?",
                (
                    RequestStatus.SUCCEEDED.value,
                    completed_at.isoformat(),
                    request.request_id,
                    request.operator.subject,
                    OPERATION,
                    RequestStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise SignalBotControlError(
                    SignalBotControlErrorCode.RECOVERY_REQUIRED,
                    "test-notification result cannot be persisted safely",
                    request_id=request.request_id,
                )
            self._db.execute(
                "UPDATE signal_bot_operator_status SET reachable=1,last_success_at=?,last_error_type=NULL "
                "WHERE singleton=1",
                (completed_at.isoformat(),),
            )
            self._append_audit(completed_at, request, "SUCCEEDED")
        return completed_at

    def complete_failure(
        self,
        request: TestNotificationRequest,
        code: SignalBotControlErrorCode,
        now: datetime,
    ) -> datetime:
        if code in {
            SignalBotControlErrorCode.INVALID_REQUEST,
            SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT,
            SignalBotControlErrorCode.RECOVERY_REQUIRED,
        }:
            raise ValueError("failure code cannot be persisted as a terminal send result")
        completed_at = _utc(now)
        if code is SignalBotControlErrorCode.UNREACHABLE:
            reachable: int | None = 0
        elif code in {SignalBotControlErrorCode.RATE_LIMITED, SignalBotControlErrorCode.REJECTED}:
            reachable = 1
        else:
            reachable = None
        with self._lock, self._write_transaction():
            cursor = self._db.execute(
                "UPDATE signal_bot_operator_requests SET status=?,error_code=?,completed_at=? "
                "WHERE request_id=? AND operator=? AND operation=? AND status=?",
                (
                    RequestStatus.FAILED.value,
                    code.value,
                    completed_at.isoformat(),
                    request.request_id,
                    request.operator.subject,
                    OPERATION,
                    RequestStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise SignalBotControlError(
                    SignalBotControlErrorCode.RECOVERY_REQUIRED,
                    "test-notification failure cannot be persisted safely",
                    request_id=request.request_id,
                )
            if reachable is None:
                self._db.execute(
                    "UPDATE signal_bot_operator_status SET last_error_type=? WHERE singleton=1",
                    (code.value,),
                )
            else:
                self._db.execute(
                    "UPDATE signal_bot_operator_status SET reachable=?,last_error_type=? WHERE singleton=1",
                    (reachable, code.value),
                )
            self._append_audit(completed_at, request, code.value)
        return completed_at

    def owner_status(self) -> tuple[bool | None, datetime | None, str | None]:
        with self._lock:
            row = self._db.execute(
                "SELECT reachable,last_success_at,last_error_type "
                "FROM signal_bot_operator_status WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise SignalBotControlError(
                SignalBotControlErrorCode.OWNER_FAILURE,
                "Signal Bot control status is unavailable",
            )
        reachable_raw, success_raw, error_raw = row
        if reachable_raw not in {None, 0, 1}:
            raise SignalBotControlError(
                SignalBotControlErrorCode.RECOVERY_REQUIRED,
                "Signal Bot reachability state is invalid",
            )
        reachable = None if reachable_raw is None else bool(reachable_raw)
        success = _parse_timestamp(success_raw) if success_raw is not None else None
        error_type = str(error_raw) if error_raw is not None else None
        if error_type is not None and error_type not in {
            code.value for code in SignalBotControlErrorCode
        }:
            raise SignalBotControlError(
                SignalBotControlErrorCode.RECOVERY_REQUIRED,
                "Signal Bot error state is invalid",
            )
        return reachable, success, error_type

    def audit_entries(self, *, limit: int = 100) -> tuple[SignalBotAuditEntry, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id,timestamp,operation,request_id,operator,result "
                "FROM signal_bot_operator_audit ORDER BY event_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        entries: list[SignalBotAuditEntry] = []
        for event_id, timestamp, operation, request_id, operator, result in rows:
            try:
                event_number = int(event_id)
                timestamp_value = _parse_timestamp(timestamp)
                operation_value = str(operation)
                request_value = str(request_id)
                operator_value = str(operator)
                result_value = str(result)
                if event_number < 1 or operation_value != OPERATION:
                    raise ValueError("invalid audit identity")
                if REQUEST_ID_PATTERN.fullmatch(request_value) is None:
                    raise ValueError("invalid audit request")
                if OPERATOR_PATTERN.fullmatch(operator_value) is None:
                    raise ValueError("invalid audit operator")
                if result_value not in _AUDIT_RESULTS:
                    raise ValueError("invalid audit result")
            except (TypeError, ValueError):
                raise SignalBotControlError(
                    SignalBotControlErrorCode.RECOVERY_REQUIRED,
                    "persisted Signal Bot audit entry is invalid",
                ) from None
            entries.append(
                SignalBotAuditEntry(
                    event_id=event_number,
                    timestamp=timestamp_value,
                    operation=operation_value,
                    request_id=request_value,
                    operator=operator_value,
                    result=result_value,
                )
            )
        return tuple(entries)

    def _append_audit(
        self,
        timestamp: datetime,
        request: TestNotificationRequest,
        result: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO signal_bot_operator_audit(timestamp,operation,request_id,operator,result) "
            "VALUES(?,?,?,?,?)",
            (
                timestamp.isoformat(),
                OPERATION,
                request.request_id,
                request.operator.subject,
                result,
            ),
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _decode_record(request_id: str, row: tuple[object, ...]) -> RequestRecord:
    try:
        operator, operation, status_raw, error_raw, created_raw, completed_raw = row
        if operation != OPERATION:
            raise ValueError("unexpected operation")
        status = RequestStatus(str(status_raw))
        error = SignalBotControlErrorCode(str(error_raw)) if error_raw is not None else None
        created_at = _parse_timestamp(created_raw)
        completed_at = _parse_timestamp(completed_raw) if completed_raw is not None else None
        if status is RequestStatus.SUCCEEDED and completed_at is None:
            raise ValueError("missing completion")
        if status is RequestStatus.FAILED and (completed_at is None or error is None):
            raise ValueError("missing failure completion")
        if status is RequestStatus.IN_PROGRESS and completed_at is not None:
            raise ValueError("unexpected in-progress completion")
        operator_text = str(operator)
        if OPERATOR_PATTERN.fullmatch(operator_text) is None:
            raise ValueError("invalid operator")
    except (TypeError, ValueError):
        raise SignalBotControlError(
            SignalBotControlErrorCode.RECOVERY_REQUIRED,
            "persisted test-notification request is invalid",
            request_id=request_id,
        ) from None
    return RequestRecord(
        request_id=request_id,
        operator=operator_text,
        status=status,
        error_code=error,
        created_at=created_at,
        completed_at=completed_at,
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    return _utc(datetime.fromisoformat(value))
