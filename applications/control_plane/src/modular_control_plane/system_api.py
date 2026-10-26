from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from .api import ControlApiError, ReaderAuthorizer
from .api_contracts import ApiErrorDetail
from .observability import UnifiedObservabilitySnapshot
from .observability_service import ObservabilityControlService
from .service import Clock, utc_now
from .system_artifact_capacity import (
    ExternalArtifactCapacityControlService,
    ExternalArtifactCapacitySnapshot,
    external_artifact_capacity_service_from_environment,
)
from .system_capacity import (
    SystemCapacityControlService,
    SystemCapacitySnapshot,
    system_capacity_service_from_environment,
)
from .system_resources import SystemResourcesControlService, SystemResourcesSnapshot

SafeTechnicalDetail = str | int | float | bool | None


def create_system_resources_router(
    control: SystemResourcesControlService | None,
    *,
    observability_control: ObservabilityControlService | None = None,
    capacity_control: SystemCapacityControlService | None = None,
    artifact_capacity_control: ExternalArtifactCapacityControlService | None = None,
    attach_environment_capacity: bool = True,
    authorize_reader: ReaderAuthorizer | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()
    resolved_capacity = capacity_control
    resolved_artifact_capacity = artifact_capacity_control
    if attach_environment_capacity:
        if resolved_capacity is None:
            resolved_capacity = system_capacity_service_from_environment(clock=clock)
        if resolved_artifact_capacity is None:
            resolved_artifact_capacity = external_artifact_capacity_service_from_environment(
                clock=clock
            )

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    @router.get(
        "/api/control/v1/system/resources",
        response_model=SystemResourcesSnapshot,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_system_resources() -> SystemResourcesSnapshot:
        current = _require_resources_control(control, clock=clock)
        try:
            return current.resources()
        except Exception as exc:
            raise _error(
                status_code=503,
                code="CONTROL_SYSTEM_RESOURCES_UNAVAILABLE",
                message="System resource metrics could not be collected safely.",
                action="Inspect the local Control Plane process and retry the read.",
                retryable=True,
                details={"error_type": type(exc).__name__},
                operation="get_system_resources",
                clock=clock,
            ) from exc

    @router.get(
        "/api/control/v1/observability/overview",
        response_model=UnifiedObservabilitySnapshot,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_unified_observability() -> UnifiedObservabilitySnapshot:
        current = _require_observability_control(observability_control, clock=clock)
        try:
            return current.snapshot()
        except Exception as exc:
            raise _error(
                status_code=503,
                code="CONTROL_OBSERVABILITY_UNAVAILABLE",
                message="Unified observability could not be projected safely.",
                action=(
                    "Inspect authoritative owner read attachments and retry. "
                    "Do not infer missing component health."
                ),
                retryable=True,
                details={"error_type": type(exc).__name__},
                operation="get_unified_observability",
                clock=clock,
            ) from exc

    @router.get(
        "/api/control/v1/system/capacity",
        response_model=SystemCapacitySnapshot,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_system_capacity() -> SystemCapacitySnapshot:
        current = _require_capacity_control(resolved_capacity, clock=clock)
        try:
            return current.capacity()
        except Exception as exc:
            raise _error(
                status_code=503,
                code="CONTROL_SYSTEM_CAPACITY_UNAVAILABLE",
                message="Storage capacity policy could not be projected safely.",
                action=(
                    "Verify the trusted server-side storage-pressure policy attachment and retry "
                    "the read. Do not infer missing capacity thresholds."
                ),
                retryable=True,
                details={"error_type": type(exc).__name__},
                operation="get_system_capacity",
                clock=clock,
            ) from exc

    @router.get(
        "/api/control/v1/system/capacity/artifacts",
        response_model=ExternalArtifactCapacitySnapshot,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_external_artifact_capacity() -> ExternalArtifactCapacitySnapshot:
        current = _require_artifact_capacity_control(
            resolved_artifact_capacity,
            clock=clock,
        )
        try:
            return current.capacity()
        except Exception as exc:
            raise _error(
                status_code=503,
                code="CONTROL_ARTIFACT_CAPACITY_UNAVAILABLE",
                message="External artifact capacity policy could not be projected safely.",
                action=(
                    "Verify the trusted storage-pressure policy attachment and retry. "
                    "Do not infer proactive quota usage or cleanup authority."
                ),
                retryable=True,
                details={"error_type": type(exc).__name__},
                operation="get_external_artifact_capacity",
                clock=clock,
            ) from exc

    return router


def _require_resources_control(
    control: SystemResourcesControlService | None,
    *,
    clock: Clock,
) -> SystemResourcesControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_SYSTEM_RESOURCES_NOT_ATTACHED",
            message="System resource metrics are not attached to this Control Plane process.",
            action="Start the standard local Control Plane server and retry the read.",
            retryable=False,
            details={},
            operation="get_system_resources",
            clock=clock,
        )
    return control


def _require_observability_control(
    control: ObservabilityControlService | None,
    *,
    clock: Clock,
) -> ObservabilityControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_OBSERVABILITY_NOT_ATTACHED",
            message="Unified observability is not attached to this Control Plane process.",
            action=(
                "Install the accepted observability backend and start the standard local "
                "Control Plane composition. Unknown health must remain unavailable."
            ),
            retryable=False,
            details={},
            operation="get_unified_observability",
            clock=clock,
        )
    return control


def _require_capacity_control(
    control: SystemCapacityControlService | None,
    *,
    clock: Clock,
) -> SystemCapacityControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_SYSTEM_CAPACITY_NOT_ATTACHED",
            message="Storage capacity policy is not attached to this Control Plane process.",
            action=(
                "Configure the trusted server-side storage-pressure policy path and retry. "
                "Unknown capacity must remain unknown."
            ),
            retryable=False,
            details={},
            operation="get_system_capacity",
            clock=clock,
        )
    return control


def _require_artifact_capacity_control(
    control: ExternalArtifactCapacityControlService | None,
    *,
    clock: Clock,
) -> ExternalArtifactCapacityControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_ARTIFACT_CAPACITY_NOT_ATTACHED",
            message="External artifact capacity policy is not attached to this Control Plane process.",
            action=(
                "Configure the trusted storage-pressure policy path and retry. "
                "External quota usage must remain unknown without accepted evidence."
            ),
            retryable=False,
            details={},
            operation="get_external_artifact_capacity",
            clock=clock,
        )
    return control


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
    action: str,
    retryable: bool,
    details: dict[str, SafeTechnicalDetail],
    operation: str,
    clock: Clock,
) -> ControlApiError:
    return ControlApiError(
        status_code,
        ApiErrorDetail(
            code=code,
            message=message,
            action=action,
            error_id=f"ctrl_{uuid4().hex[:20]}",
            component=None,
            operation=operation,
            occurred_at=_checked_at(clock),
            retryable=retryable,
            technical_details=details,
        ),
    )


def _checked_at(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value
