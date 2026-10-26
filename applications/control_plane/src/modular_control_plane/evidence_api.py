from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from .api import ControlApiError, ReaderAuthorizer
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .evidence import (
    AuditDecisionList,
    AuditEvidenceList,
    EvidenceControlService,
    EvidenceOverview,
    EvidenceSourceNotConfigured,
    EvidenceStateInvalid,
    ResultsEvidenceList,
)
from .service import Clock, utc_now


def create_evidence_router(
    control: EvidenceControlService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    @router.get(
        "/api/control/v1/evidence/overview",
        response_model=EvidenceOverview,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def overview() -> EvidenceOverview:
        current = _require_control(control, operation="get_evidence_overview", clock=clock)
        try:
            return current.overview()
        except EvidenceStateInvalid as exc:
            raise _state_invalid("get_evidence_overview", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("get_evidence_overview", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/evidence/audit/decisions",
        response_model=AuditDecisionList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def audit_decisions(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AuditDecisionList:
        current = _require_control(control, operation="list_audit_decisions", clock=clock)
        try:
            return current.list_audit_decisions(limit=limit, offset=offset)
        except EvidenceSourceNotConfigured as exc:
            raise _source_not_configured(
                "strategy_audit",
                "list_audit_decisions",
                clock=clock,
            ) from exc
        except EvidenceStateInvalid as exc:
            raise _state_invalid("list_audit_decisions", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("list_audit_decisions", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/evidence/audit/evidence",
        response_model=AuditEvidenceList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def audit_evidence(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AuditEvidenceList:
        current = _require_control(control, operation="list_audit_evidence", clock=clock)
        try:
            return current.list_audit_evidence(limit=limit, offset=offset)
        except EvidenceSourceNotConfigured as exc:
            raise _source_not_configured(
                "strategy_audit",
                "list_audit_evidence",
                clock=clock,
            ) from exc
        except EvidenceStateInvalid as exc:
            raise _state_invalid("list_audit_evidence", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("list_audit_evidence", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/evidence/results",
        response_model=ResultsEvidenceList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def results_evidence(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ResultsEvidenceList:
        current = _require_control(control, operation="list_results_evidence", clock=clock)
        try:
            return current.list_results_evidence(limit=limit, offset=offset)
        except EvidenceSourceNotConfigured as exc:
            raise _source_not_configured(
                "results_registry",
                "list_results_evidence",
                clock=clock,
            ) from exc
        except EvidenceStateInvalid as exc:
            raise _state_invalid("list_results_evidence", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("list_results_evidence", exc=exc, clock=clock) from exc

    return router


def _require_control(
    control: EvidenceControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> EvidenceControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_EVIDENCE_NOT_CONFIGURED",
            message="Evidence Control is not configured on this local Control Plane.",
            action="Configure at least one existing evidence database and restart Control Plane.",
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _source_not_configured(source: str, operation: str, *, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_EVIDENCE_SOURCE_NOT_CONFIGURED",
        message="The requested Evidence source is not configured.",
        action="Configure the existing source database or use an available Evidence source.",
        operation=operation,
        retryable=False,
        details={"source": source},
        clock=clock,
    )


def _state_invalid(operation: str, *, clock: Clock) -> ControlApiError:
    return _error(
        status_code=409,
        code="CONTROL_EVIDENCE_STATE_INVALID",
        message="Evidence durable state failed read-only integrity validation.",
        action=(
            "Inspect the owning evidence producer locally; Control Plane will not repair "
            "or migrate it."
        ),
        operation=operation,
        retryable=False,
        details={},
        clock=clock,
    )


def _unexpected(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_EVIDENCE_UNAVAILABLE",
        message="Evidence durable state could not be read.",
        action="Retry the read and inspect the owning evidence producer if the failure persists.",
        operation=operation,
        retryable=True,
        details={"error_type": type(exc).__name__},
        clock=clock,
    )


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
    action: str,
    operation: str,
    retryable: bool,
    details: dict[str, SafeScalar],
    clock: Clock,
) -> ControlApiError:
    return ControlApiError(
        status_code,
        ApiErrorDetail(
            code=code,
            message=message,
            action=action,
            error_id=f"ctrl_{uuid4().hex[:20]}",
            component=ComponentId.EVIDENCE,
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
