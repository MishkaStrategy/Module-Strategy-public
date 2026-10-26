from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """Strict immutable public contract used by Control Plane transports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1


class ComponentId(StrEnum):
    MARKET_DATA = "market_data"
    BACKTEST = "backtest"
    RESEARCH = "research"
    STRATEGY = "strategy"
    PAPER_RUNTIME = "paper_runtime"
    ACCEPTANCE = "acceptance"
    SIGNAL_BOT = "signal_bot"
    EVIDENCE = "evidence"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class EventType(StrEnum):
    STATE = "state"
    PROGRESS = "progress"
    LOG = "log"
    WARNING = "warning"
    RESULT = "result"
    ERROR = "error"
    AUDIT = "audit"


SafeScalar = str | int | float | bool | None


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _bounded_details(value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
    if len(value) > 64:
        raise ValueError("details must contain at most 64 entries")
    normalized: dict[str, SafeScalar] = {}
    for key, item in value.items():
        name = key.strip()
        if not name or len(name) > 128:
            raise ValueError("detail keys must contain 1..128 non-whitespace characters")
        if isinstance(item, str) and len(item) > 1000:
            raise ValueError("string detail values must contain at most 1000 characters")
        normalized[name] = item
    return dict(sorted(normalized.items()))


class ComponentHealth(ContractModel):
    component: ComponentId
    status: HealthStatus
    checked_at: datetime
    summary: str = Field(min_length=1, max_length=500)
    details: dict[str, SafeScalar] = Field(default_factory=dict)

    @field_validator("checked_at")
    @classmethod
    def require_aware_checked_at(cls, value: datetime) -> datetime:
        return _aware(value, "checked_at")

    @field_validator("details")
    @classmethod
    def validate_details(cls, value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        return _bounded_details(value)


class JobProgress(ContractModel):
    phase: str = Field(min_length=1, max_length=128)
    determinate: bool = False
    completed_units: int = Field(default=0, ge=0)
    total_units: int | None = Field(default=None, ge=1)
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    message: str | None = Field(default=None, max_length=500)
    counters: dict[str, int] = Field(default_factory=dict)

    @field_validator("counters")
    @classmethod
    def validate_counters(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > 64:
            raise ValueError("counters must contain at most 64 entries")
        normalized: dict[str, int] = {}
        for key, count in value.items():
            name = key.strip()
            if not name or len(name) > 128 or count < 0:
                raise ValueError("counter names must be bounded and values non-negative")
            normalized[name] = count
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def validate_progress(self) -> JobProgress:
        if self.determinate:
            if self.total_units is None:
                raise ValueError("determinate progress requires total_units")
            if self.completed_units > self.total_units:
                raise ValueError("completed_units must not exceed total_units")
            expected = round(self.completed_units * 100.0 / self.total_units, 6)
            if self.percent is not None and abs(self.percent - expected) > 0.001:
                raise ValueError("percent must match completed_units / total_units")
            object.__setattr__(self, "percent", expected)
        elif self.total_units is not None or self.percent is not None:
            raise ValueError("indeterminate progress must not publish total_units or percent")
        return self


class JobError(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    action: str | None = Field(default=None, max_length=2000)
    retryable: bool = False
    error_id: str = Field(min_length=1, max_length=128)
    details: dict[str, SafeScalar] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def validate_details(cls, value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        return _bounded_details(value)


class JobSummary(ContractModel):
    job_id: str = Field(min_length=1, max_length=128)
    component: ComponentId
    operation: str = Field(min_length=1, max_length=128)
    status: JobStatus
    phase: str = Field(min_length=1, max_length=128)
    progress: JobProgress | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: JobError | None = None

    @field_validator("created_at", "updated_at", "started_at", "completed_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "timestamp")
        return _aware(value, str(field_name))

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> JobSummary:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not precede created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")
        if self.status.terminal and self.completed_at is None:
            raise ValueError("terminal jobs require completed_at")
        if not self.status.terminal and self.completed_at is not None:
            raise ValueError("non-terminal jobs must not publish completed_at")
        if self.status is JobStatus.FAILED and self.error is None:
            raise ValueError("failed jobs require error details")
        if self.status is not JobStatus.FAILED and self.error is not None:
            raise ValueError("only failed jobs may publish error details")
        return self


class ControlEvent(ContractModel):
    event_id: str = Field(min_length=1, max_length=256)
    source_cursor: str | None = Field(default=None, max_length=256)
    component: ComponentId
    resource_type: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=256)
    operation: str | None = Field(default=None, max_length=128)
    event_type: EventType
    status: JobStatus | None = None
    phase: str | None = Field(default=None, max_length=128)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, SafeScalar] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "timestamp")

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        return _bounded_details(value)


class SystemOverview(ContractModel):
    mode: Literal["PAPER"] = "PAPER"
    status: HealthStatus
    checked_at: datetime
    components: tuple[ComponentHealth, ...]

    @field_validator("checked_at")
    @classmethod
    def require_aware_checked_at(cls, value: datetime) -> datetime:
        return _aware(value, "checked_at")

    @model_validator(mode="after")
    def validate_components(self) -> SystemOverview:
        identities = tuple(item.component for item in self.components)
        if len(set(identities)) != len(identities):
            raise ValueError("components must be unique")
        if identities != tuple(sorted(identities, key=str)):
            raise ValueError("components must be sorted by component id")
        return self
