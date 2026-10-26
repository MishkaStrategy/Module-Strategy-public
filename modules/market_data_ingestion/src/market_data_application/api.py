from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from market_data_ingestion.enums import DataType, MarketType, RunState

from .artifact_contracts import ArtifactDescriptor, CreateDatasetExportRequest
from .backfill_application import MarketDataApplication
from .backfill_contracts import CreateDatasetBackfillRequest
from .catalog_contracts import (
    DatasetDetails,
    DatasetListResponse,
    ExchangeCapabilitiesResponse,
    ExchangeListResponse,
    InstrumentMetadataResponse,
    MarketListResponse,
)
from .contracts import (
    ApiError,
    ApiErrorDetail,
    CreateDownloadRequest,
    HealthResponse,
    JobCreatedResponse,
    JobDetails,
    JobEvent,
    JobListResponse,
    JobOperation,
)
from .control_contracts import JobCancellationResponse
from .control_plane_contracts import (
    AuditRecordResponse,
    TokenReloadRequest,
    TokenRevocationRequest,
    TokenStateResponse,
)
from .control_plane_http import (
    ApiSecurityAuditUnavailableError,
    ControlPlaneAdmissionMiddleware,
    ControlPlaneSecurityController,
    request_security_context,
)
from .diagnostics import health_response, version_response
from .errors import ApiAuthenticationError, ApplicationError
from .inspection_contracts import DatasetValidationResponse, RepairPlanPreviewResponse
from .repair_confirmation_contracts import (
    CreateRepairConfirmationRequest,
    RepairConfirmationResponse,
    RepairConfirmationStatusResponse,
)
from .repair_execution_contracts import CreateRepairExecutionRequest
from .security import ApiPrincipal, ApiRole, ApiTokenAuthenticator
from .transport_contracts import VersionResponse
from .update_contracts import CreateDatasetUpdateRequest

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
Authorizer = Callable[..., Awaitable[ApiPrincipal]]


def _http_status(error: ApplicationError) -> int:
    if error.code == "API_AUTHENTICATION_REQUIRED":
        return status.HTTP_401_UNAUTHORIZED
    if error.code == "API_ACCESS_DENIED":
        return status.HTTP_403_FORBIDDEN
    if error.code == "API_RATE_LIMITED":
        return status.HTTP_429_TOO_MANY_REQUESTS
    if error.code == "API_SECURITY_AUDIT_UNAVAILABLE":
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if error.code == "API_SECURITY_TOKEN_NOT_FOUND":
        return status.HTTP_404_NOT_FOUND
    if error.code == "API_SECURITY_STATE_CONFLICT":
        return status.HTTP_409_CONFLICT
    if error.code in {
        "JOB_NOT_FOUND",
        "DATASET_NOT_FOUND",
        "EXCHANGE_NOT_FOUND",
        "INSTRUMENT_NOT_FOUND",
        "ARTIFACT_NOT_FOUND",
        "REPAIR_CONFIRMATION_NOT_FOUND",
    }:
        return status.HTTP_404_NOT_FOUND
    if error.code in {
        "IDEMPOTENCY_KEY_CONFLICT",
        "JOB_LEASE_LOST",
        "JOB_ALREADY_RUNNING",
        "JOB_CANCELLATION_CONFLICT",
        "JOB_CANCELLATION_UNAVAILABLE",
        "JOB_BACKEND_IDENTITY_CONFLICT",
        "REPAIR_PREVIEW_STALE",
        "REPAIR_CONFIRMATION_CONFLICT",
        "REPAIR_CONFIRMATION_REJECTED",
        "ARTIFACT_INTEGRITY_FAILED",
        "UPDATE_PARENT_CHANGED",
        "BACKFILL_PARENT_CHANGED",
        "BACKFILL_RANGE_OVERLAP",
    }:
        return status.HTTP_409_CONFLICT
    if error.code in {
        "INVALID_JOB_TRANSITION",
        "JOB_OPERATION_UNSUPPORTED",
        "ARTIFACT_LIMIT_EXCEEDED",
        "MARKET_UNSUPPORTED",
        "UPDATE_RANGE_EMPTY",
        "UPDATE_DATA_TYPE_UNAVAILABLE",
        "UPDATE_ACTOR_INVALID",
        "UPDATE_IDEMPOTENCY_KEY_INVALID",
        "BACKFILL_BASE_TIMEFRAME_UNAVAILABLE",
        "BACKFILL_ACTOR_INVALID",
        "BACKFILL_IDEMPOTENCY_KEY_INVALID",
    }:
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def create_api(
    application: MarketDataApplication,
    *,
    lifespan: Lifespan | None = None,
    authenticator: ApiTokenAuthenticator | None = None,
    control_plane: ControlPlaneSecurityController | None = None,
) -> FastAPI:
    if (authenticator is None) == (control_plane is None):
        raise ValueError("exactly one authentication provider must be configured")
    if authenticator is not None and not isinstance(authenticator, ApiTokenAuthenticator):
        raise TypeError("authenticator must be an ApiTokenAuthenticator")
    if control_plane is not None and not isinstance(control_plane, ControlPlaneSecurityController):
        raise TypeError("control_plane must be a ControlPlaneSecurityController")
    api = FastAPI(
        title="Market Data API",
        version="1.0.0",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        lifespan=lifespan,
    )
    if control_plane is not None:
        api.add_middleware(ControlPlaneAdmissionMiddleware, limits=control_plane.limits)

    bearer = HTTPBearer(auto_error=False)

    def authorize(required_role: ApiRole) -> Authorizer:
        async def dependency(
            request: Request,
            credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
        ) -> ApiPrincipal:
            if control_plane is not None:
                return control_plane.authorize(
                    credentials.credentials if credentials is not None else "",
                    required_role=required_role,
                    context=request_security_context(request),
                )
            if authenticator is None:
                raise ApiAuthenticationError()
            if credentials is None:
                raise ApiAuthenticationError()
            return authenticator.authenticate(
                credentials.credentials,
                required_role=required_role,
            )

        return dependency

    require_reader = authorize(ApiRole.READER)
    require_operator = authorize(ApiRole.OPERATOR)
    require_admin = authorize(ApiRole.ADMIN)

    def hardened_control_plane() -> ControlPlaneSecurityController:
        if control_plane is None:
            raise ApiSecurityAuditUnavailableError()
        return control_plane

    @api.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, error: ApplicationError) -> JSONResponse:
        correlation_id = (
            request_security_context(request).correlation_id
            if control_plane is not None
            else request.headers.get("X-Correlation-ID") or f"corr_api_{uuid4().hex[:20]}"
        )
        payload = ApiError(
            error=ApiErrorDetail(
                code=error.code,
                message=error.message,
                details=error.details,
                correlation_id=correlation_id,
            )
        )
        headers: dict[str, str] = {"X-Correlation-ID": correlation_id}
        if error.code == "API_AUTHENTICATION_REQUIRED":
            headers["WWW-Authenticate"] = "Bearer"
        if error.code == "API_RATE_LIMITED":
            retry_after = error.details.get("retry_after_seconds")
            if isinstance(retry_after, int):
                headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=_http_status(error),
            content=payload.model_dump(mode="json"),
            headers=headers,
        )

    @api.get("/api/v1/version", response_model=VersionResponse)
    def get_version() -> VersionResponse:
        return version_response()

    @api.get(
        "/api/v1/health",
        response_model=HealthResponse,
        dependencies=[Depends(require_reader)],
    )
    def get_health() -> HealthResponse:
        return health_response(application)

    @api.get(
        "/api/v1/exchanges",
        response_model=ExchangeListResponse,
        dependencies=[Depends(require_reader)],
    )
    def list_exchanges() -> ExchangeListResponse:
        return application.list_exchanges()

    @api.get(
        "/api/v1/exchanges/{exchange_id}/markets",
        response_model=MarketListResponse,
        dependencies=[Depends(require_reader)],
    )
    def list_exchange_markets(exchange_id: str) -> MarketListResponse:
        return application.list_markets(exchange_id)

    @api.get(
        "/api/v1/exchanges/{exchange_id}/instruments/{symbol}",
        response_model=InstrumentMetadataResponse,
        dependencies=[Depends(require_reader)],
    )
    def resolve_exchange_instrument(
        exchange_id: str,
        symbol: str,
        market_type: Annotated[MarketType, Query()],
    ) -> InstrumentMetadataResponse:
        return application.resolve_instrument(exchange_id, market_type, symbol)

    @api.get(
        "/api/v1/exchanges/{exchange_id}",
        response_model=ExchangeCapabilitiesResponse,
        dependencies=[Depends(require_reader)],
    )
    def get_exchange(exchange_id: str) -> ExchangeCapabilitiesResponse:
        return application.get_exchange(exchange_id)

    @api.get(
        "/api/v1/datasets",
        response_model=DatasetListResponse,
        dependencies=[Depends(require_reader)],
    )
    def list_datasets(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DatasetListResponse:
        return application.list_datasets(limit=limit, offset=offset)

    @api.get(
        "/api/v1/datasets/{dataset_id}/validation",
        response_model=DatasetValidationResponse,
        dependencies=[Depends(require_reader)],
    )
    def validate_dataset(
        dataset_id: str,
        version: Annotated[int | None, Query(gt=0)] = None,
        deep: Annotated[bool, Query()] = False,
    ) -> DatasetValidationResponse:
        return application.validate_dataset(
            dataset_id,
            version=version,
            deep=deep,
        )

    @api.get(
        "/api/v1/datasets/{dataset_id}/repair-preview",
        response_model=RepairPlanPreviewResponse,
        dependencies=[Depends(require_reader)],
    )
    def preview_dataset_repair(
        dataset_id: str,
        version: Annotated[int | None, Query(gt=0)] = None,
        data_type: Annotated[DataType | None, Query()] = None,
        deep: Annotated[bool, Query()] = False,
    ) -> RepairPlanPreviewResponse:
        return application.preview_repair(
            dataset_id,
            version=version,
            data_type=data_type,
            deep=deep,
        )

    @api.post(
        "/api/v1/datasets/{dataset_id}/repair-confirmations",
        response_model=RepairConfirmationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_repair_confirmation(
        dataset_id: str,
        request: CreateRepairConfirmationRequest,
        principal: ApiPrincipal = Depends(require_operator),  # noqa: B008
    ) -> RepairConfirmationResponse:
        return application.create_repair_confirmation(
            dataset_id,
            request,
            actor_id=principal.principal_id,
        )

    @api.post(
        "/api/v1/datasets/{dataset_id}/repairs",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_repair(
        dataset_id: str,
        request: CreateRepairExecutionRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        correlation_id: Annotated[
            str | None,
            Header(alias="X-Correlation-ID", max_length=128),
        ] = None,
        principal: ApiPrincipal = Depends(require_operator),  # noqa: B008
    ) -> JobCreatedResponse:
        return application.submit_repair(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            actor_id=principal.principal_id,
            correlation_id=correlation_id,
        )

    @api.post(
        "/api/v1/datasets/{dataset_id}/backfills",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_dataset_backfill(
        dataset_id: str,
        request: CreateDatasetBackfillRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        correlation_id: Annotated[
            str | None,
            Header(alias="X-Correlation-ID", max_length=128),
        ] = None,
        principal: ApiPrincipal = Depends(require_operator),  # noqa: B008
    ) -> JobCreatedResponse:
        return application.submit_backfill(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            actor_id=principal.principal_id,
            correlation_id=correlation_id,
        )

    @api.post(
        "/api/v1/datasets/{dataset_id}/updates",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_dataset_update(
        dataset_id: str,
        request: CreateDatasetUpdateRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        correlation_id: Annotated[
            str | None,
            Header(alias="X-Correlation-ID", max_length=128),
        ] = None,
        principal: ApiPrincipal = Depends(require_operator),  # noqa: B008
    ) -> JobCreatedResponse:
        return application.submit_update(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            actor_id=principal.principal_id,
            correlation_id=correlation_id,
        )

    @api.post(
        "/api/v1/datasets/{dataset_id}/exports",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_dataset_export(
        dataset_id: str,
        request: CreateDatasetExportRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        correlation_id: Annotated[
            str | None,
            Header(alias="X-Correlation-ID", max_length=128),
        ] = None,
        principal: ApiPrincipal = Depends(require_operator),  # noqa: B008
    ) -> JobCreatedResponse:
        return application.submit_export(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=principal.principal_id,
        )

    @api.get(
        "/api/v1/repair-confirmations/{confirmation_id}",
        response_model=RepairConfirmationStatusResponse,
        dependencies=[Depends(require_operator)],
    )
    def get_repair_confirmation(
        confirmation_id: str,
    ) -> RepairConfirmationStatusResponse:
        return application.get_repair_confirmation(confirmation_id)

    @api.get(
        "/api/v1/datasets/{dataset_id}",
        response_model=DatasetDetails,
        dependencies=[Depends(require_reader)],
    )
    def get_dataset(
        dataset_id: str,
        version: Annotated[int | None, Query(gt=0)] = None,
    ) -> DatasetDetails:
        return application.get_dataset(dataset_id, version=version)

    @api.get(
        "/api/v1/artifacts/{artifact_id}",
        response_model=ArtifactDescriptor,
        dependencies=[Depends(require_reader)],
    )
    def get_artifact(artifact_id: str) -> ArtifactDescriptor:
        return application.get_artifact(artifact_id)

    @api.get(
        "/api/v1/artifacts/{artifact_id}/content",
        dependencies=[Depends(require_reader)],
    )
    def get_artifact_content(artifact_id: str) -> StreamingResponse:
        content = application.artifact_content(artifact_id)

        def stream() -> Iterator[bytes]:
            with content.path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            stream(),
            media_type=content.descriptor.content_type,
            headers={
                "Content-Disposition": (f'attachment; filename="{content.descriptor.filename}"'),
                "Content-Length": str(content.descriptor.byte_size),
                "ETag": f'"sha256:{content.descriptor.checksum_sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @api.post(
        "/api/v1/downloads",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_download(
        request: CreateDownloadRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        correlation_id: Annotated[
            str | None,
            Header(alias="X-Correlation-ID", max_length=128),
        ] = None,
        actor_id: Annotated[
            str | None,
            Header(alias="X-Actor-ID", max_length=128),
        ] = None,
        principal: ApiPrincipal = Depends(require_operator),  # noqa: B008
    ) -> JobCreatedResponse:
        return application.submit_download(
            request,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=principal.principal_id if control_plane is not None else actor_id,
        )

    @api.get(
        "/api/v1/jobs",
        response_model=JobListResponse,
        dependencies=[Depends(require_reader)],
    )
    def list_jobs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        states: Annotated[list[RunState] | None, Query()] = None,
        operations: Annotated[list[JobOperation] | None, Query()] = None,
    ) -> JobListResponse:
        return application.list_jobs(
            limit=limit,
            offset=offset,
            states=tuple(states or ()),
            operations=tuple(operations or ()),
        )

    @api.get(
        "/api/v1/jobs/{job_id}",
        response_model=JobDetails,
        dependencies=[Depends(require_reader)],
    )
    def get_job(job_id: str) -> JobDetails:
        return application.get_job(job_id)

    @api.post(
        "/api/v1/jobs/{job_id}/cancel",
        response_model=JobCancellationResponse,
        dependencies=[Depends(require_operator)],
    )
    def cancel_job(job_id: str, response: Response) -> JobCancellationResponse:
        cancelled = application.cancel_job(job_id)
        response.status_code = (
            status.HTTP_202_ACCEPTED if cancelled.status == "requested" else status.HTTP_200_OK
        )
        return cancelled

    @api.get(
        "/api/v1/jobs/{job_id}/events",
        response_model=list[JobEvent],
        dependencies=[Depends(require_reader)],
    )
    def get_job_events(
        job_id: str,
        after_event_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> list[JobEvent]:
        return list(
            application.events_after(
                job_id,
                after_event_id=after_event_id,
                limit=limit,
            )
        )

    @api.get(
        "/api/v1/security/tokens",
        response_model=TokenStateResponse,
    )
    def get_security_tokens(
        _principal: ApiPrincipal = Depends(require_admin),  # noqa: B008
    ) -> TokenStateResponse:
        return TokenStateResponse.from_state(hardened_control_plane().state)

    @api.post(
        "/api/v1/security/tokens/reload",
        response_model=TokenStateResponse,
    )
    def reload_security_tokens(
        request: Request,
        payload: TokenReloadRequest,
        principal: ApiPrincipal = Depends(require_admin),  # noqa: B008
    ) -> TokenStateResponse:
        state = hardened_control_plane().reload_tokens(
            payload,
            principal=principal,
            context=request_security_context(request),
        )
        return TokenStateResponse.from_state(state)

    @api.post(
        "/api/v1/security/tokens/{token_id}/revoke",
        response_model=TokenStateResponse,
    )
    def revoke_security_token(
        token_id: str,
        request: Request,
        payload: TokenRevocationRequest,
        principal: ApiPrincipal = Depends(require_admin),  # noqa: B008
    ) -> TokenStateResponse:
        state = hardened_control_plane().revoke_token(
            token_id,
            payload,
            principal=principal,
            context=request_security_context(request),
        )
        return TokenStateResponse.from_state(state)

    @api.get(
        "/api/v1/security/audit",
        response_model=list[AuditRecordResponse],
    )
    def get_security_audit(
        limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
        _principal: ApiPrincipal = Depends(require_admin),  # noqa: B008
    ) -> list[AuditRecordResponse]:
        return [
            AuditRecordResponse.from_record(record)
            for record in hardened_control_plane().audit_records(limit=limit)
        ]

    @api.get("/api/v1/jobs/{job_id}/events/stream")
    def stream_job_events(
        job_id: str,
        request: Request,
        principal: ApiPrincipal = Depends(require_reader),  # noqa: B008
        after_event_id: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[
            int | None,
            Header(alias="Last-Event-ID", ge=0),
        ] = None,
    ) -> StreamingResponse:
        application.get_job(job_id)
        cursor = max(after_event_id, last_event_id or 0)
        lease = (
            control_plane.acquire_sse(principal, context=request_security_context(request))
            if control_plane is not None
            else None
        )

        def stream() -> Iterator[str]:
            try:
                for event in application.events_after(
                    job_id,
                    after_event_id=cursor,
                    limit=1000,
                ):
                    payload = json.dumps(
                        event.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    yield f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {payload}\n\n"
            finally:
                if lease is not None:
                    lease.release()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return api
