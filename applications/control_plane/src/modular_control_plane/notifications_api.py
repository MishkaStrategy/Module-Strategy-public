from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from .adapters.signal_bot_control_http import (
    SignalBotControlHttpError,
    SignalBotControlTransportErrorCode,
)
from .api import (
    ControlApiError,
    OperatorAuthorizer,
    ReaderAuthorizer,
    operator_auth_not_configured,
)
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .notifications import (
    NotificationsControlService,
    NotificationStatus,
    TestNotificationRequest,
    TestNotificationResult,
)
from .service import Clock, utc_now


def create_notifications_router(
    control: NotificationsControlService | None,
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
        "/api/control/v1/notifications/status",
        response_model=NotificationStatus,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_status() -> NotificationStatus:
        current = _require_control(control, operation="get_notification_status", clock=clock)
        try:
            return current.status()
        except SignalBotControlHttpError as exc:
            raise _owner_error(exc, operation="get_notification_status", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("get_notification_status", exc=exc, clock=clock) from exc

    @router.post(
        "/api/control/v1/notifications/test",
        response_model=TestNotificationResult,
    )
    def test_notification(
        request: TestNotificationRequest,
        _actor: str = Depends(require_operator),  # noqa: B008
    ) -> TestNotificationResult:
        current = _require_control(control, operation="test_notification", clock=clock)
        try:
            return current.test_notification(request)
        except SignalBotControlHttpError as exc:
            raise _owner_error(exc, operation="test_notification", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("test_notification", exc=exc, clock=clock) from exc

    return router


def _require_control(
    control: NotificationsControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> NotificationsControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_NOTIFICATIONS_NOT_CONFIGURED",
            message="Signal Bot owner control is not configured on this local Control Plane.",
            action="Configure the authenticated loopback Signal Bot owner control transport.",
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _owner_error(
    error: SignalBotControlHttpError,
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
        code=f"CONTROL_NOTIFICATIONS_{error.code.value}",
        message=message,
        action=action,
        operation=operation,
        retryable=retryable,
        details=details,
        clock=clock,
    )


def _owner_error_shape(
    code: SignalBotControlTransportErrorCode,
) -> tuple[int, bool, str, str]:
    if code is SignalBotControlTransportErrorCode.INVALID_REQUEST:
        return 400, False, "The test-notification request is invalid.", "Correct the request ID."
    if code is SignalBotControlTransportErrorCode.NOT_CONFIGURED:
        return (
            503,
            False,
            "Signal Bot notifications are not configured by the owner.",
            "Configure notifications in the Signal Bot owner before testing delivery.",
        )
    if code is SignalBotControlTransportErrorCode.UNREACHABLE:
        return (
            503,
            True,
            "Signal Bot owner could not reach the notification transport.",
            "Retry the same request ID after local/network reachability recovers.",
        )
    if code is SignalBotControlTransportErrorCode.RATE_LIMITED:
        return (
            429,
            True,
            "Signal Bot test notification was rate limited.",
            "Retry the same request ID only after the reported rate limit has cleared.",
        )
    if code is SignalBotControlTransportErrorCode.REJECTED:
        return (
            502,
            False,
            "Signal Bot test notification was rejected by the owner transport.",
            "Inspect the safe owner status before issuing a new request.",
        )
    if code is SignalBotControlTransportErrorCode.IDEMPOTENCY_CONFLICT:
        return (
            409,
            False,
            "The request ID conflicts with existing owner idempotency state.",
            "Use the original identical request or create a new request ID.",
        )
    if code is SignalBotControlTransportErrorCode.RECOVERY_REQUIRED:
        return (
            503,
            False,
            "Signal Bot owner requires recovery before the request can be resolved safely.",
            "Keep the same request identity and resolve owner recovery before retrying.",
        )
    if code is SignalBotControlTransportErrorCode.UNAUTHORIZED:
        return (
            503,
            False,
            "Control Plane could not authenticate to the Signal Bot owner.",
            "Repair the server-side owner credential configuration.",
        )
    return (
        503,
        True,
        "Signal Bot owner could not complete the requested operation.",
        "Inspect safe owner status and retry only when the owner is healthy.",
    )


def _unexpected(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_NOTIFICATIONS_UNAVAILABLE",
        message="Notifications Control could not reach or validate the owner projection.",
        action="Retry once and inspect local Signal Bot owner diagnostics if it persists.",
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
            component=ComponentId.SIGNAL_BOT,
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


__all__ = ["create_notifications_router"]
