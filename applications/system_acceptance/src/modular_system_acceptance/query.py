from __future__ import annotations

from datetime import UTC, datetime

from .contracts import (
    AcceptanceAuditEntry,
    AcceptanceErrorCode,
    AcceptanceMutation,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceStatus,
)
from .storage import SQLiteAcceptanceStore


class SystemAcceptanceQuery:
    """Side-effect-free query over an already-owned Acceptance store instance."""

    def __init__(self, store: SQLiteAcceptanceStore) -> None:
        if not isinstance(store, SQLiteAcceptanceStore):
            raise TypeError("store must be SQLiteAcceptanceStore")
        self._store = store

    def audit_entries(self, *, limit: int = 100) -> tuple[AcceptanceAuditEntry, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "audit limit must be between 1 and 500",
            )
        try:
            with self._store._lock:  # noqa: SLF001 - same-owner query, never Control Plane direct DB
                rows = self._store._db.execute(  # noqa: SLF001
                    "SELECT event_id,timestamp,mutation,request_id,operator,run_id,result,"
                    "previous_status,new_status,phase FROM acceptance_audit "
                    "ORDER BY event_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance audit read failed",
            ) from None
        entries: list[AcceptanceAuditEntry] = []
        for row in rows:
            try:
                (
                    event_id,
                    timestamp,
                    mutation,
                    request_id,
                    operator,
                    run_id,
                    result,
                    previous_status,
                    new_status,
                    phase,
                ) = row
                event_number = int(event_id)
                if event_number < 1:
                    raise ValueError("invalid event id")
                parsed = datetime.fromisoformat(str(timestamp))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("naive audit timestamp")
                entry = AcceptanceAuditEntry(
                    event_id=event_number,
                    timestamp=parsed.astimezone(UTC),
                    mutation=AcceptanceMutation(str(mutation)),
                    request_id=str(request_id),
                    operator=str(operator),
                    acceptance_run_id=str(run_id),
                    result=str(result),
                    previous_status=(
                        AcceptanceStatus(str(previous_status))
                        if previous_status is not None
                        else None
                    ),
                    new_status=(
                        AcceptanceStatus(str(new_status)) if new_status is not None else None
                    ),
                    phase=AcceptancePhase(str(phase)),
                )
                _validate_audit_text(entry)
            except (TypeError, ValueError):
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.RECOVERY_REQUIRED,
                    "persisted acceptance audit is invalid",
                ) from None
            entries.append(entry)
        return tuple(entries)


def _validate_audit_text(entry: AcceptanceAuditEntry) -> None:
    values = (
        entry.request_id,
        entry.operator,
        entry.acceptance_run_id,
        entry.result,
    )
    if any(
        not value
        or len(value) > 128
        or any(character in value for character in ("/", "\\", "=", "\n", "\r"))
        for value in values
    ):
        raise ValueError("unsafe audit text")
