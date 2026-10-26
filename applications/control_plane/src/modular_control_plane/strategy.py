from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator

from .contracts import ComponentHealth, ComponentId, ContractModel, HealthStatus, JobStatus
from .service import Clock, utc_now


class StrategyFactoryStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DATASET_REJECTED = "DATASET_REJECTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    BLOCKED = "BLOCKED"
    CORRUPTED = "CORRUPTED"


class StrategyFactoryStage(StrEnum):
    DATA_GATE = "data_gate"
    INSTRUMENT_PROFILE = "instrument_profile"
    REGIME_DISCOVERY = "regime_discovery"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    ARCHITECTURE_GENERATION = "architecture_generation"
    EXPERIMENT_PLANNING = "experiment_planning"
    STRUCTURAL_SCREENING = "structural_screening"
    COARSE_SEARCH = "coarse_search"
    LOCAL_REFINEMENT = "local_refinement"
    WALK_FORWARD = "walk_forward"
    ROBUSTNESS = "robustness"
    HOLDOUT = "holdout"
    SIMPLICITY_SELECTION = "simplicity_selection"
    FINAL_AUDIT = "final_audit"
    PACKAGE = "package"
    PAPER_ADMISSION = "paper_admission"


class StrategyRunSummary(ContractModel):
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    factory_status: StrategyFactoryStatus
    status: JobStatus
    stage: StrategyFactoryStage
    stage_sequence: int = Field(ge=0)
    updated_at: datetime
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    manifest_id: str = Field(min_length=1, max_length=128)
    completed_artifact_count: int = Field(ge=0)
    submitted_batch_count: int = Field(ge=0)
    processed_event_count: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    decision_chain_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_chain_verified: bool = True

    @field_validator("updated_at")
    @classmethod
    def require_aware_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return value.astimezone(UTC)


class StrategyRunList(ContractModel):
    runs: tuple[StrategyRunSummary, ...]
    total_valid: int = Field(ge=0)
    invalid_count: int = Field(ge=0)
    ignored_count: int = Field(ge=0)
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class StrategyFactorySnapshot:
    run_id: str
    factory_status: str
    stage: str
    stage_sequence: int
    updated_at: datetime
    request_fingerprint: str
    dataset_fingerprint: str
    commit_sha: str
    manifest_id: str
    completed_artifact_count: int
    submitted_batch_count: int
    processed_event_count: int
    decision_count: int
    decision_chain_head: str | None
    decision_chain_verified: bool


@dataclass(frozen=True, slots=True)
class StrategyFactoryScan:
    runs: tuple[StrategyFactorySnapshot, ...]
    invalid_count: int = 0
    ignored_count: int = 0
    truncated: bool = False


class StrategyFactoryReader(Protocol):
    def scan(self, *, max_runs: int) -> StrategyFactoryScan: ...

    def get(self, run_id: str) -> StrategyFactorySnapshot: ...


class StrategyRunNotFound(RuntimeError):
    pass


class StrategyRunCorrupted(RuntimeError):
    pass


class StrategyControlService:
    component = ComponentId.STRATEGY

    def __init__(
        self,
        reader: StrategyFactoryReader,
        *,
        clock: Clock = utc_now,
        health_scan_limit: int = 500,
    ) -> None:
        if not 1 <= health_scan_limit <= 2_000:
            raise ValueError("health_scan_limit must be between 1 and 2000")
        self._reader = reader
        self._clock = clock
        self._health_scan_limit = health_scan_limit

    def health(self) -> ComponentHealth:
        try:
            scan = self._reader.scan(max_runs=self._health_scan_limit)
        except Exception as exc:
            return ComponentHealth(
                component=self.component,
                status=HealthStatus.UNAVAILABLE,
                checked_at=self._now(),
                summary="Strategy Factory state is unavailable",
                details={"error_type": type(exc).__name__, "reason": "strategy_state_unavailable"},
            )
        status = HealthStatus.HEALTHY
        summary = "Strategy Factory governance state is readable"
        if scan.invalid_count:
            status = HealthStatus.DEGRADED
            summary = "Strategy Factory contains unreadable or integrity-failed run state"
        elif scan.truncated:
            status = HealthStatus.DEGRADED
            summary = "Strategy Factory run scan reached the bounded Control Plane limit"
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=self._now(),
            summary=summary,
            details={
                "runs_visible": len(scan.runs),
                "runs_invalid": scan.invalid_count,
                "entries_ignored": scan.ignored_count,
                "scan_truncated": scan.truncated,
            },
        )

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> StrategyRunList:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        max_runs = min(2_000, max(self._health_scan_limit, offset + limit))
        scan = self._reader.scan(max_runs=max_runs)
        ordered = tuple(
            sorted(scan.runs, key=lambda item: (item.updated_at, item.run_id), reverse=True)
        )
        selected = ordered[offset : offset + limit]
        return StrategyRunList(
            runs=tuple(self._public(item) for item in selected),
            total_valid=len(ordered),
            invalid_count=scan.invalid_count,
            ignored_count=scan.ignored_count,
            truncated=scan.truncated,
        )

    def get_run(self, run_id: str) -> StrategyRunSummary:
        return self._public(self._reader.get(run_id))

    def _public(self, snapshot: StrategyFactorySnapshot) -> StrategyRunSummary:
        native_status = StrategyFactoryStatus(snapshot.factory_status)
        return StrategyRunSummary(
            run_id=snapshot.run_id,
            factory_status=native_status,
            status=_project_status(native_status),
            stage=StrategyFactoryStage(snapshot.stage),
            stage_sequence=snapshot.stage_sequence,
            updated_at=snapshot.updated_at,
            request_fingerprint=snapshot.request_fingerprint,
            dataset_fingerprint=snapshot.dataset_fingerprint,
            commit_sha=snapshot.commit_sha,
            manifest_id=snapshot.manifest_id,
            completed_artifact_count=snapshot.completed_artifact_count,
            submitted_batch_count=snapshot.submitted_batch_count,
            processed_event_count=snapshot.processed_event_count,
            decision_count=snapshot.decision_count,
            decision_chain_head=snapshot.decision_chain_head,
            decision_chain_verified=snapshot.decision_chain_verified,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _project_status(status: StrategyFactoryStatus) -> JobStatus:
    return {
        StrategyFactoryStatus.CREATED: JobStatus.QUEUED,
        StrategyFactoryStatus.RUNNING: JobStatus.RUNNING,
        StrategyFactoryStatus.PAUSED: JobStatus.PAUSED,
        StrategyFactoryStatus.CANCELED: JobStatus.CANCELLED,
        StrategyFactoryStatus.COMPLETED: JobStatus.SUCCEEDED,
        StrategyFactoryStatus.FAILED: JobStatus.FAILED,
        StrategyFactoryStatus.DATASET_REJECTED: JobStatus.FAILED,
        StrategyFactoryStatus.BUDGET_EXHAUSTED: JobStatus.BLOCKED,
        StrategyFactoryStatus.BLOCKED: JobStatus.BLOCKED,
        StrategyFactoryStatus.CORRUPTED: JobStatus.FAILED,
    }[status]
