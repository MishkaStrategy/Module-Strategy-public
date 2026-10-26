from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from .api import ControlApiError, ReaderAuthorizer
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .research import (
    ResearchControlService,
    ResearchExperimentList,
    ResearchExperimentSummary,
    ResearchNotFound,
    ResearchRunList,
    ResearchRunSummary,
    ResearchStateInvalid,
)
from .service import Clock, utc_now


def create_research_router(
    control: ResearchControlService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    @router.get(
        "/api/control/v1/research/experiments",
        response_model=ResearchExperimentList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def list_experiments(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ResearchExperimentList:
        current = _require_control(control, operation="list_research_experiments", clock=clock)
        try:
            return current.list_experiments(limit=limit, offset=offset)
        except ResearchStateInvalid as exc:
            raise _state_invalid("list_research_experiments", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("list_research_experiments", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/research/experiments/{experiment_id}",
        response_model=ResearchExperimentSummary,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_experiment(experiment_id: str) -> ResearchExperimentSummary:
        current = _require_control(control, operation="get_research_experiment", clock=clock)
        try:
            return current.get_experiment(experiment_id)
        except ResearchNotFound as exc:
            raise _not_found(
                operation="get_research_experiment",
                resource="experiment",
                resource_id=experiment_id,
                clock=clock,
            ) from exc
        except ResearchStateInvalid as exc:
            raise _state_invalid("get_research_experiment", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("get_research_experiment", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/research/experiments/{experiment_id}/runs",
        response_model=ResearchRunList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def list_runs(
        experiment_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ResearchRunList:
        current = _require_control(control, operation="list_research_runs", clock=clock)
        try:
            return current.list_runs(experiment_id, limit=limit, offset=offset)
        except ResearchNotFound as exc:
            raise _not_found(
                operation="list_research_runs",
                resource="experiment",
                resource_id=experiment_id,
                clock=clock,
            ) from exc
        except ResearchStateInvalid as exc:
            raise _state_invalid("list_research_runs", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("list_research_runs", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/research/runs/{run_id}",
        response_model=ResearchRunSummary,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_run(run_id: str) -> ResearchRunSummary:
        current = _require_control(control, operation="get_research_run", clock=clock)
        try:
            return current.get_run(run_id)
        except ResearchNotFound as exc:
            raise _not_found(
                operation="get_research_run",
                resource="run",
                resource_id=run_id,
                clock=clock,
            ) from exc
        except ResearchStateInvalid as exc:
            raise _state_invalid("get_research_run", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("get_research_run", exc=exc, clock=clock) from exc

    return router


def _require_control(
    control: ResearchControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> ResearchControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_RESEARCH_NOT_CONFIGURED",
            message="Research read-only Control is not configured on this local Control Plane.",
            action=(
                "Configure the existing Research Runner state database and restart Control Plane."
            ),
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _not_found(
    *,
    operation: str,
    resource: str,
    resource_id: str,
    clock: Clock,
) -> ControlApiError:
    return _error(
        status_code=404,
        code="CONTROL_RESEARCH_NOT_FOUND",
        message=f"The requested Research {resource} does not exist.",
        action="Choose an existing Research resource from the Research page.",
        operation=operation,
        retryable=False,
        details={f"{resource}_id": resource_id[:256]},
        clock=clock,
    )


def _state_invalid(operation: str, *, clock: Clock) -> ControlApiError:
    return _error(
        status_code=409,
        code="CONTROL_RESEARCH_STATE_INVALID",
        message="Research durable state failed read-only validation.",
        action=(
            "Inspect Research Runner diagnostics locally; Control Plane will not migrate the database."
        ),
        operation=operation,
        retryable=False,
        details={},
        clock=clock,
    )


def _unexpected(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_RESEARCH_UNAVAILABLE",
        message="Research durable state could not be read.",
        action="Retry the read and inspect Research Runner diagnostics if the failure persists.",
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
            component=ComponentId.RESEARCH,
            operation=operation,
            occurred_at=_checked_at(clock),
            retryable=retryable,
            technical_details=details,
        ),
    )


def _checked_at(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value
