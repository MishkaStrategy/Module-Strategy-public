from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from .api import ControlApiError, ReaderAuthorizer
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .paper import (
    PaperBlockedScopeList,
    PaperOrderList,
    PaperRuntimeControlService,
    PaperRuntimeSummary,
)
from .paper_positions import PaperPositionsAvailableSnapshot, paper_positions_snapshot_adapter
from .service import Clock, utc_now

_MAX_PAPER_POSITION_ROWS = 500
_MAX_PAPER_POSITION_RESPONSE_BYTES = 256 * 1024
PaperPositionsReader = Callable[[], object]


def create_paper_router(
    control: PaperRuntimeControlService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    positions_reader: PaperPositionsReader | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    def require_positions_reader(request: Request) -> None:
        if authorize_reader is None:
            raise ControlApiError(
                503,
                ApiErrorDetail(
                    code="CONTROL_PAPER_POSITIONS_AUTH_NOT_CONFIGURED",
                    message="Paper positions reader authorization is not configured.",
                    action="Configure the Control Plane reader authorization before reading positions.",
                    error_id=f"ctrl_{uuid4().hex[:20]}",
                    component=ComponentId.PAPER_RUNTIME,
                    operation="read_paper_positions",
                    occurred_at=_checked_at(clock),
                    retryable=False,
                    technical_details={},
                ),
            )
        authorize_reader(request)

    @router.get(
        "/api/control/v1/paper/runtime",
        response_model=PaperRuntimeSummary,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def runtime_summary() -> PaperRuntimeSummary:
        current = _require_control(control, operation="get_paper_runtime", clock=clock)
        try:
            return current.summary()
        except Exception as exc:
            raise _unavailable("get_paper_runtime", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/paper/orders",
        response_model=PaperOrderList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def orders(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> PaperOrderList:
        current = _require_control(control, operation="list_paper_orders", clock=clock)
        try:
            return current.orders(limit=limit, offset=offset)
        except Exception as exc:
            raise _unavailable("list_paper_orders", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/paper/blocked-scopes",
        response_model=PaperBlockedScopeList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def blocked_scopes() -> PaperBlockedScopeList:
        current = _require_control(control, operation="list_paper_blocked_scopes", clock=clock)
        try:
            return current.blocked_scopes()
        except Exception as exc:
            raise _unavailable("list_paper_blocked_scopes", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/paper/positions",
        dependencies=[Depends(require_positions_reader)],  # noqa: B008
    )
    def positions() -> Response:
        if positions_reader is None:
            raise _positions_unavailable("read_paper_positions", clock=clock)
        try:
            raw_snapshot = positions_reader()
            if isinstance(raw_snapshot, BaseModel):
                if getattr(raw_snapshot, "status", None) == "available":
                    _bounded_position_rows(getattr(raw_snapshot, "positions", None))
                # ContractModel instances may have been created with model_construct
                # or altered with model_copy; turn them back into raw data so the
                # adapter revalidates every field and nested fact.
                raw_snapshot = raw_snapshot.model_dump(mode="python")
            if isinstance(raw_snapshot, Mapping) and raw_snapshot.get("status") == "available":
                raw_rows = _bounded_position_rows(raw_snapshot.get("positions"))
                normalized = dict(raw_snapshot)
                normalized["positions"] = [
                    row.model_dump(mode="python") if isinstance(row, BaseModel) else row
                    for row in raw_rows
                ]
                raw_snapshot = normalized
            snapshot = paper_positions_snapshot_adapter.validate_python(raw_snapshot)
            row_count = (
                len(snapshot.positions)
                if isinstance(snapshot, PaperPositionsAvailableSnapshot)
                else 0
            )
            if row_count > _MAX_PAPER_POSITION_ROWS:
                raise ValueError("paper position snapshot exceeds row limit")
            body = paper_positions_snapshot_adapter.dump_json(snapshot)
            if len(body) > _MAX_PAPER_POSITION_RESPONSE_BYTES:
                raise ValueError("paper position snapshot exceeds response limit")
        except Exception as exc:
            raise _positions_unavailable("read_paper_positions", clock=clock) from exc
        return Response(content=body, media_type="application/json")

    return router


def _require_control(
    control: PaperRuntimeControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> PaperRuntimeControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_PAPER_RUNTIME_NOT_ATTACHED",
            message="Paper Runtime is not attached to this Control Plane process.",
            action=(
                "Attach the already-owned live TradingRunner and Paper Execution coordinator; "
                "Control Plane will not create a second runtime."
            ),
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _unavailable(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_PAPER_RUNTIME_UNAVAILABLE",
        message="Paper Runtime owner state could not be projected safely.",
        action="Inspect the owning paper runtime locally and retry the read.",
        operation=operation,
        retryable=True,
        details={"error_type": type(exc).__name__},
        clock=clock,
    )


def _positions_unavailable(operation: str, *, clock: Clock) -> ControlApiError:
    return ControlApiError(
        503,
        ApiErrorDetail(
            code="CONTROL_PAPER_POSITIONS_UNAVAILABLE",
            message="Paper positions are unavailable as a complete owner snapshot.",
            action="Inspect the owning Paper Execution composition locally and retry the read.",
            error_id=f"ctrl_{uuid4().hex[:20]}",
            component=ComponentId.PAPER_RUNTIME,
            operation=operation,
            occurred_at=_checked_at(clock),
            retryable=True,
            technical_details={},
        ),
    )


def _bounded_position_rows(value: object) -> list[object] | tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("paper position rows must be a bounded list or tuple")
    if len(value) > _MAX_PAPER_POSITION_ROWS:
        raise ValueError("paper position snapshot exceeds row limit")
    return value


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
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value
