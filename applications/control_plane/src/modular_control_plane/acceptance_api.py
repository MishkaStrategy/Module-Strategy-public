from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from .acceptance import (
    AcceptanceControlService,
    AcceptanceCurrent,
    AcceptanceMutationResult,
    AcceptanceOwnerResponseError,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
)
from .api import (
    ControlApiError,
    OperatorAuthorizer,
    ReaderAuthorizer,
    operator_auth_not_configured,
)
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .service import Clock, utc_now


def create_acceptance_router(
    control: AcceptanceControlService | None,
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
        "/api/control/v1/acceptance/current",
        response_model=AcceptanceCurrent,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_current() -> AcceptanceCurrent:
        current = _require_control(control, operation="get_acceptance_current", clock=clock)
        try:
            return current.current()
        except AcceptanceOwnerResponseError as exc:
            raise _owner_error(exc, operation="get_acceptance_current", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("get_acceptance_current", exc=exc, clock=clock) from exc

    @router.post(
        "/api/control/v1/acceptance/start",
        response_model=AcceptanceMutationResult,
    )
    def start_acceptance(
        request: AcceptanceStartRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> AcceptanceMutationResult:
        current = _require_control(control, operation="start_acceptance", clock=clock)
        try:
            return current.start(request)
        except AcceptanceOwnerResponseError as exc:
            raise _owner_error(exc, operation="start_acceptance", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("start_acceptance", exc=exc, clock=clock) from exc

    @router.post(
        "/api/control/v1/acceptance/stop",
        response_model=AcceptanceMutationResult,
    )
    def stop_acceptance(
        request: AcceptanceRunMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> AcceptanceMutationResult:
        return _mutate(control, "stop", request, clock=clock)

    @router.post(
        "/api/control/v1/acceptance/cancel",
        response_model=AcceptanceMutationResult,
    )
    def cancel_acceptance(
        request: AcceptanceRunMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> AcceptanceMutationResult:
        return _mutate(control, "cancel", request, clock=clock)

    @router.post(
        "/api/control/v1/acceptance/retry",
        response_model=AcceptanceMutationResult,
    )
    def retry_acceptance(
        request: AcceptanceRunMutationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> AcceptanceMutationResult:
        return _mutate(control, "retry", request, clock=clock)

    return router


def _mutate(
    control: AcceptanceControlService | None,
    mutation: str,
    request: AcceptanceRunMutationRequest,
    *,
    clock: Clock,
) -> AcceptanceMutationResult:
    operation = f"{mutation}_acceptance"
    current = _require_control(control, operation=operation, clock=clock)
    try:
        callback = {
            "stop": current.stop,
            "cancel": current.cancel,
            "retry": current.retry,
        }[mutation]
        return callback(request)
    except AcceptanceOwnerResponseError as exc:
        raise _owner_error(exc, operation=operation, clock=clock) from exc
    except Exception as exc:
        raise _unexpected(operation, exc=exc, clock=clock) from exc


def _require_control(
    control: AcceptanceControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> AcceptanceControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_ACCEPTANCE_NOT_CONFIGURED",
            message="System Acceptance owner is not configured on this local Control Plane.",
            action="Configure the pinned loopback System Acceptance owner and restart Control Plane.",
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _owner_error(
    error: AcceptanceOwnerResponseError,
    *,
    operation: str,
    clock: Clock,
) -> ControlApiError:
    code = error.code
    status_code, retryable, message, action = _owner_error_shape(code)
    details: dict[str, SafeScalar] = {"owner_code": code}
    if error.request_id is not None:
        details["request_id"] = error.request_id
    if error.acceptance_run_id is not None:
        details["acceptance_run_id"] = error.acceptance_run_id
    return _error(
        status_code=status_code,
        code=f"CONTROL_ACCEPTANCE_{code}",
        message=message,
        action=action,
        operation=operation,
        retryable=retryable,
        details=details,
        clock=clock,
    )


def _owner_error_shape(code: str) -> tuple[int, bool, str, str]:
    if code == "INVALID_REQUEST":
        return (
            400,
            False,
            "The System Acceptance request is invalid.",
            "Correct the request and retry.",
        )
    if code == "PAPER_ONLY":
        return (
            400,
            False,
            "System Acceptance is restricted to PAPER mode.",
            "Use the PAPER-only acceptance owner.",
        )
    if code == "RUN_NOT_FOUND":
        return (
            404,
            False,
            "The requested System Acceptance run does not exist.",
            "Read the current Acceptance run and retry with its identifier.",
        )
    if code in {"IDEMPOTENCY_CONFLICT", "RUN_ALREADY_ACTIVE"}:
        return (
            409,
            False,
            "The System Acceptance mutation conflicts with durable owner state.",
            "Read the current run and reuse a request ID only for the identical mutation.",
        )
    if code == "INVALID_TRANSITION":
        return (
            422,
            False,
            "The requested System Acceptance state transition is not allowed.",
            "Read the current run status and choose a valid action.",
        )
    if code == "RECOVERY_REQUIRED":
        return (
            503,
            True,
            "System Acceptance requires owner recovery before this action can continue.",
            "Keep the same durable run/request identity and retry after owner recovery.",
        )
    if code == "UNAUTHORIZED":
        return (
            503,
            False,
            "Control Plane could not authenticate to the System Acceptance owner.",
            "Repair the server-side downstream owner credential configuration.",
        )
    if code == "OPERATOR_NOT_CONFIGURED":
        return (
            503,
            False,
            "System Acceptance mutation transport is not configured.",
            "Configure the server-side downstream operator credential before enabling mutations.",
        )
    if code == "OWNER_UNAVAILABLE":
        return (
            503,
            True,
            "System Acceptance owner is unavailable.",
            "Retry after the local owner transport is available.",
        )
    if code in {"STORAGE_FAILURE", "OWNER_FAILURE", "PHASE_FAILURE", "PHASE_BLOCKED"}:
        return (
            503,
            code != "PHASE_BLOCKED",
            "System Acceptance owner could not complete the requested operation.",
            "Inspect sanitized owner status/evidence and retry only when the reported condition is resolved.",
        )
    if code == "CANCELLED":
        return (
            409,
            False,
            "The System Acceptance operation was cancelled by authoritative owner state.",
            "Read the current run before issuing another mutation.",
        )
    return (
        503,
        True,
        "System Acceptance owner returned an unsupported safe error.",
        "Retry once; inspect local owner diagnostics if the failure persists.",
    )


def _unexpected(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_ACCEPTANCE_UNAVAILABLE",
        message="System Acceptance Control could not reach or validate the owner projection.",
        action="Retry the request and inspect local System Acceptance diagnostics if it persists.",
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
            component=ComponentId.ACCEPTANCE,
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


__all__ = ["create_acceptance_router"]
