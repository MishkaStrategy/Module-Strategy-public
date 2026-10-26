from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from .adapters.trading_runner_events_http import (
    TradingRunnerEventsHttpError,
    TradingRunnerEventsTransportErrorCode,
)
from .api import ControlApiError, ReaderAuthorizer
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .operational_events import (
    OperationalEventPage,
    OperationalEventsControlService,
    OperationalEventSeverity,
)
from .service import Clock, utc_now

_ALLOWED_QUERY_KEYS = frozenset({"cursor", "limit", "event_type", "severity"})

EventTypes = Annotated[list[str] | None, Query(max_length=128)]
Severities = Annotated[list[OperationalEventSeverity] | None, Query()]


def create_operational_events_router(
    control: OperationalEventsControlService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    @router.get(
        "/api/control/v1/events/trading-runner",
        response_model=OperationalEventPage,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def query_events(
        request: Request,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        event_type: EventTypes = None,
        severity: Severities = None,
    ) -> OperationalEventPage:
        if set(request.query_params) - _ALLOWED_QUERY_KEYS:
            raise _error(
                status_code=400,
                code="CONTROL_EVENTS_INVALID_REQUEST",
                message="The structured operational-events query contains unsupported fields.",
                action="Use only cursor, limit, event_type and severity query fields.",
                operation="query_operational_events",
                retryable=False,
                details={"source": "trading_runner"},
                clock=clock,
            )
        current = _require_control(control, operation="query_operational_events", clock=clock)
        try:
            return current.query(
                cursor=cursor,
                limit=limit,
                event_types=tuple(event_type or ()),
                severities=tuple(severity or ()),
            )
        except TradingRunnerEventsHttpError as exc:
            raise _owner_error(exc, operation="query_operational_events", clock=clock) from exc
        except Exception as exc:
            raise _unexpected("query_operational_events", exc=exc, clock=clock) from exc

    return router


def _require_control(
    control: OperationalEventsControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> OperationalEventsControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_EVENTS_NOT_CONFIGURED",
            message="Trading Runner structured operational events are not configured.",
            action="Configure the authenticated loopback Trading Runner events transport.",
            operation=operation,
            retryable=False,
            details={"source": "trading_runner"},
            clock=clock,
        )
    return control


def _owner_error(
    error: TradingRunnerEventsHttpError,
    *,
    operation: str,
    clock: Clock,
) -> ControlApiError:
    status_code, retryable, message, action = _owner_error_shape(error.code)
    details: dict[str, SafeScalar] = {
        "source": "trading_runner",
        "owner_code": error.code.value,
    }
    if error.status_code is not None:
        details["owner_http_status"] = error.status_code
    return _error(
        status_code=status_code,
        code=f"CONTROL_EVENTS_{error.code.value}",
        message=message,
        action=action,
        operation=operation,
        retryable=retryable,
        details=details,
        clock=clock,
    )


def _owner_error_shape(
    code: TradingRunnerEventsTransportErrorCode,
) -> tuple[int, bool, str, str]:
    if code is TradingRunnerEventsTransportErrorCode.INVALID_REQUEST:
        return (
            400,
            False,
            "The operational-events query is invalid.",
            "Use only supported bounded event filters and page limits.",
        )
    if code is TradingRunnerEventsTransportErrorCode.INVALID_CURSOR:
        return (
            400,
            False,
            "The operational-events cursor is invalid or no longer belongs to this owner stream.",
            "Restart the query without a cursor.",
        )
    if code is TradingRunnerEventsTransportErrorCode.SOURCE_UNAVAILABLE:
        return (
            503,
            True,
            "Trading Runner structured operational events are temporarily unavailable.",
            "Retry the same bounded query after the owner source recovers.",
        )
    if code is TradingRunnerEventsTransportErrorCode.UNREACHABLE:
        return (
            503,
            True,
            "Trading Runner operational-events owner is unreachable.",
            "Check the local owner transport and retry the read.",
        )
    if code is TradingRunnerEventsTransportErrorCode.UNAUTHORIZED:
        return (
            503,
            False,
            "Control Plane could not authenticate to the Trading Runner events owner.",
            "Repair the server-side reader credential configuration.",
        )
    return (
        503,
        True,
        "Trading Runner operational-events owner returned an incompatible or failed response.",
        "Inspect local owner diagnostics and retry only after the owner is healthy.",
    )


def _unexpected(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_EVENTS_UNAVAILABLE",
        message="Structured operational events could not be projected safely.",
        action="Inspect local Trading Runner owner diagnostics and retry the bounded read.",
        operation=operation,
        retryable=True,
        details={"source": "trading_runner", "error_type": type(exc).__name__},
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


__all__ = ["create_operational_events_router"]
