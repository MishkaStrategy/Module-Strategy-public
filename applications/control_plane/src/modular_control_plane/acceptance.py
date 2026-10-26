from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ComponentHealth, ComponentId, ContractModel, HealthStatus, SafeScalar
from .service import Clock, utc_now

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_CHECKPOINT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_WARNINGS = 32
_MAX_EVIDENCE = 64
_MAX_RESOURCES = 256


class AcceptancePhase(StrEnum):
    ENVIRONMENT = "ENVIRONMENT"
    DATASET = "DATASET"
    BACKTEST_A = "BACKTEST_A"
    BACKTEST_B = "BACKTEST_B"
    DETERMINISM = "DETERMINISM"
    RESEARCH = "RESEARCH"
    RUNTIME_SMOKE = "RUNTIME_SMOKE"
    RECOVERY = "RECOVERY"
    SOAK_2H = "SOAK_2H"
    SOAK_24H = "SOAK_24H"
    SOAK_72H = "SOAK_72H"
    FINAL_VERIFICATION = "FINAL_VERIFICATION"


class AcceptanceStatus(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED, self.BLOCKED}


class AcceptanceMutation(StrEnum):
    START = "START"
    STOP = "STOP"
    CANCEL = "CANCEL"
    RETRY = "RETRY"


class AcceptanceCandidate(ContractModel):
    full_rc_sha: str
    dataset_id: str
    dataset_digest: str
    strategy_id: str
    strategy_digest: str
    config_digest: str
    mode: str
    enable_soak_24h: bool = False
    enable_soak_72h: bool = False

    @model_validator(mode="after")
    def validate_identity(self) -> AcceptanceCandidate:
        if _GIT_SHA.fullmatch(self.full_rc_sha) is None:
            raise ValueError("full_rc_sha must be a lowercase 40-character Git SHA")
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("strategy_id", self.strategy_id),
        ):
            _identifier(value, name)
        for name, value in (
            ("dataset_digest", self.dataset_digest),
            ("strategy_digest", self.strategy_digest),
            ("config_digest", self.config_digest),
        ):
            _sha256(value, name)
        if self.mode != "PAPER":
            raise ValueError("System Acceptance Control is PAPER-only")
        if self.enable_soak_72h and not self.enable_soak_24h:
            raise ValueError("72h soak requires 24h soak")
        return self


class AcceptanceCheckpoint(ContractModel):
    phase: AcceptancePhase
    updated_at: datetime
    token: str = Field(min_length=1, max_length=256)
    digest: str

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: datetime) -> datetime:
        return _aware(value, "checkpoint.updated_at")

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if _CHECKPOINT_TOKEN.fullmatch(value) is None:
            raise ValueError("checkpoint.token must be a bounded portable token")
        return value

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _sha256(value, "checkpoint.digest")


class AcceptanceResourceSample(ContractModel):
    timestamp: datetime
    cpu_percent: float = Field(ge=0.0, le=100.0)
    ram_bytes: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "resource.timestamp")


class AcceptanceEvidenceReference(ContractModel):
    kind: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9._-]{0,63}$",
    )
    digest: str
    reference: str = Field(min_length=1, max_length=128)

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _sha256(value, "evidence.digest")

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        return _identifier(value, "evidence.reference")


class AcceptanceRunSnapshot(ContractModel):
    acceptance_run_id: str = Field(min_length=1, max_length=128)
    status: AcceptanceStatus
    phase: AcceptancePhase
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    completed_at: datetime | None = None
    active_test_time: float = Field(ge=0.0)
    wall_clock_elapsed: float = Field(ge=0.0)
    restart_count: int = Field(ge=0)
    checkpoint: AcceptanceCheckpoint | None = None
    resources: tuple[AcceptanceResourceSample, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = Field(default=None, max_length=128)
    next_action: str | None = Field(default=None, max_length=128)
    evidence: tuple[AcceptanceEvidenceReference, ...] = ()
    identity: AcceptanceCandidate
    phases: tuple[AcceptancePhase, ...]

    @field_validator("acceptance_run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return _identifier(value, "acceptance_run_id")

    @field_validator("created_at", "started_at", "updated_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _aware(value, str(getattr(info, "field_name", "timestamp")))

    @field_validator("resources")
    @classmethod
    def validate_resources(
        cls,
        value: tuple[AcceptanceResourceSample, ...],
    ) -> tuple[AcceptanceResourceSample, ...]:
        if len(value) > _MAX_RESOURCES:
            raise ValueError("Acceptance resources exceed the bounded sample limit")
        return value

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > _MAX_WARNINGS:
            raise ValueError("Acceptance warnings exceed the bounded entry limit")
        if any(
            not item or len(item) > 128 or _IDENTIFIER.fullmatch(item) is None for item in value
        ):
            raise ValueError("Acceptance warnings must be bounded portable owner codes")
        return value

    @field_validator("error", "next_action")
    @classmethod
    def validate_optional_owner_codes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _identifier(value, "owner status code")

    @field_validator("evidence")
    @classmethod
    def validate_evidence(
        cls,
        value: tuple[AcceptanceEvidenceReference, ...],
    ) -> tuple[AcceptanceEvidenceReference, ...]:
        if len(value) > _MAX_EVIDENCE:
            raise ValueError("Acceptance evidence exceeds the bounded entry limit")
        return value

    @field_validator("phases")
    @classmethod
    def validate_phases(
        cls,
        value: tuple[AcceptancePhase, ...],
    ) -> tuple[AcceptancePhase, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Acceptance phases must be non-empty and unique")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> AcceptanceRunSnapshot:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not precede created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")
        if self.status.terminal and self.completed_at is None:
            raise ValueError("terminal Acceptance runs require completed_at")
        if not self.status.terminal and self.completed_at is not None:
            raise ValueError("non-terminal Acceptance runs must not publish completed_at")
        if self.phase not in self.phases:
            raise ValueError("current Acceptance phase must be enabled for the run")
        return self


class AcceptanceCurrent(ContractModel):
    run: AcceptanceRunSnapshot | None


class AcceptanceStartRequest(ContractModel):
    request_id: str
    dataset_id: str
    dataset_digest: str
    strategy_id: str
    strategy_digest: str
    config_digest: str
    enable_soak_24h: bool = False
    enable_soak_72h: bool = False

    @model_validator(mode="after")
    def validate_request(self) -> AcceptanceStartRequest:
        _request_id(self.request_id)
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("strategy_id", self.strategy_id),
        ):
            _identifier(value, name)
        for name, value in (
            ("dataset_digest", self.dataset_digest),
            ("strategy_digest", self.strategy_digest),
            ("config_digest", self.config_digest),
        ):
            _sha256(value, name)
        if self.enable_soak_72h and not self.enable_soak_24h:
            raise ValueError("72h soak requires 24h soak")
        return self


class AcceptanceRunMutationRequest(ContractModel):
    request_id: str
    acceptance_run_id: str = Field(min_length=1, max_length=128)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _request_id(value)

    @field_validator("acceptance_run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return _identifier(value, "acceptance_run_id")


class AcceptanceMutationResult(ContractModel):
    request_id: str
    operator: str = Field(min_length=1, max_length=128)
    mutation: AcceptanceMutation
    acceptance_run_id: str = Field(min_length=1, max_length=128)
    status: AcceptanceStatus
    phase: AcceptancePhase
    completed_at: datetime
    replayed: bool

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _request_id(value)

    @field_validator("operator", "acceptance_run_id")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _identifier(value, str(getattr(info, "field_name", "identifier")))

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _aware(value, "completed_at")


class AcceptanceOwnerResponseError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        request_id: str | None = None,
        acceptance_run_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code[:128]
        self.request_id = _safe_request_id(request_id)
        self.acceptance_run_id = _safe_identifier(acceptance_run_id)


class AcceptanceOwnerClient(Protocol):
    @property
    def operator_available(self) -> bool: ...

    def current(self) -> AcceptanceCurrent: ...

    def start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult: ...

    def stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult: ...

    def cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult: ...

    def retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult: ...


class AcceptanceControlService:
    component = ComponentId.ACCEPTANCE

    def __init__(self, client: AcceptanceOwnerClient, *, clock: Clock = utc_now) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._client = client
        self._clock = clock

    def health(self) -> ComponentHealth:
        current = self._client.current()
        run = current.run
        details: dict[str, SafeScalar] = {
            "owner_attached": True,
            "mutations_available": self._client.operator_available,
            "current_run": run is not None,
        }
        status = HealthStatus.HEALTHY
        summary = "System Acceptance owner is available"
        if run is not None:
            details.update(
                {
                    "acceptance_run_id": run.acceptance_run_id,
                    "run_status": run.status.value,
                    "phase": run.phase.value,
                    "restart_count": run.restart_count,
                    "active_test_time": run.active_test_time,
                    "wall_clock_elapsed": run.wall_clock_elapsed,
                }
            )
            if run.status in {AcceptanceStatus.FAILED, AcceptanceStatus.BLOCKED}:
                status = HealthStatus.DEGRADED
                summary = "System Acceptance owner reports a failed or blocked run"
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=self._checked_at(),
            summary=summary,
            details=details,
        )

    def current(self) -> AcceptanceCurrent:
        return self._client.current()

    def start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult:
        return self._client.start(request)

    def stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._client.stop(request)

    def cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._client.cancel(request)

    def retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._client.retry(request)

    def _checked_at(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        return _aware(value, "checked_at")


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _sha256(value: str, name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: str, name: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded portable identifier")
    return value


def _request_id(value: str) -> str:
    if _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id must be a bounded portable identifier")
    return value


def _safe_request_id(value: str | None) -> str | None:
    if value is None or _REQUEST_ID.fullmatch(value) is None:
        return None
    return value


def _safe_identifier(value: str | None) -> str | None:
    if value is None or _IDENTIFIER.fullmatch(value) is None:
        return None
    return value


__all__ = [
    "AcceptanceCandidate",
    "AcceptanceCheckpoint",
    "AcceptanceControlService",
    "AcceptanceCurrent",
    "AcceptanceEvidenceReference",
    "AcceptanceMutation",
    "AcceptanceMutationResult",
    "AcceptanceOwnerClient",
    "AcceptanceOwnerResponseError",
    "AcceptancePhase",
    "AcceptanceResourceSample",
    "AcceptanceRunMutationRequest",
    "AcceptanceRunSnapshot",
    "AcceptanceStartRequest",
    "AcceptanceStatus",
]
