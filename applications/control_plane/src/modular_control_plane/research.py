from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ComponentHealth, ComponentId, ContractModel, HealthStatus, JobStatus
from .service import Clock, utc_now


class ResearchExperimentNativeStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CORRUPTED = "CORRUPTED"


class ResearchRunNativeStatus(StrEnum):
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    BLOCKED = "BLOCKED"
    CORRUPTED = "CORRUPTED"


class ResearchFailureCategory(StrEnum):
    TRANSIENT = "TRANSIENT"
    RESOURCE = "RESOURCE"
    INPUT = "INPUT"
    ENGINE = "ENGINE"
    CORRUPTION = "CORRUPTION"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


class ResearchExperimentSummary(ContractModel):
    experiment_id: str = Field(min_length=1, max_length=256)
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_status: ResearchExperimentNativeStatus
    status: JobStatus
    has_failures: bool
    created_at: datetime
    updated_at: datetime
    run_counts: dict[ResearchRunNativeStatus, int]

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Research timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("run_counts")
    @classmethod
    def validate_run_counts(
        cls,
        value: dict[ResearchRunNativeStatus, int],
    ) -> dict[ResearchRunNativeStatus, int]:
        if len(value) > len(ResearchRunNativeStatus):
            raise ValueError("run_counts contains too many statuses")
        if any(count < 0 for count in value.values()):
            raise ValueError("run_counts must be non-negative")
        return dict(sorted(value.items(), key=lambda item: item[0].value))

    @model_validator(mode="after")
    def validate_projection(self) -> ResearchExperimentSummary:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status is not _project_experiment_status(self.native_status):
            raise ValueError("Research experiment common status does not match native status")
        expected_failures = (
            self.native_status is ResearchExperimentNativeStatus.COMPLETED_WITH_FAILURES
        )
        if self.has_failures is not expected_failures:
            raise ValueError("has_failures must match COMPLETED_WITH_FAILURES native status")
        return self


class ResearchExperimentList(ContractModel):
    experiments: tuple[ResearchExperimentSummary, ...]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool


class ResearchRunSummary(ContractModel):
    run_id: str = Field(min_length=1, max_length=256)
    experiment_id: str = Field(min_length=1, max_length=256)
    native_status: ResearchRunNativeStatus
    status: JobStatus
    stage_id: str = Field(min_length=1, max_length=256)
    split_id: str = Field(min_length=1, max_length=256)
    dataset_id: str = Field(min_length=1, max_length=256)
    scenario_id: str = Field(min_length=1, max_length=256)
    stage_role: str = Field(min_length=1, max_length=256)
    repetition_index: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    revision: int = Field(ge=0)
    lease_expires_at: datetime | None
    failure_category: ResearchFailureCategory | None
    failure_retryable: bool | None
    engine_run_id: str | None = Field(default=None, max_length=256)
    bars_processed: int | None = Field(default=None, ge=0)
    restart_verified: bool | None

    @field_validator("created_at", "updated_at", "available_at", "lease_expires_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Research timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_projection(self) -> ResearchRunSummary:
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count must not exceed max_attempts")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status is not _project_run_status(self.native_status):
            raise ValueError("Research run common status does not match native status")
        if self.native_status is ResearchRunNativeStatus.RUNNING:
            if self.lease_expires_at is None:
                raise ValueError("RUNNING Research run requires lease_expires_at")
        elif self.lease_expires_at is not None:
            raise ValueError("only RUNNING Research run may expose lease_expires_at")
        if (self.failure_category is None) != (self.failure_retryable is None):
            raise ValueError("Research failure projection must be complete")
        result_values = (self.engine_run_id, self.bars_processed, self.restart_verified)
        complete_result = all(value is not None for value in result_values)
        if self.native_status is ResearchRunNativeStatus.SUCCEEDED:
            if not complete_result:
                raise ValueError("SUCCEEDED Research run requires complete result identity")
        elif any(value is not None for value in result_values):
            raise ValueError("only SUCCEEDED Research run may expose result identity")
        return self


class ResearchRunList(ContractModel):
    experiment_id: str = Field(min_length=1, max_length=256)
    runs: tuple[ResearchRunSummary, ...]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool


@dataclass(frozen=True, slots=True)
class ResearchExperimentSnapshot:
    experiment_id: str
    specification_sha256: str
    native_status: str
    created_at: datetime
    updated_at: datetime
    run_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ResearchRunSnapshot:
    run_id: str
    experiment_id: str
    native_status: str
    stage_id: str
    split_id: str
    dataset_id: str
    scenario_id: str
    stage_role: str
    repetition_index: int
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    revision: int
    lease_expires_at: datetime | None
    failure_category: str | None
    failure_retryable: bool | None
    engine_run_id: str | None
    bars_processed: int | None
    restart_verified: bool | None


class ResearchStateReader(Protocol):
    def list_experiments(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ResearchExperimentSnapshot, ...]: ...

    def get_experiment(self, experiment_id: str) -> ResearchExperimentSnapshot: ...

    def list_runs(
        self,
        experiment_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ResearchRunSnapshot, ...]: ...

    def get_run(self, run_id: str) -> ResearchRunSnapshot: ...


class ResearchNotFound(RuntimeError):
    pass


class ResearchStateInvalid(RuntimeError):
    pass


class ResearchControlService:
    component = ComponentId.RESEARCH

    def __init__(
        self,
        reader: ResearchStateReader,
        *,
        clock: Clock = utc_now,
        health_limit: int = 100,
    ) -> None:
        if not 1 <= health_limit <= 200:
            raise ValueError("health_limit must be between 1 and 200")
        self._reader = reader
        self._clock = clock
        self._health_limit = health_limit

    def health(self) -> ComponentHealth:
        try:
            snapshots = self._reader.list_experiments(limit=self._health_limit, offset=0)
            scan_bounded = len(snapshots) == self._health_limit and bool(
                self._reader.list_experiments(limit=1, offset=self._health_limit)
            )
            native = tuple(ResearchExperimentNativeStatus(item.native_status) for item in snapshots)
        except Exception as exc:
            return ComponentHealth(
                component=self.component,
                status=HealthStatus.UNAVAILABLE,
                checked_at=self._now(),
                summary="Research Runner durable state is unavailable",
                details={"error_type": type(exc).__name__, "reason": "research_state_unavailable"},
            )
        corrupted = sum(status is ResearchExperimentNativeStatus.CORRUPTED for status in native)
        status = HealthStatus.DEGRADED if corrupted else HealthStatus.HEALTHY
        summary = (
            "Research Runner durable state contains corrupted experiments"
            if corrupted
            else "Research Runner durable state is readable"
        )
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=self._now(),
            summary=summary,
            details={
                "experiments_visible": len(snapshots),
                "experiments_corrupted": corrupted,
                "scan_bounded": scan_bounded,
            },
        )

    def list_experiments(self, *, limit: int = 50, offset: int = 0) -> ResearchExperimentList:
        _validate_page(limit=limit, offset=offset)
        snapshots = self._reader.list_experiments(limit=limit, offset=offset)
        has_more = len(snapshots) == limit and bool(
            self._reader.list_experiments(limit=1, offset=offset + limit)
        )
        return ResearchExperimentList(
            experiments=tuple(self._public_experiment(item) for item in snapshots),
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    def get_experiment(self, experiment_id: str) -> ResearchExperimentSummary:
        return self._public_experiment(self._reader.get_experiment(experiment_id))

    def list_runs(
        self,
        experiment_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ResearchRunList:
        _validate_page(limit=limit, offset=offset)
        snapshots = self._reader.list_runs(experiment_id, limit=limit, offset=offset)
        has_more = len(snapshots) == limit and bool(
            self._reader.list_runs(experiment_id, limit=1, offset=offset + limit)
        )
        return ResearchRunList(
            experiment_id=experiment_id,
            runs=tuple(self._public_run(item) for item in snapshots),
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    def get_run(self, run_id: str) -> ResearchRunSummary:
        return self._public_run(self._reader.get_run(run_id))

    @staticmethod
    def _public_experiment(snapshot: ResearchExperimentSnapshot) -> ResearchExperimentSummary:
        native = ResearchExperimentNativeStatus(snapshot.native_status)
        counts: dict[ResearchRunNativeStatus, int] = {}
        for status, count in snapshot.run_counts.items():
            counts[ResearchRunNativeStatus(status)] = count
        return ResearchExperimentSummary(
            experiment_id=snapshot.experiment_id,
            specification_sha256=snapshot.specification_sha256,
            native_status=native,
            status=_project_experiment_status(native),
            has_failures=native is ResearchExperimentNativeStatus.COMPLETED_WITH_FAILURES,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            run_counts=counts,
        )

    @staticmethod
    def _public_run(snapshot: ResearchRunSnapshot) -> ResearchRunSummary:
        native = ResearchRunNativeStatus(snapshot.native_status)
        return ResearchRunSummary(
            run_id=snapshot.run_id,
            experiment_id=snapshot.experiment_id,
            native_status=native,
            status=_project_run_status(native),
            stage_id=snapshot.stage_id,
            split_id=snapshot.split_id,
            dataset_id=snapshot.dataset_id,
            scenario_id=snapshot.scenario_id,
            stage_role=snapshot.stage_role,
            repetition_index=snapshot.repetition_index,
            attempt_count=snapshot.attempt_count,
            max_attempts=snapshot.max_attempts,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            available_at=snapshot.available_at,
            revision=snapshot.revision,
            lease_expires_at=snapshot.lease_expires_at,
            failure_category=(
                ResearchFailureCategory(snapshot.failure_category)
                if snapshot.failure_category is not None
                else None
            ),
            failure_retryable=snapshot.failure_retryable,
            engine_run_id=snapshot.engine_run_id,
            bars_processed=snapshot.bars_processed,
            restart_verified=snapshot.restart_verified,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _validate_page(*, limit: int, offset: int) -> None:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be non-negative")


def _project_experiment_status(status: ResearchExperimentNativeStatus) -> JobStatus:
    return {
        ResearchExperimentNativeStatus.CREATED: JobStatus.QUEUED,
        ResearchExperimentNativeStatus.VALIDATING: JobStatus.STARTING,
        ResearchExperimentNativeStatus.PLANNED: JobStatus.QUEUED,
        ResearchExperimentNativeStatus.RUNNING: JobStatus.RUNNING,
        ResearchExperimentNativeStatus.PAUSED: JobStatus.PAUSED,
        ResearchExperimentNativeStatus.CANCELED: JobStatus.CANCELLED,
        ResearchExperimentNativeStatus.COMPLETED: JobStatus.SUCCEEDED,
        ResearchExperimentNativeStatus.COMPLETED_WITH_FAILURES: JobStatus.SUCCEEDED,
        ResearchExperimentNativeStatus.FAILED: JobStatus.FAILED,
        ResearchExperimentNativeStatus.BLOCKED: JobStatus.BLOCKED,
        ResearchExperimentNativeStatus.CORRUPTED: JobStatus.FAILED,
    }[status]


def _project_run_status(status: ResearchRunNativeStatus) -> JobStatus:
    return {
        ResearchRunNativeStatus.PLANNED: JobStatus.QUEUED,
        ResearchRunNativeStatus.QUEUED: JobStatus.QUEUED,
        ResearchRunNativeStatus.RUNNING: JobStatus.RUNNING,
        ResearchRunNativeStatus.RETRY_WAIT: JobStatus.RECOVERING,
        ResearchRunNativeStatus.SUCCEEDED: JobStatus.SUCCEEDED,
        ResearchRunNativeStatus.FAILED: JobStatus.FAILED,
        ResearchRunNativeStatus.CANCELED: JobStatus.CANCELLED,
        ResearchRunNativeStatus.BLOCKED: JobStatus.BLOCKED,
        ResearchRunNativeStatus.CORRUPTED: JobStatus.FAILED,
    }[status]
