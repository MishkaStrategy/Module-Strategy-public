from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, TypeAlias

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_EVIDENCE_KIND = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_CHECKPOINT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_MAX_WARNINGS = 32
_MAX_EVIDENCE = 64

Scalar: TypeAlias = str | int | float | bool | None


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


PHASE_ORDER: tuple[AcceptancePhase, ...] = tuple(AcceptancePhase)
SOAK_PHASES = frozenset(
    {
        AcceptancePhase.SOAK_2H,
        AcceptancePhase.SOAK_24H,
        AcceptancePhase.SOAK_72H,
    }
)
ACTIVE_TEST_PHASES = frozenset(PHASE_ORDER)


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


TERMINAL_STATUSES = frozenset(
    {
        AcceptanceStatus.SUCCEEDED,
        AcceptanceStatus.FAILED,
        AcceptanceStatus.CANCELLED,
        AcceptanceStatus.BLOCKED,
    }
)


class AcceptanceMutation(StrEnum):
    START = "START"
    STOP = "STOP"
    CANCEL = "CANCEL"
    RETRY = "RETRY"


class AcceptanceErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    STORAGE_FAILURE = "STORAGE_FAILURE"
    OWNER_FAILURE = "OWNER_FAILURE"
    PHASE_FAILURE = "PHASE_FAILURE"
    PHASE_BLOCKED = "PHASE_BLOCKED"
    CANCELLED = "CANCELLED"
    PAPER_ONLY = "PAPER_ONLY"


class AcceptanceOwnerError(RuntimeError):
    def __init__(
        self,
        code: AcceptanceErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if not isinstance(code, AcceptanceErrorCode):
            raise TypeError("code must be AcceptanceErrorCode")
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class TrustedOperator:
    subject: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or _IDENTIFIER.fullmatch(self.subject) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "operator identity is invalid",
            )


@dataclass(frozen=True, slots=True)
class AcceptanceRunSpec:
    full_rc_sha: str
    dataset_id: str
    dataset_digest: str
    strategy_id: str
    strategy_digest: str
    config_digest: str
    enable_soak_24h: bool = False
    enable_soak_72h: bool = False
    mode: str = "PAPER"

    def __post_init__(self) -> None:
        if not isinstance(self.full_rc_sha, str) or _GIT_SHA.fullmatch(self.full_rc_sha) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "full_rc_sha must be a lowercase 40-character Git SHA",
            )
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("strategy_id", self.strategy_id),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    f"{name} is invalid",
                )
        for name, value in (
            ("dataset_digest", self.dataset_digest),
            ("strategy_digest", self.strategy_digest),
            ("config_digest", self.config_digest),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    f"{name} must be a lowercase SHA-256 digest",
                )
        if not isinstance(self.enable_soak_24h, bool) or not isinstance(self.enable_soak_72h, bool):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "soak enable flags must be boolean",
            )
        if self.enable_soak_72h and not self.enable_soak_24h:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "72h soak requires 24h soak",
            )
        if self.mode != "PAPER":
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PAPER_ONLY,
                "System Acceptance owner is PAPER-only",
            )

    @property
    def phases(self) -> tuple[AcceptancePhase, ...]:
        phases = [
            AcceptancePhase.ENVIRONMENT,
            AcceptancePhase.DATASET,
            AcceptancePhase.BACKTEST_A,
            AcceptancePhase.BACKTEST_B,
            AcceptancePhase.DETERMINISM,
            AcceptancePhase.RESEARCH,
            AcceptancePhase.RUNTIME_SMOKE,
            AcceptancePhase.RECOVERY,
            AcceptancePhase.SOAK_2H,
        ]
        if self.enable_soak_24h:
            phases.append(AcceptancePhase.SOAK_24H)
        if self.enable_soak_72h:
            phases.append(AcceptancePhase.SOAK_72H)
        phases.append(AcceptancePhase.FINAL_VERIFICATION)
        return tuple(phases)


@dataclass(frozen=True, slots=True)
class AcceptanceMutationRequest:
    request_id: str
    operator: TrustedOperator
    mutation: AcceptanceMutation
    run_id: str | None = None
    spec: AcceptanceRunSpec | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or _REQUEST_ID.fullmatch(self.request_id) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "request_id is invalid",
            )
        if not isinstance(self.operator, TrustedOperator):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "operator must be TrustedOperator",
            )
        if not isinstance(self.mutation, AcceptanceMutation):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "mutation must be AcceptanceMutation",
            )
        if self.run_id is not None and (
            not isinstance(self.run_id, str) or _REQUEST_ID.fullmatch(self.run_id) is None
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "run_id is invalid",
            )
        if self.mutation is AcceptanceMutation.START:
            if self.spec is None or self.run_id is not None:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    "START requires spec and forbids run_id",
                )
        elif self.spec is not None or self.run_id is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "non-START mutations require run_id and forbid spec",
            )


@dataclass(frozen=True, slots=True)
class ResourceSample:
    timestamp: datetime
    cpu_percent: float
    ram_bytes: int
    disk_free_bytes: int

    def __post_init__(self) -> None:
        _aware(self.timestamp, "resource timestamp")
        if not isinstance(self.cpu_percent, (int, float)) or isinstance(self.cpu_percent, bool):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "cpu_percent must be numeric",
            )
        if not 0.0 <= float(self.cpu_percent) <= 100.0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "cpu_percent must be between 0 and 100",
            )
        for name, value in (
            ("ram_bytes", self.ram_bytes),
            ("disk_free_bytes", self.disk_free_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    f"{name} must be a non-negative integer",
                )


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    kind: str
    digest: str
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or _EVIDENCE_KIND.fullmatch(self.kind) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "evidence kind is invalid",
            )
        if not isinstance(self.digest, str) or _SHA256.fullmatch(self.digest) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "evidence digest is invalid",
            )
        if not isinstance(self.reference, str) or _IDENTIFIER.fullmatch(self.reference) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "evidence reference is invalid",
            )


@dataclass(frozen=True, slots=True)
class AcceptanceCheckpoint:
    phase: AcceptancePhase
    updated_at: datetime
    token: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.phase, AcceptancePhase):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "checkpoint phase is invalid",
            )
        _aware(self.updated_at, "checkpoint updated_at")
        if not isinstance(self.token, str) or _CHECKPOINT_TOKEN.fullmatch(self.token) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "checkpoint token is invalid",
            )
        if not isinstance(self.digest, str) or _SHA256.fullmatch(self.digest) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "checkpoint digest is invalid",
            )


class PhaseOutcomeStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    RUNNING = "RUNNING"


@dataclass(frozen=True, slots=True)
class PhaseOutcome:
    status: PhaseOutcomeStatus
    active_seconds: float = 0.0
    checkpoint: AcceptanceCheckpoint | None = None
    resources: tuple[ResourceSample, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    next_action: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PhaseOutcomeStatus):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase outcome status is invalid",
            )
        if (
            isinstance(self.active_seconds, bool)
            or not isinstance(self.active_seconds, (int, float))
            or not 0.0 <= float(self.active_seconds) <= 86_400.0
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase active_seconds is invalid",
            )
        if self.checkpoint is not None and not isinstance(self.checkpoint, AcceptanceCheckpoint):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase checkpoint is invalid",
            )
        if len(self.resources) > 16 or any(
            not isinstance(sample, ResourceSample) for sample in self.resources
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase resource samples are invalid",
            )
        if len(self.evidence) > _MAX_EVIDENCE or any(
            not isinstance(item, EvidenceReference) for item in self.evidence
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase evidence is invalid",
            )
        _warnings(self.warnings)
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or _IDENTIFIER.fullmatch(self.error_code) is None
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase error_code is invalid",
            )
        if self.next_action is not None and (
            not isinstance(self.next_action, str) or _IDENTIFIER.fullmatch(self.next_action) is None
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "phase next_action is invalid",
            )


@dataclass(frozen=True, slots=True)
class AcceptanceRunSnapshot:
    acceptance_run_id: str
    spec: AcceptanceRunSpec
    status: AcceptanceStatus
    phase: AcceptancePhase
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    active_test_time: float
    wall_clock_elapsed: float
    restart_count: int
    checkpoint: AcceptanceCheckpoint | None
    resources: tuple[ResourceSample, ...]
    warnings: tuple[str, ...]
    error: str | None
    next_action: str | None
    evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.acceptance_run_id, str)
            or _REQUEST_ID.fullmatch(self.acceptance_run_id) is None
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance_run_id is invalid",
            )
        if not isinstance(self.spec, AcceptanceRunSpec):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance spec is invalid",
            )
        if not isinstance(self.status, AcceptanceStatus) or not isinstance(
            self.phase, AcceptancePhase
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance status or phase is invalid",
            )
        if self.phase not in self.spec.phases:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance phase is disabled by the run spec",
            )
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.started_at is not None:
            _aware(self.started_at, "started_at")
        if self.completed_at is not None:
            _aware(self.completed_at, "completed_at")
        if (
            isinstance(self.active_test_time, bool)
            or not isinstance(self.active_test_time, (int, float))
            or self.active_test_time < 0
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted active_test_time is invalid",
            )
        if (
            isinstance(self.wall_clock_elapsed, bool)
            or not isinstance(self.wall_clock_elapsed, (int, float))
            or self.wall_clock_elapsed < 0
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted wall_clock_elapsed is invalid",
            )
        if isinstance(self.restart_count, bool) or not isinstance(self.restart_count, int):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted restart_count is invalid",
            )
        if not 0 <= self.restart_count <= 10_000:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted restart_count is out of bounds",
            )
        if len(self.resources) > 120 or any(
            not isinstance(sample, ResourceSample) for sample in self.resources
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted resource samples are invalid",
            )
        _warnings(self.warnings)
        if len(self.evidence) > _MAX_EVIDENCE or any(
            not isinstance(item, EvidenceReference) for item in self.evidence
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted evidence is invalid",
            )
        if self.error is not None and (
            not isinstance(self.error, str) or _IDENTIFIER.fullmatch(self.error) is None
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted error is invalid",
            )
        if self.next_action is not None and (
            not isinstance(self.next_action, str) or _IDENTIFIER.fullmatch(self.next_action) is None
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted next_action is invalid",
            )

    @property
    def phase_progress(self) -> Mapping[str, bool]:
        current_index = self.spec.phases.index(self.phase)
        return MappingProxyType(
            {phase.value: index < current_index for index, phase in enumerate(self.spec.phases)}
        )


@dataclass(frozen=True, slots=True)
class AcceptanceMutationResult:
    request_id: str
    operator: str
    mutation: AcceptanceMutation
    acceptance_run_id: str
    status: AcceptanceStatus
    phase: AcceptancePhase
    completed_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class AcceptanceAuditEntry:
    event_id: int
    timestamp: datetime
    mutation: AcceptanceMutation
    request_id: str
    operator: str
    acceptance_run_id: str
    result: str
    previous_status: AcceptanceStatus | None
    new_status: AcceptanceStatus | None
    phase: AcceptancePhase


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be timezone-aware",
        )
    return value.astimezone(UTC)


def _warnings(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or len(values) > _MAX_WARNINGS:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "warnings are invalid",
        )
    for value in values:
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "warning must be a bounded owner code",
            )
