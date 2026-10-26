from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .enums import KillSwitchScope, RuntimeState
from .errors import (
    BootstrapError,
    KillSwitchResetError,
    LifecycleTransitionError,
    TradingRunnerError,
)
from .ports import RuntimeStateStore
from .runtime import TradingRunner

_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OPERATOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_RESOURCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STORE_SCHEMA_VERSION = "1"
_STORE_SUFFIX = "operator-lifecycle:v1"
_MAX_RECORDS = 2048
_KILL_SWITCH_REASON = "operator_control"


class LifecycleOperation(StrEnum):
    START = "START"
    STOP = "STOP"
    ACTIVATE_KILL_SWITCH = "ACTIVATE_KILL_SWITCH"
    RESET_KILL_SWITCH = "RESET_KILL_SWITCH"


class LifecycleMutationErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    OWNER_FAILURE = "OWNER_FAILURE"
    PAPER_ONLY = "PAPER_ONLY"


class _MutationStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class TrustedOperatorIdentity:
    subject: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or _OPERATOR_PATTERN.fullmatch(self.subject) is None:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.INVALID_REQUEST,
                "operator identity is invalid",
            )


@dataclass(frozen=True, slots=True)
class LifecycleMutationRequest:
    request_id: str
    operator: TrustedOperatorIdentity
    operation: LifecycleOperation

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or _REQUEST_ID_PATTERN.fullmatch(self.request_id) is None
        ):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.INVALID_REQUEST,
                "request_id is invalid",
            )
        if not isinstance(self.operator, TrustedOperatorIdentity):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.INVALID_REQUEST,
                "operator must be TrustedOperatorIdentity",
            )
        if not isinstance(self.operation, LifecycleOperation):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.INVALID_REQUEST,
                "operation must be LifecycleOperation",
            )


@dataclass(frozen=True, slots=True)
class LifecycleMutationResult:
    request_id: str
    operator: str
    operation: LifecycleOperation
    runtime_id: str
    previous_state: RuntimeState
    new_state: RuntimeState
    config_hash: str
    completed_at: datetime
    replayed: bool = False


class LifecycleMutationError(TradingRunnerError):
    def __init__(
        self,
        code: LifecycleMutationErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        operation: LifecycleOperation | None = None,
    ) -> None:
        if not isinstance(code, LifecycleMutationErrorCode):
            raise TypeError("code must be LifecycleMutationErrorCode")
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.operation = operation


@dataclass(frozen=True, slots=True)
class _MutationRecord:
    request_id: str
    operator: str
    operation: LifecycleOperation
    status: _MutationStatus
    previous_state: RuntimeState
    new_state: RuntimeState | None
    created_at: datetime
    completed_at: datetime | None
    error_code: LifecycleMutationErrorCode | None

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "request_id": self.request_id,
            "operator": self.operator,
            "operation": self.operation.value,
            "status": self.status.value,
            "previous_state": self.previous_state.value,
            "new_state": self.new_state.value if self.new_state is not None else None,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_code": self.error_code.value if self.error_code else None,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> _MutationRecord:
        try:
            request_id = str(value["request_id"])
            operator = str(value["operator"])
            operation = LifecycleOperation(str(value["operation"]))
            status = _MutationStatus(str(value["status"]))
            previous_state = RuntimeState(str(value["previous_state"]))
            raw_new_state = value.get("new_state")
            new_state = RuntimeState(str(raw_new_state)) if raw_new_state is not None else None
            created_at = _parse_utc_timestamp(value["created_at"], "created_at")
            raw_completed_at = value.get("completed_at")
            completed_at = (
                _parse_utc_timestamp(raw_completed_at, "completed_at")
                if raw_completed_at is not None
                else None
            )
            raw_error_code = value.get("error_code")
            error_code = (
                LifecycleMutationErrorCode(str(raw_error_code))
                if raw_error_code is not None
                else None
            )
        except (KeyError, TypeError, ValueError):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted lifecycle mutation record is invalid",
            ) from None
        if _REQUEST_ID_PATTERN.fullmatch(request_id) is None:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted lifecycle request_id is invalid",
            )
        if _OPERATOR_PATTERN.fullmatch(operator) is None:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted lifecycle operator is invalid",
            )
        if status is _MutationStatus.SUCCEEDED and (new_state is None or completed_at is None):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted successful lifecycle mutation is incomplete",
            )
        if status is _MutationStatus.FAILED and (error_code is None or completed_at is None):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted failed lifecycle mutation is incomplete",
            )
        if status is _MutationStatus.IN_PROGRESS and completed_at is not None:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted in-progress lifecycle mutation is inconsistent",
            )
        return cls(
            request_id=request_id,
            operator=operator,
            operation=operation,
            status=status,
            previous_state=previous_state,
            new_state=new_state,
            created_at=created_at,
            completed_at=completed_at,
            error_code=error_code,
        )


def _parse_utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a timestamp string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LifecycleMutationError(
            LifecycleMutationErrorCode.OWNER_FAILURE,
            "owner clock returned an invalid timestamp",
        )
    return value.astimezone(UTC)


class TradingRunnerOperatorLifecycle:
    """Durable PAPER-only operator mutation boundary owned by Trading Runner."""

    def __init__(
        self,
        runner: TradingRunner,
        *,
        state_store: RuntimeStateStore | None = None,
    ) -> None:
        if not isinstance(runner, TradingRunner):
            raise TypeError("runner must be TradingRunner")
        if runner.config.execution_mode.value != "paper":
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.PAPER_ONLY,
                "operator lifecycle mutations are PAPER-only",
            )
        if _RESOURCE_PATTERN.fullmatch(runner.config.runtime_id) is None:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.OWNER_FAILURE,
                "runtime identity is not safe for operator lifecycle",
            )
        self.runner = runner
        self.state_store = state_store or runner.state_store
        self._lock = asyncio.Lock()
        self._store_key = f"{runner.config.runtime_id}:{_STORE_SUFFIX}"
        self._load_records()

    async def execute(self, request: LifecycleMutationRequest) -> LifecycleMutationResult:
        if not isinstance(request, LifecycleMutationRequest):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.INVALID_REQUEST,
                "request must be LifecycleMutationRequest",
            )
        async with self._lock:
            records = self._load_records()
            existing = next(
                (record for record in records if record.request_id == request.request_id),
                None,
            )
            if existing is not None:
                return self._replay_existing(existing, request)

            now = _utc(self.runner.clock.now())
            previous_state = self.runner.state
            record = _MutationRecord(
                request_id=request.request_id,
                operator=request.operator.subject,
                operation=request.operation,
                status=_MutationStatus.IN_PROGRESS,
                previous_state=previous_state,
                new_state=None,
                created_at=now,
                completed_at=None,
                error_code=None,
            )
            records = self._append_bounded(records, record)
            self._save_records(records)
            self._audit(request, result="ACCEPTED", previous_state=previous_state)

            try:
                await self._execute_native(request.operation)
            except Exception as exc:
                code = self._classify_owner_failure(exc)
                completed_at = _utc(self.runner.clock.now())
                failed = replace(
                    record,
                    status=_MutationStatus.FAILED,
                    new_state=self.runner.state,
                    completed_at=completed_at,
                    error_code=code,
                )
                try:
                    self._replace_record(records, failed)
                except Exception:
                    self._audit(
                        request,
                        result=LifecycleMutationErrorCode.RECOVERY_REQUIRED.value,
                        previous_state=previous_state,
                        new_state=self.runner.state,
                        error_code=LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                    )
                    raise LifecycleMutationError(
                        LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                        "mutation outcome requires owner recovery",
                        request_id=request.request_id,
                        operation=request.operation,
                    ) from None
                self._audit(
                    request,
                    result=code.value,
                    previous_state=previous_state,
                    new_state=self.runner.state,
                    error_code=code,
                )
                raise LifecycleMutationError(
                    code,
                    "owner lifecycle mutation failed",
                    request_id=request.request_id,
                    operation=request.operation,
                ) from None

            completed_at = _utc(self.runner.clock.now())
            result = LifecycleMutationResult(
                request_id=request.request_id,
                operator=request.operator.subject,
                operation=request.operation,
                runtime_id=self.runner.config.runtime_id,
                previous_state=previous_state,
                new_state=self.runner.state,
                config_hash=self.runner.config.canonical_hash,
                completed_at=completed_at,
            )
            succeeded = replace(
                record,
                status=_MutationStatus.SUCCEEDED,
                new_state=result.new_state,
                completed_at=completed_at,
            )
            try:
                self._replace_record(records, succeeded)
            except Exception:
                self._audit(
                    request,
                    result=LifecycleMutationErrorCode.RECOVERY_REQUIRED.value,
                    previous_state=previous_state,
                    new_state=self.runner.state,
                    error_code=LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                )
                raise LifecycleMutationError(
                    LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                    "mutation completed but durable result requires owner recovery",
                    request_id=request.request_id,
                    operation=request.operation,
                ) from None
            self._audit(
                request,
                result="SUCCEEDED",
                previous_state=previous_state,
                new_state=result.new_state,
            )
            return result

    def _replay_existing(
        self,
        record: _MutationRecord,
        request: LifecycleMutationRequest,
    ) -> LifecycleMutationResult:
        if record.operation is not request.operation or record.operator != request.operator.subject:
            self._audit(
                request,
                result=LifecycleMutationErrorCode.IDEMPOTENCY_CONFLICT.value,
                previous_state=self.runner.state,
                new_state=self.runner.state,
                error_code=LifecycleMutationErrorCode.IDEMPOTENCY_CONFLICT,
            )
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.IDEMPOTENCY_CONFLICT,
                "request_id was already used for a different lifecycle mutation",
                request_id=request.request_id,
                operation=request.operation,
            )
        if record.status is _MutationStatus.IN_PROGRESS:
            self._audit(
                request,
                result=LifecycleMutationErrorCode.RECOVERY_REQUIRED.value,
                previous_state=record.previous_state,
                new_state=self.runner.state,
                replayed=True,
                error_code=LifecycleMutationErrorCode.RECOVERY_REQUIRED,
            )
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "prior mutation outcome is uncertain; owner recovery is required",
                request_id=request.request_id,
                operation=request.operation,
            )
        if record.status is _MutationStatus.FAILED:
            code = record.error_code or LifecycleMutationErrorCode.OWNER_FAILURE
            self._audit(
                request,
                result=code.value,
                previous_state=record.previous_state,
                new_state=record.new_state or self.runner.state,
                replayed=True,
                error_code=code,
            )
            raise LifecycleMutationError(
                code,
                "replayed owner lifecycle mutation failure",
                request_id=request.request_id,
                operation=request.operation,
            )
        if record.new_state is None or record.completed_at is None:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "persisted mutation result is incomplete",
                request_id=request.request_id,
                operation=request.operation,
            )
        result = LifecycleMutationResult(
            request_id=record.request_id,
            operator=record.operator,
            operation=record.operation,
            runtime_id=self.runner.config.runtime_id,
            previous_state=record.previous_state,
            new_state=record.new_state,
            config_hash=self.runner.config.canonical_hash,
            completed_at=record.completed_at,
            replayed=True,
        )
        self._audit(
            request,
            result="REPLAYED",
            previous_state=result.previous_state,
            new_state=result.new_state,
            replayed=True,
        )
        return result

    async def _execute_native(self, operation: LifecycleOperation) -> None:
        if operation is LifecycleOperation.START:
            if self.runner.state is RuntimeState.RUNNING:
                return
            await self.runner.start()
            return
        if operation is LifecycleOperation.STOP:
            if self.runner.state in {RuntimeState.STOPPED, RuntimeState.DISPOSED}:
                return
            await self.runner.stop()
            return
        if operation is LifecycleOperation.ACTIVATE_KILL_SWITCH:
            if self.runner.kill_switch.active:
                return
            await self.runner.activate_kill_switch(
                reason=_KILL_SWITCH_REASON,
                scope=KillSwitchScope.GLOBAL,
            )
            return
        if operation is LifecycleOperation.RESET_KILL_SWITCH:
            if not self.runner.kill_switch.active:
                return
            await self.runner.reset_kill_switch()
            return
        raise LifecycleMutationError(
            LifecycleMutationErrorCode.INVALID_REQUEST,
            "unsupported lifecycle operation",
            operation=operation,
        )

    def _classify_owner_failure(self, exc: Exception) -> LifecycleMutationErrorCode:
        if isinstance(exc, LifecycleMutationError):
            return exc.code
        if isinstance(exc, (LifecycleTransitionError, KillSwitchResetError)):
            return LifecycleMutationErrorCode.INVALID_TRANSITION
        if self.runner.state in {RuntimeState.RECOVERING, RuntimeState.FAILED}:
            return LifecycleMutationErrorCode.RECOVERY_REQUIRED
        if isinstance(exc, BootstrapError):
            return LifecycleMutationErrorCode.INVALID_TRANSITION
        return LifecycleMutationErrorCode.OWNER_FAILURE

    def _load_records(self) -> list[_MutationRecord]:
        try:
            payload = self.state_store.load(self._store_key)
        except Exception:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store is unavailable",
            ) from None
        if payload is None:
            return []
        if not isinstance(payload, Mapping):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store payload is invalid",
            )
        if payload.get("schema_version") != _STORE_SCHEMA_VERSION:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store schema is unsupported",
            )
        if payload.get("runtime_id") != self.runner.config.runtime_id:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store runtime identity does not match",
            )
        if payload.get("config_hash") != self.runner.config.canonical_hash:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store configuration does not match",
            )
        raw_records = payload.get("records")
        if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation records are invalid",
            )
        records: list[_MutationRecord] = []
        seen: set[str] = set()
        for raw_record in raw_records:
            if not isinstance(raw_record, Mapping):
                raise LifecycleMutationError(
                    LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                    "lifecycle mutation record is invalid",
                )
            record = _MutationRecord.from_mapping(raw_record)
            if record.request_id in seen:
                raise LifecycleMutationError(
                    LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                    "duplicate persisted lifecycle request_id",
                )
            seen.add(record.request_id)
            records.append(record)
        if len(records) > _MAX_RECORDS:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store exceeds the bounded record limit",
            )
        return records

    def _append_bounded(
        self,
        records: list[_MutationRecord],
        record: _MutationRecord,
    ) -> list[_MutationRecord]:
        if len(records) >= _MAX_RECORDS:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.OWNER_FAILURE,
                "lifecycle mutation idempotency capacity is exhausted",
            )
        return [*records, record]

    def _replace_record(
        self,
        records: list[_MutationRecord],
        replacement: _MutationRecord,
    ) -> None:
        updated = [
            replacement if record.request_id == replacement.request_id else record
            for record in records
        ]
        self._save_records(updated)

    def _save_records(self, records: Sequence[_MutationRecord]) -> None:
        payload = {
            "schema_version": _STORE_SCHEMA_VERSION,
            "runtime_id": self.runner.config.runtime_id,
            "config_hash": self.runner.config.canonical_hash,
            "records": [record.to_mapping() for record in records],
        }
        try:
            self.state_store.save(self._store_key, payload)
        except LifecycleMutationError:
            raise
        except Exception:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation store write failed",
            ) from None

    def _audit(
        self,
        request: LifecycleMutationRequest,
        *,
        result: str,
        previous_state: RuntimeState,
        new_state: RuntimeState | None = None,
        replayed: bool = False,
        error_code: LifecycleMutationErrorCode | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "operation": request.operation.value,
            "resource": self.runner.config.runtime_id,
            "request_id": request.request_id,
            "operator": request.operator.subject,
            "result": result,
            "previous_state": previous_state.value,
            "new_state": (new_state or self.runner.state).value,
            "config_hash": self.runner.config.canonical_hash,
            "replayed": replayed,
        }
        if error_code is not None:
            payload["error_code"] = error_code.value
        try:
            self.runner.journal.append(
                timestamp=_utc(self.runner.clock.now()),
                event_type="operator.lifecycle",
                aggregate_id=self.runner.config.runtime_id,
                payload=payload,
                correlation_id=request.request_id,
            )
        except LifecycleMutationError:
            raise
        except Exception:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "owner audit journal is unavailable",
                request_id=request.request_id,
                operation=request.operation,
            ) from None
