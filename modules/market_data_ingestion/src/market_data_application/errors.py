from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import JobError


@dataclass(slots=True)
class ApplicationError(Exception):
    code: str
    message: str
    retryable: bool = False
    stage: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("error code must be non-empty")
        if not self.message.strip():
            raise ValueError("error message must be non-empty")
        Exception.__init__(self, self.message)

    def to_job_error(self, *, correlation_id: str) -> JobError:
        return JobError(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            stage=self.stage,
            details=self.details,
            correlation_id=correlation_id,
        )


class JobNotFoundError(ApplicationError):
    def __init__(self, job_id: str) -> None:
        super().__init__(
            "JOB_NOT_FOUND",
            "job was not found",
            False,
            "job_query",
            {"job_id": job_id},
        )


class DatasetNotFoundError(ApplicationError):
    def __init__(self, *, dataset_id: str, version: int | None) -> None:
        details: dict[str, Any] = {"dataset_id": dataset_id}
        if version is not None:
            details["version"] = version
        super().__init__(
            "DATASET_NOT_FOUND",
            "dataset was not found in the Dataset Registry",
            False,
            "dataset_query",
            details,
        )


class ExchangeNotFoundError(ApplicationError):
    def __init__(self, exchange_id: str) -> None:
        super().__init__(
            "EXCHANGE_NOT_FOUND",
            "exchange adapter is not configured",
            False,
            "exchange_query",
            {"exchange_id": exchange_id},
        )


class MarketUnsupportedError(ApplicationError):
    def __init__(self, *, exchange_id: str, market_type: str) -> None:
        super().__init__(
            "MARKET_UNSUPPORTED",
            "market type is not supported by the configured exchange adapter",
            False,
            "exchange_query",
            {"exchange_id": exchange_id, "market_type": market_type},
        )


class InstrumentNotFoundError(ApplicationError):
    def __init__(self, *, exchange_id: str, market_type: str, symbol: str) -> None:
        super().__init__(
            "INSTRUMENT_NOT_FOUND",
            "instrument was not found for the configured exchange and market",
            False,
            "instrument_query",
            {
                "exchange_id": exchange_id,
                "market_type": market_type,
                "symbol": symbol,
            },
        )


class InstrumentResolutionError(ApplicationError):
    def __init__(
        self,
        *,
        exchange_id: str,
        market_type: str,
        symbol: str,
        backend_code: str,
        retryable: bool,
    ) -> None:
        super().__init__(
            "INSTRUMENT_RESOLUTION_FAILED",
            "instrument metadata could not be resolved",
            retryable,
            "instrument_query",
            {
                "exchange_id": exchange_id,
                "market_type": market_type,
                "symbol": symbol,
                "backend_code": backend_code,
            },
        )


class ArtifactNotFoundError(ApplicationError):
    def __init__(self, artifact_id: str) -> None:
        super().__init__(
            "ARTIFACT_NOT_FOUND",
            "artifact was not found",
            False,
            "artifact_query",
            {"artifact_id": artifact_id},
        )


class ArtifactIntegrityError(ApplicationError):
    def __init__(self, artifact_id: str, reason: str) -> None:
        super().__init__(
            "ARTIFACT_INTEGRITY_FAILED",
            "artifact integrity validation failed",
            False,
            "artifact_storage",
            {"artifact_id": artifact_id, "reason": reason},
        )


class ArtifactLimitError(ApplicationError):
    def __init__(self, message: str, *, observed: int, limit: int) -> None:
        super().__init__(
            "ARTIFACT_LIMIT_EXCEEDED",
            message,
            False,
            "artifact_export",
            {"observed_bytes": observed, "limit_bytes": limit},
        )


class ArtifactStorageError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(
            "ARTIFACT_STORAGE_UNSAFE",
            "artifact storage configuration is unsafe",
            False,
            "artifact_storage",
            {"reason": reason},
        )


class RepairPreviewStaleError(ApplicationError):
    def __init__(self, *, dataset_id: str, supplied: str, current: str) -> None:
        super().__init__(
            "REPAIR_PREVIEW_STALE",
            "repair preview no longer matches the current immutable dataset evidence",
            False,
            "repair_confirmation",
            {"dataset_id": dataset_id, "supplied": supplied, "current": current},
        )


class RepairConfirmationConflictError(ApplicationError):
    def __init__(self, *, dataset_id: str, reason: str) -> None:
        super().__init__(
            "REPAIR_CONFIRMATION_CONFLICT",
            "repair confirmation does not exactly acknowledge the current preview",
            False,
            "repair_confirmation",
            {"dataset_id": dataset_id, "reason": reason},
        )


class RepairConfirmationNotFoundError(ApplicationError):
    def __init__(self, confirmation_id: str) -> None:
        super().__init__(
            "REPAIR_CONFIRMATION_NOT_FOUND",
            "repair confirmation was not found",
            False,
            "repair_confirmation",
            {"confirmation_id": confirmation_id},
        )


class RepairConfirmationRejectedError(ApplicationError):
    def __init__(self, *, confirmation_id: str, reason: str) -> None:
        super().__init__(
            "REPAIR_CONFIRMATION_REJECTED",
            "repair confirmation cannot be used",
            False,
            "repair_confirmation",
            {"confirmation_id": confirmation_id, "reason": reason},
        )


class IdempotencyConflictError(ApplicationError):
    def __init__(self, *, idempotency_key: str, job_id: str) -> None:
        super().__init__(
            "IDEMPOTENCY_KEY_CONFLICT",
            "idempotency key was already used for different canonical content",
            False,
            "job_creation",
            {"idempotency_key": idempotency_key, "job_id": job_id},
        )


class InvalidJobTransitionError(ApplicationError):
    def __init__(self, *, job_id: str, state: str, target: str) -> None:
        super().__init__(
            "INVALID_JOB_TRANSITION",
            "job state transition is not allowed",
            False,
            "job_transition",
            {"job_id": job_id, "state": state, "target": target},
        )


class JobCancellationConflictError(ApplicationError):
    def __init__(self, *, job_id: str, state: str) -> None:
        super().__init__(
            "JOB_CANCELLATION_CONFLICT",
            "job can no longer be cancelled in its current phase",
            False,
            "job_cancellation",
            {"job_id": job_id, "state": state},
        )


class JobCancellationUnavailableError(ApplicationError):
    def __init__(self, *, job_id: str, exchange_id: str) -> None:
        super().__init__(
            "JOB_CANCELLATION_UNAVAILABLE",
            "job cancellation identity cannot be resolved for the stored request",
            False,
            "job_cancellation",
            {"job_id": job_id, "exchange_id": exchange_id},
        )


class JobBackendIdentityConflictError(ApplicationError):
    def __init__(self, *, job_id: str, stored: str, derived: str) -> None:
        super().__init__(
            "JOB_BACKEND_IDENTITY_CONFLICT",
            "stored backend run identity differs from the canonical derived identity",
            False,
            "job_cancellation",
            {"job_id": job_id, "stored": stored, "derived": derived},
        )


class JobLeaseLostError(ApplicationError):
    def __init__(self, *, job_id: str, owner_id: str, generation: int) -> None:
        super().__init__(
            "JOB_LEASE_LOST",
            "job execution ownership was lost before the write could be committed",
            True,
            "job_execution",
            {
                "job_id": job_id,
                "owner_id": owner_id,
                "generation": generation,
            },
        )


class ApiAuthenticationError(ApplicationError):
    def __init__(self) -> None:
        super().__init__(
            "API_AUTHENTICATION_REQUIRED",
            "a valid bearer token is required",
            False,
            "api_authentication",
        )


class ApiAuthorizationError(ApplicationError):
    def __init__(
        self,
        *,
        principal_id: str,
        actual_role: str,
        required_role: str,
    ) -> None:
        super().__init__(
            "API_ACCESS_DENIED",
            "the authenticated principal does not have the required role",
            False,
            "api_authorization",
            {
                "principal_id": principal_id,
                "actual_role": actual_role,
                "required_role": required_role,
            },
        )
