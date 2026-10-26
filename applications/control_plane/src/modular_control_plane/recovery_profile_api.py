from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from .api import ControlApiError, OperatorAuthorizer, ReaderAuthorizer
from .api_contracts import ApiError, ApiErrorDetail
from .recovery_profiles import (
    RecoveryProfileCapability,
    RecoveryProfileRequest,
    RecoveryProfileService,
    RecoveryResolutionSummary,
    RecoveryResolutionUnavailable,
)
from .service import Clock, utc_now

_MAX_RECOVERY_REQUEST_BODY_BYTES = 16 * 1024


def create_recovery_profile_router(
    service: RecoveryProfileService | None,
    *,
    authorize_reader: ReaderAuthorizer | None,
    authorize_operator: OperatorAuthorizer | None,
    clock: Clock = utc_now,
) -> APIRouter:
    """Build a fail-closed router with explicit authorization and service injection.

    POST bodies are capped at 16 KiB before JSON parsing, including chunked and
    missing-Content-Length requests. The local disabled service has no #377 effect port.
    """

    router = APIRouter(route_class=_sanitized_recovery_route(clock))

    def require_reader(request: Request) -> None:
        if authorize_reader is None:
            raise _not_configured_error(
                code="CONTROL_RECOVERY_AUTH_NOT_CONFIGURED",
                message="Recovery-specific read authorization is not configured.",
                operation="recovery_capability",
                clock=clock,
            )
        authorize_reader(request)

    def require_operator(request: Request) -> str:
        if authorize_operator is None:
            raise _not_configured_error(
                code="CONTROL_RECOVERY_AUTH_NOT_CONFIGURED",
                message="Recovery-specific operator authorization is not configured.",
                operation="recovery_request",
                clock=clock,
            )
        actor = authorize_operator(request)
        if not actor or actor.strip() != actor or len(actor) > 128:
            raise _not_configured_error(
                code="CONTROL_RECOVERY_AUTH_NOT_CONFIGURED",
                message="Recovery-specific operator identity is unavailable.",
                operation="recovery_request",
                clock=clock,
            )
        return actor

    @router.get(
        "/api/control/v1/recovery/capability",
        response_model=RecoveryProfileCapability,
        dependencies=[Depends(require_reader)],  # noqa: B008
    )
    def get_capability() -> RecoveryProfileCapability:
        current = _require_service(service, operation="recovery_capability", clock=clock)
        return current.capability()

    @router.post(
        "/api/control/v1/recovery/requests",
        response_model=RecoveryResolutionSummary,
    )
    def submit_request(
        request: RecoveryProfileRequest,
        actor: str = Depends(require_operator),  # noqa: B008
    ) -> RecoveryResolutionSummary:
        current = _require_service(service, operation="recovery_request", clock=clock)
        try:
            return current.resolve(request, actor_id=actor)
        except RecoveryResolutionUnavailable as exc:
            raise _not_configured_error(
                code="CONTROL_RECOVERY_NOT_CONFIGURED",
                message="Named recovery resolution is unavailable for this request.",
                operation="recovery_request",
                clock=clock,
            ) from exc

    return router


def _require_service(
    service: RecoveryProfileService | None,
    *,
    operation: str,
    clock: Clock,
) -> RecoveryProfileService:
    if service is None:
        raise _not_configured_error(
            code="CONTROL_RECOVERY_NOT_CONFIGURED",
            message="Named recovery is not configured on this Control Plane.",
            operation=operation,
            clock=clock,
        )
    return service


def _not_configured_error(
    *,
    code: str,
    message: str,
    operation: str,
    clock: Clock,
) -> ControlApiError:
    return ControlApiError(
        status_code=503,
        detail=ApiErrorDetail(
            code=code,
            message=message,
            action="Keep recovery effects disabled until the owner-approved sources are configured.",
            error_id=str(uuid4()),
            component=None,
            operation=operation,
            occurred_at=clock(),
            retryable=False,
            technical_details={},
        ),
    )


def _sanitized_recovery_route(clock: Clock) -> type[APIRoute]:
    class SanitizedRecoveryRoute(APIRoute):
        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get("method") != "POST" or "POST" not in (self.methods or set()):
                await super().handle(scope, receive, send)
                return

            content_lengths = [
                value
                for name, value in scope.get("headers", ())
                if name.lower() == b"content-length"
            ]
            if len(content_lengths) > 1:
                await _body_error_response(
                    scope,
                    receive,
                    send,
                    clock,
                    status_code=400,
                    code="CONTROL_RECOVERY_BODY_INVALID",
                )
                return
            declared_length: int | None = None
            if content_lengths:
                raw_length = content_lengths[0]
                if not raw_length or any(byte < 48 or byte > 57 for byte in raw_length):
                    await _body_error_response(
                        scope,
                        receive,
                        send,
                        clock,
                        status_code=400,
                        code="CONTROL_RECOVERY_BODY_INVALID",
                    )
                    return
                normalized_length = raw_length.lstrip(b"0") or b"0"
                max_length_text = str(_MAX_RECOVERY_REQUEST_BODY_BYTES).encode()
                if len(normalized_length) > len(max_length_text) or (
                    len(normalized_length) == len(max_length_text)
                    and normalized_length > max_length_text
                ):
                    await _body_error_response(
                        scope,
                        receive,
                        send,
                        clock,
                        status_code=413,
                        code="CONTROL_RECOVERY_BODY_TOO_LARGE",
                    )
                    return
                declared_length = int(normalized_length)

            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                if event["type"] != "http.request":
                    await _body_error_response(
                        scope,
                        receive,
                        send,
                        clock,
                        status_code=400,
                        code="CONTROL_RECOVERY_BODY_INVALID",
                    )
                    return
                chunk = event.get("body", b"")
                if len(chunk) > _MAX_RECOVERY_REQUEST_BODY_BYTES - len(body):
                    await _body_error_response(
                        scope,
                        receive,
                        send,
                        clock,
                        status_code=413,
                        code="CONTROL_RECOVERY_BODY_TOO_LARGE",
                    )
                    return
                body.extend(chunk)
                if not event.get("more_body", False):
                    break

            if declared_length is not None and declared_length != len(body):
                await _body_error_response(
                    scope,
                    receive,
                    send,
                    clock,
                    status_code=400,
                    code="CONTROL_RECOVERY_BODY_INVALID",
                )
                return

            replayed = False

            async def replay_receive() -> Message:
                nonlocal replayed
                if replayed:
                    return await receive()
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await super().handle(scope, replay_receive, send)

        def get_route_handler(
            self,
        ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            original_handler = super().get_route_handler()

            async def sanitized_handler(request: Request) -> Response:
                try:
                    return await original_handler(request)
                except RequestValidationError:
                    error = ApiError(
                        error=ApiErrorDetail(
                            code="CONTROL_RECOVERY_REQUEST_INVALID",
                            message="The recovery request is invalid.",
                            action="Use only the documented bounded recovery identifiers.",
                            error_id=str(uuid4()),
                            component=None,
                            operation="recovery_request",
                            occurred_at=clock(),
                            retryable=False,
                            technical_details={},
                        )
                    )
                    return JSONResponse(
                        status_code=422,
                        content=cast(dict[str, object], error.model_dump(mode="json")),
                        headers={"Cache-Control": "no-store"},
                    )

            return sanitized_handler

    return SanitizedRecoveryRoute


async def _body_error_response(
    scope: Scope,
    receive: Receive,
    send: Send,
    clock: Clock,
    *,
    status_code: int,
    code: str,
) -> None:
    error = ApiError(
        error=ApiErrorDetail(
            code=code,
            message="The recovery request body is invalid or exceeds the allowed size.",
            action="Send a bounded recovery request body.",
            error_id=str(uuid4()),
            component=None,
            operation="recovery_request",
            occurred_at=clock(),
            retryable=False,
            technical_details={},
        )
    )
    response = JSONResponse(
        status_code=status_code,
        content=cast(dict[str, object], error.model_dump(mode="json")),
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)
