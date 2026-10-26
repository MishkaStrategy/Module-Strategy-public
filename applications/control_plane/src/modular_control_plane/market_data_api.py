from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from .adapters.market_data_http import MarketDataHttpCatalogError
from .api import ControlApiError, ReaderAuthorizer
from .api_contracts import ApiErrorDetail
from .contracts import ComponentId, SafeScalar
from .market_data import (
    MarketDataCatalogControlService,
    MarketDatasetList,
    MarketDatasetSummary,
    MarketDatasetValidation,
)
from .service import Clock, utc_now


def create_market_data_catalog_router(
    control: MarketDataCatalogControlService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    clock: Clock = utc_now,
) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> None:
        if authorize_reader is not None:
            authorize_reader(request)

    @router.get(
        "/api/control/v1/market-data/datasets",
        response_model=MarketDatasetList,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def list_datasets(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MarketDatasetList:
        current = _require_control(control, operation="list_market_datasets", clock=clock)
        try:
            return current.list_datasets(limit=limit, offset=offset)
        except MarketDataHttpCatalogError as exc:
            raise _catalog_error("list_market_datasets", exc=exc, clock=clock) from exc
        except Exception as exc:
            raise _unavailable("list_market_datasets", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/market-data/datasets/{dataset_id}",
        response_model=MarketDatasetSummary,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_dataset(
        dataset_id: str,
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> MarketDatasetSummary:
        current = _require_control(control, operation="get_market_dataset", clock=clock)
        try:
            return current.get_dataset(dataset_id, version=version)
        except MarketDataHttpCatalogError as exc:
            raise _catalog_error("get_market_dataset", exc=exc, clock=clock) from exc
        except Exception as exc:
            raise _unavailable("get_market_dataset", exc=exc, clock=clock) from exc

    @router.get(
        "/api/control/v1/market-data/datasets/{dataset_id}/validation",
        response_model=MarketDatasetValidation,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def validate_dataset(
        dataset_id: str,
        version: Annotated[int | None, Query(ge=1)] = None,
        deep: Annotated[bool, Query()] = False,
    ) -> MarketDatasetValidation:
        current = _require_control(control, operation="validate_market_dataset", clock=clock)
        try:
            return current.validate_dataset(dataset_id, version=version, deep=deep)
        except MarketDataHttpCatalogError as exc:
            raise _catalog_error("validate_market_dataset", exc=exc, clock=clock) from exc
        except Exception as exc:
            raise _unavailable("validate_market_dataset", exc=exc, clock=clock) from exc

    return router


def _require_control(
    control: MarketDataCatalogControlService | None,
    *,
    operation: str,
    clock: Clock,
) -> MarketDataCatalogControlService:
    if control is None:
        raise _error(
            status_code=503,
            code="CONTROL_MARKET_DATA_CATALOG_NOT_ATTACHED",
            message="Market Data catalog is not attached to this Control Plane process.",
            action="Configure the local Market Data application URL and retry the read.",
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    return control


def _catalog_error(
    operation: str,
    *,
    exc: MarketDataHttpCatalogError,
    clock: Clock,
) -> ControlApiError:
    if exc.status_code == 404:
        return _error(
            status_code=404,
            code="CONTROL_MARKET_DATASET_NOT_FOUND",
            message="The requested Market Data dataset does not exist.",
            action="Choose an existing immutable dataset from Market Data.",
            operation=operation,
            retryable=False,
            details={},
            clock=clock,
        )
    details: dict[str, SafeScalar] = {}
    if exc.status_code is not None:
        details["upstream_status"] = exc.status_code
    return _error(
        status_code=503,
        code="CONTROL_MARKET_DATA_CATALOG_UNAVAILABLE",
        message="Market Data catalog state could not be projected safely.",
        action="Inspect the local Market Data application health and reader authentication.",
        operation=operation,
        retryable=True,
        details=details,
        clock=clock,
    )


def _unavailable(operation: str, *, exc: Exception, clock: Clock) -> ControlApiError:
    return _error(
        status_code=503,
        code="CONTROL_MARKET_DATA_CATALOG_UNAVAILABLE",
        message="Market Data catalog state could not be projected safely.",
        action="Inspect the local Market Data application and retry the read.",
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
            component=ComponentId.MARKET_DATA,
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
