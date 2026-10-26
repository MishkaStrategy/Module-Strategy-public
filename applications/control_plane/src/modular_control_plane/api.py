from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from .api_contracts import ApiError, ApiErrorDetail, ApiVersion
from .backtest import (
    BacktestComparison,
    BacktestComparisonRequest,
    BacktestControlService,
    BacktestCreateResponse,
    BacktestIdempotencyConflict,
    BacktestJobRecord,
    BacktestNotFound,
    BacktestResultSummary,
    BacktestResultUnavailable,
    BacktestRunnerError,
    BacktestRunRequest,
)
from .contracts import ComponentHealth, ComponentId, JobStatus, SafeScalar, SystemOverview
from .service import Clock, ControlPlaneService, utc_now
from .strategy import (
    StrategyControlService,
    StrategyRunCorrupted,
    StrategyRunList,
    StrategyRunNotFound,
    StrategyRunSummary,
)

ReaderAuthorizer = Callable[[Request], None]
OperatorAuthorizer = Callable[[Request], str]
BacktestSubmitter = Callable[[str], None]


class ControlApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        detail: ApiErrorDetail,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail.code)
        self.status_code = status_code
        self.detail = detail
        self.headers = dict(headers or {})


def create_api(
    service: ControlPlaneService,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    authorize_operator: OperatorAuthorizer | None = None,
    backtest_control: BacktestControlService | None = None,
    submit_backtest: BacktestSubmitter | None = None,
    strategy_control: StrategyControlService | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Create the versioned local project Control Plane API.

    Read surfaces remain available with optional reader authentication. Mutating Backtest
    operations fail closed unless operator authentication and a server-owned durable executor
    are configured. Backtest requests use server-owned identities only; this API never accepts
    shell commands or filesystem paths. Strategy Factory surfaces are read-only projections of
    existing checkpoint and decision-log state.
    """

    api = FastAPI(
        title="Modular Strategy Control Plane",
        version="1.0.0",
        openapi_url="/api/control/v1/openapi.json",
        docs_url="/api/control/v1/docs",
        redoc_url=None,
    )

    @api.middleware("http")
    async def add_no_store_header(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    def require_operator(request: Request) -> str:
        if authorize_operator is None:
            raise operator_auth_not_configured(clock=clock)
        return authorize_operator(request)

    @api.exception_handler(ControlApiError)
    async def control_api_error_handler(
        _request: Request,
        error: ControlApiError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content=ApiError(error=error.detail).model_dump(mode="json"),
            headers={"Cache-Control": "no-store", **error.headers},
        )

    @api.get(
        "/api/control/v1/version",
        response_model=ApiVersion,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_version() -> ApiVersion:
        return ApiVersion()

    @api.get(
        "/api/control/v1/health",
        response_model=SystemOverview,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_health() -> SystemOverview:
        try:
            return service.overview()
        except Exception as exc:
            raise _unexpected_error(
                component=None,
                operation="system_health",
                exc=exc,
                clock=clock,
            ) from exc

    @api.get(
        "/api/control/v1/components/{component}/health",
        response_model=ComponentHealth,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_component_health(component: str) -> ComponentHealth:
        try:
            component_id = ComponentId(component)
        except ValueError as exc:
            raise _error(
                status_code=404,
                code="CONTROL_COMPONENT_UNKNOWN",
                message="The requested Control Plane component is not known.",
                action="Open System Health to view the available component identities.",
                component=None,
                operation="component_health",
                retryable=False,
                technical_details={"requested_component": component[:128]},
                clock=clock,
            ) from exc
        try:
            return service.health(component_id)
        except Exception as exc:
            raise _unexpected_error(
                component=component_id,
                operation="component_health",
                exc=exc,
                clock=clock,
            ) from exc

    @api.get(
        "/api/control/v1/strategy/runs",
        response_model=StrategyRunList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def list_strategy_runs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> StrategyRunList:
        control = _require_strategy(strategy_control, operation="list_strategy_runs", clock=clock)
        try:
            return control.list_runs(limit=limit, offset=offset)
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.STRATEGY,
                operation="list_strategy_runs",
                exc=exc,
                clock=clock,
            ) from exc

    @api.get(
        "/api/control/v1/strategy/runs/{run_id}",
        response_model=StrategyRunSummary,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_strategy_run(run_id: str) -> StrategyRunSummary:
        control = _require_strategy(strategy_control, operation="get_strategy_run", clock=clock)
        try:
            return control.get_run(run_id)
        except StrategyRunNotFound as exc:
            raise _error(
                status_code=404,
                code="CONTROL_STRATEGY_RUN_NOT_FOUND",
                message="The requested Strategy Factory run does not exist.",
                action="Choose an existing run from the Strategies page.",
                component=ComponentId.STRATEGY,
                operation="get_strategy_run",
                retryable=False,
                technical_details={"run_id": run_id[:128]},
                clock=clock,
            ) from exc
        except StrategyRunCorrupted as exc:
            raise _error(
                status_code=409,
                code="CONTROL_STRATEGY_RUN_INTEGRITY_FAILED",
                message="The requested Strategy Factory run failed checkpoint or decision-chain validation.",
                action="Open Logs & Evidence and inspect the Strategy Factory run locally.",
                component=ComponentId.STRATEGY,
                operation="get_strategy_run",
                retryable=False,
                technical_details={"run_id": run_id[:128]},
                clock=clock,
            ) from exc
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.STRATEGY,
                operation="get_strategy_run",
                exc=exc,
                clock=clock,
            ) from exc

    @api.get(
        "/api/control/v1/backtests",
        response_model=list[BacktestJobRecord],
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def list_backtests(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[BacktestJobRecord, ...]:
        control = _require_backtest(backtest_control, operation="list_backtests", clock=clock)
        try:
            return control.list_jobs(limit=limit, offset=offset)
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.BACKTEST,
                operation="list_backtests",
                exc=exc,
                clock=clock,
            ) from exc

    @api.get(
        "/api/control/v1/backtests/{job_id}",
        response_model=BacktestJobRecord,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_backtest(job_id: str) -> BacktestJobRecord:
        control = _require_backtest(backtest_control, operation="get_backtest", clock=clock)
        try:
            return control.get_job(job_id)
        except BacktestNotFound as exc:
            raise _backtest_not_found(job_id, operation="get_backtest", clock=clock) from exc
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.BACKTEST,
                operation="get_backtest",
                exc=exc,
                clock=clock,
            ) from exc

    @api.get(
        "/api/control/v1/backtests/{job_id}/result",
        response_model=BacktestResultSummary,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_backtest_result(job_id: str) -> BacktestResultSummary:
        control = _require_backtest(
            backtest_control,
            operation="get_backtest_result",
            clock=clock,
        )
        try:
            return control.get_result(job_id)
        except BacktestNotFound as exc:
            raise _backtest_not_found(
                job_id,
                operation="get_backtest_result",
                clock=clock,
            ) from exc
        except BacktestResultUnavailable as exc:
            raise _error(
                status_code=409,
                code="CONTROL_BACKTEST_RESULT_UNAVAILABLE",
                message="The requested Backtest job does not have a verified result.",
                action="Open the Backtest job and inspect its current durable status.",
                component=ComponentId.BACKTEST,
                operation="get_backtest_result",
                retryable=False,
                technical_details={"job_id": job_id[:128]},
                clock=clock,
            ) from exc
        except BacktestRunnerError as exc:
            raise _backtest_runner_error(exc, operation="get_backtest_result", clock=clock) from exc
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.BACKTEST,
                operation="get_backtest_result",
                exc=exc,
                clock=clock,
            ) from exc

    @api.post(
        "/api/control/v1/backtests/compare",
        response_model=BacktestComparison,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def compare_backtests(request: BacktestComparisonRequest) -> BacktestComparison:
        control = _require_backtest(backtest_control, operation="compare_backtests", clock=clock)
        try:
            return control.compare(request)
        except BacktestNotFound as exc:
            raise _error(
                status_code=404,
                code="CONTROL_BACKTEST_NOT_FOUND",
                message="One of the requested durable Backtest jobs does not exist.",
                action="Choose completed Backtest jobs from the Backtests page.",
                component=ComponentId.BACKTEST,
                operation="compare_backtests",
                retryable=False,
                technical_details={},
                clock=clock,
            ) from exc
        except BacktestResultUnavailable as exc:
            raise _error(
                status_code=409,
                code="CONTROL_BACKTEST_RESULT_UNAVAILABLE",
                message="Both Backtest jobs must have verified results before comparison.",
                action="Wait for both runs to succeed, then retry the comparison.",
                component=ComponentId.BACKTEST,
                operation="compare_backtests",
                retryable=False,
                technical_details={},
                clock=clock,
            ) from exc
        except BacktestRunnerError as exc:
            raise _backtest_runner_error(exc, operation="compare_backtests", clock=clock) from exc
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.BACKTEST,
                operation="compare_backtests",
                exc=exc,
                clock=clock,
            ) from exc

    @api.post(
        "/api/control/v1/backtests",
        response_model=BacktestCreateResponse,
        status_code=202,
    )
    def create_backtest(
        request: BacktestRunRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
        actor_id: str = Depends(require_operator),  # noqa: B008
    ) -> BacktestCreateResponse:
        control = _require_backtest(backtest_control, operation="create_backtest", clock=clock)
        submit = _require_backtest_submitter(submit_backtest, clock=clock)
        try:
            response = control.create_run(
                request,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
            )
        except BacktestIdempotencyConflict as exc:
            raise _error(
                status_code=409,
                code="CONTROL_BACKTEST_IDEMPOTENCY_CONFLICT",
                message="The idempotency key is already bound to a different Backtest request.",
                action="Reuse the key only for the identical request or submit a new key.",
                component=ComponentId.BACKTEST,
                operation="create_backtest",
                retryable=False,
                technical_details={},
                clock=clock,
            ) from exc
        except Exception as exc:
            raise _unexpected_error(
                component=ComponentId.BACKTEST,
                operation="create_backtest",
                exc=exc,
                clock=clock,
            ) from exc
        if response.record.job.status is JobStatus.QUEUED:
            try:
                submit(response.record.job.job_id)
            except Exception as exc:
                raise _error(
                    status_code=503,
                    code="CONTROL_BACKTEST_EXECUTOR_UNAVAILABLE",
                    message="The durable Backtest job was created but could not be scheduled locally.",
                    action=(
                        "Retry the identical request with the same idempotency key after the local "
                        "Backtest executor is available."
                    ),
                    component=ComponentId.BACKTEST,
                    operation="create_backtest",
                    retryable=True,
                    technical_details={"error_type": type(exc).__name__},
                    clock=clock,
                ) from exc
        return response

    return api


def reader_auth_required(*, clock: Clock = utc_now) -> ControlApiError:
    return _error(
        status_code=401,
        code="CONTROL_READER_AUTH_REQUIRED",
        message="Control Plane reader authentication is required.",
        action="Provide the configured Control reader token and retry the read.",
        component=None,
        operation="reader_authentication",
        retryable=True,
        technical_details={},
        clock=clock,
        headers={"WWW-Authenticate": "Bearer"},
    )


def operator_auth_required(*, clock: Clock = utc_now) -> ControlApiError:
    return _error(
        status_code=401,
        code="CONTROL_OPERATOR_AUTH_REQUIRED",
        message="Control Plane operator authentication is required.",
        action="Provide the configured Control operator token and retry the mutation.",
        component=None,
        operation="operator_authentication",
        retryable=True,
        technical_details={},
        clock=clock,
        headers={"WWW-Authenticate": "Bearer"},
    )


def operator_auth_not_configured(*, clock: Clock = utc_now) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_OPERATOR_AUTH_NOT_CONFIGURED",
        message="Control Plane mutation authentication is not configured.",
        action="Configure a local Control operator token before enabling mutation endpoints.",
        component=None,
        operation="operator_authentication",
        retryable=False,
        technical_details={},
        clock=clock,
    )


def _require_strategy(
    control: StrategyControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> StrategyControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_STRATEGY_NOT_CONFIGURED",
            message="Strategy Factory read-only Control is not configured on this local Control Plane.",
            action="Configure the existing Strategy Factory state root and restart the local Control Plane.",
            component=ComponentId.STRATEGY,
            operation=operation,
            retryable=False,
            technical_details={},
            clock=clock,
        )
    return control


def _require_backtest(
    control: BacktestControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> BacktestControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_BACKTEST_NOT_CONFIGURED",
            message="Backtest Control is not configured on this local Control Plane.",
            action="Configure the managed Backtest config root and restart the local Control Plane.",
            component=ComponentId.BACKTEST,
            operation=operation,
            retryable=False,
            technical_details={},
            clock=clock,
        )
    return control


def _require_backtest_submitter(
    submitter: BacktestSubmitter | None,
    *,
    clock: Clock,
) -> BacktestSubmitter:
    if submitter is None:
        raise _error(
            status_code=503,
            code="CONTROL_BACKTEST_EXECUTOR_NOT_CONFIGURED",
            message="The local durable Backtest executor is not configured.",
            action="Start Backtest Control through the local Control Plane server composition.",
            component=ComponentId.BACKTEST,
            operation="create_backtest",
            retryable=False,
            technical_details={},
            clock=clock,
        )
    return submitter


def _backtest_not_found(job_id: str, *, operation: str, clock: Clock) -> ControlApiError:
    return _error(
        status_code=404,
        code="CONTROL_BACKTEST_NOT_FOUND",
        message="The requested durable Backtest job does not exist.",
        action="Open the Backtests list and choose an existing job.",
        component=ComponentId.BACKTEST,
        operation=operation,
        retryable=False,
        technical_details={"job_id": job_id[:128]},
        clock=clock,
    )


def _backtest_runner_error(
    exc: BacktestRunnerError,
    *,
    operation: str,
    clock: Clock,
) -> ControlApiError:
    return _error(
        status_code=503 if exc.retryable else 409,
        code=exc.code,
        message=exc.safe_message,
        action=exc.action,
        component=ComponentId.BACKTEST,
        operation=operation,
        retryable=exc.retryable,
        technical_details=dict(exc.details),
        clock=clock,
    )


def _unexpected_error(
    *,
    component: ComponentId | None,
    operation: str,
    exc: Exception,
    clock: Clock,
) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_PLANE_UNAVAILABLE",
        message="The Control Plane could not read the requested system state.",
        action="Retry the request. If it persists, open Logs & Evidence for diagnostics.",
        component=component,
        operation=operation,
        retryable=True,
        technical_details={"error_type": type(exc).__name__},
        clock=clock,
    )


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
    action: str | None,
    component: ComponentId | None,
    operation: str,
    retryable: bool,
    technical_details: dict[str, SafeScalar],
    clock: Clock,
    headers: Mapping[str, str] | None = None,
) -> ControlApiError:
    return ControlApiError(
        status_code,
        ApiErrorDetail(
            code=code,
            message=message,
            action=action,
            error_id=f"ctrl_{uuid4().hex[:20]}",
            component=component,
            operation=operation,
            occurred_at=_checked_at(clock),
            retryable=retryable,
            technical_details=technical_details,
        ),
        headers=headers,
    )


def _checked_at(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value
