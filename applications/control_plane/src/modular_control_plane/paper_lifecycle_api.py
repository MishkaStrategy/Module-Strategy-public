from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from .adapters.trading_runner_lifecycle_http import (
    TradingRunnerLifecycleHttpError,
    TradingRunnerLifecycleTransportErrorCode,
)
from .api import (
    ControlApiError,
    OperatorAuthorizer,
    ReaderAuthorizer,
    operator_auth_not_configured,
)
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .paper_lifecycle import (
    PaperLifecycleCapability,
    PaperLifecycleControlService,
    PaperLifecycleMutationRequest,
    PaperLifecycleMutationResult,
)
from .service import Clock, utc_now


def create_paper_lifecycle_router(
    control: PaperLifecycleControlService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    authorize_operator: OperatorAuthorizer | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    def require_operator(request: Request) -> str:
        if authorize_operator is None:
            raise operator_auth_not_configured(clock=clock)
        return authorize_operator(request)

    @router.get(
        "/api/control/v1/paper/lifecycle-capability",
        response_model=PaperLifecycleCapability,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def capability() -> PaperLifecycleCapability:
        return PaperLifecycleCapability(available=control is not None)

    @router.post("/api/control/v1/paper/start", response_model=PaperLifecycleMutationResult)
    def start(
        request: PaperLifecycleMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> PaperLifecycleMutationResult:
        return _mutate(control, "start", request, clock=clock)

    @router.post("/api/control/v1/paper/stop", response_model=PaperLifecycleMutationResult)
    def stop(
        request: PaperLifecycleMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> PaperLifecycleMutationResult:
        return _mutate(control, "stop", request, clock=clock)

    @router.post(
        "/api/control/v1/paper/kill-switch/activate",
        response_model=PaperLifecycleMutationResult,
    )
    def activate_kill_switch(
        request: PaperLifecycleMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> PaperLifecycleMutationResult:
        return _mutate(control, "activate_kill_switch", request, clock=clock)

    @router.post(
        "/api/control/v1/paper/kill-switch/reset",
        response_model=PaperLifecycleMutationResult,
    )
    def reset_kill_switch(
        request: PaperLifecycleMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> PaperLifecycleMutationResult:
        return _mutate(control, "reset_kill_switch", request, clock=clock)

    return router


def _mutate(
    control: PaperLifecycleControlService | None,
    method: str,
    request: PaperLifecycleMutationRequest,
    *,
    clock: Clock,
) -> PaperLifecycleMutationResult:
    operation = f"paper_{method}"
    current = _require_control(control, operation=operation, clock=clock)
    try:
        callback = {
            "start": current.start,
            "stop": current.stop,
            "activate_kill_switch": current.activate_kill_switch,
            "reset_kill_switch": current.reset_kill_switch,
        }[method]
        return callback(request)
    except TradingRunnerLifecycleHttpError as exc:
        raise _owner_error(exc, operation=operation, clock=clock) from exc
    except Exception as exc:
        raise _unexpected(operation, exc=exc, clock=clock) from exc


def _require_control(
    control: PaperLifecycleControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> PaperLifecycleControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_PAPER_LIFECYCLE_NOT_CONFIGURED",
            message="Trading Runner lifecycle owner is not configured on this local Control Plane.",
            action="Configure the authenticated loopback Trading Runner lifecycle transport.",
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _owner_error(
    error: TradingRunnerLifecycleHttpError,
    *,
    operation: str,
    clock: Clock,
) -> ControlApiError:
    status_code, retryable, message, action = _owner_error_shape(error.code)
    details: dict[str, SafeScalar] = {"owner_code": error.code.value}
    if error.status_code is not None:
        details["owner_http_status"] = error.status_code
    return _error(
        status_code=status_code,
        code=f"CONTROL_PAPER_{error.code.value}",
        message=message,
        action=action,
        operation=operation,
        retryable=retryable,
        details=details,
        clock=clock,
    )


def _owner_error_shape(
    code: TradingRunnerLifecycleTransportErrorCode,
) -> tuple[int, bool, str, str]:
    if code is TradingRunnerLifecycleTransportErrorCode.INVALID_REQUEST:
        return 400, False, "The PAPER lifecycle request is invalid.", "Correct the request ID."
    if code is TradingRunnerLifecycleTransportErrorCode.PAPER_ONLY:
        return 400, False, "Trading Runner lifecycle control is PAPER-only.", "Use PAPER mode."
    if code is TradingRunnerLifecycleTransportErrorCode.INVALID_TRANSITION:
        return (
            422,
            False,
            "The requested PAPER lifecycle transition is not allowed by owner state.",
            "Read the current Paper Runtime projection before choosing another action.",
        )
    if code is TradingRunnerLifecycleTransportErrorCode.IDEMPOTENCY_CONFLICT:
        return (
            409,
            False,
            "The lifecycle request ID conflicts with durable owner state.",
            "Reuse a request ID only for the identical lifecycle mutation.",
        )
    if code is TradingRunnerLifecycleTransportErrorCode.RECOVERY_REQUIRED:
        return (
            503,
            True,
            "Trading Runner owner requires lifecycle recovery before the result is known safely.",
            "Re-read authoritative Paper Runtime state and retry the same request ID.",
        )
    if code is TradingRunnerLifecycleTransportErrorCode.UNREACHABLE:
        return (
            503,
            True,
            "Trading Runner lifecycle owner is unavailable.",
            "Re-read Paper Runtime state when the owner is reachable, then retry the same request ID.",
        )
    if code is TradingRunnerLifecycleTransportErrorCode.UNAUTHORIZED:
        return (
            503,
            False,
            "Control Plane could not authenticate to the Trading Runner lifecycle owner.",
            "Repair the server-side lifecycle owner credentials.",
        )
    return (
        503,
        True,
        "Trading Runner lifecycle owner could not complete the requested operation.",
        "Re-read owner state and retry only when the reported condition is safe.",
    )


def _unexpected(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_PAPER_LIFECYCLE_UNAVAILABLE",
        message="PAPER lifecycle control could not reach or validate the owner result.",
        action="Re-read Paper Runtime state and inspect local Trading Runner diagnostics.",
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
            component=ComponentId.PAPER_RUNTIME,
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


__all__ = ["create_paper_lifecycle_router"]
