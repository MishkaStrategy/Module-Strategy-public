from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .engine import BacktestRunReference
from .models import SpecificationError


class StateTransitionError(RuntimeError):
    """Raised when a durable run state transition is not legal."""


class RunStatus(StrEnum):
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    BLOCKED = "BLOCKED"
    CORRUPTED = "CORRUPTED"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELED,
            RunStatus.BLOCKED,
            RunStatus.CORRUPTED,
        }


class FailureCategory(StrEnum):
    TRANSIENT = "TRANSIENT"
    RESOURCE = "RESOURCE"
    INPUT = "INPUT"
    ENGINE = "ENGINE"
    CORRUPTION = "CORRUPTION"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PLANNED: frozenset({RunStatus.QUEUED, RunStatus.CANCELED, RunStatus.BLOCKED}),
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELED, RunStatus.BLOCKED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.RETRY_WAIT,
            RunStatus.FAILED,
            RunStatus.CANCELED,
            RunStatus.CORRUPTED,
        }
    ),
    RunStatus.RETRY_WAIT: frozenset({RunStatus.QUEUED, RunStatus.CANCELED, RunStatus.BLOCKED}),
    RunStatus.SUCCEEDED: frozenset({RunStatus.CORRUPTED}),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
    RunStatus.BLOCKED: frozenset({RunStatus.QUEUED, RunStatus.CANCELED}),
    RunStatus.CORRUPTED: frozenset(),
}


def ensure_transition(current: RunStatus, target: RunStatus) -> None:
    if target is current:
        return
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise StateTransitionError(f"illegal run transition {current.value} -> {target.value}")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SpecificationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FailureRecord:
    category: FailureCategory
    message: str
    occurred_at: datetime
    retryable: bool
    exception_type: str | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise SpecificationError("failure message must be non-empty")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "failure time"))
        if self.exception_type is not None and not self.exception_type.strip():
            raise SpecificationError("exception_type must be non-empty when provided")


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: str
    experiment_id: str
    status: RunStatus
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    revision: int
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    failure: FailureRecord | None = None
    result: BacktestRunReference | None = None

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise SpecificationError("attempt_count must be non-negative")
        if self.max_attempts < 1 or self.attempt_count > self.max_attempts:
            raise SpecificationError("attempt counters are inconsistent")
        if self.revision < 0:
            raise SpecificationError("revision must be non-negative")
        for name in ("created_at", "updated_at", "available_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.lease_expires_at is not None:
            object.__setattr__(
                self,
                "lease_expires_at",
                _utc(self.lease_expires_at, "lease_expires_at"),
            )
        if self.status is RunStatus.RUNNING:
            if self.lease_owner is None or self.lease_expires_at is None:
                raise SpecificationError("running state requires an active lease")
        elif self.lease_owner is not None or self.lease_expires_at is not None:
            raise SpecificationError("only a running state may own a lease")
        if self.status is RunStatus.SUCCEEDED and self.result is None:
            raise SpecificationError("successful state requires a result reference")
        if self.status is not RunStatus.SUCCEEDED and self.result is not None:
            raise SpecificationError("only a successful state may contain a result")


@dataclass(frozen=True, slots=True)
class StateEvent:
    event_id: int
    run_id: str
    from_status: RunStatus | None
    to_status: RunStatus
    occurred_at: datetime
    revision: int
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "event time"))
