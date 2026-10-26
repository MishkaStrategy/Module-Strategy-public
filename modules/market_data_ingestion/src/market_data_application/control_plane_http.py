from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .control_plane_contracts import TokenReloadRequest, TokenRevocationRequest
from .control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    SseAdmissionLease,
    SseAdmissionLimiter,
    TokenSnapshotState,
    utc_now,
)
from .control_plane_store import (
    AuditContext,
    AuditIntegrityError,
    AuditRecord,
    ControlPlaneStateError,
    SQLiteControlPlaneStore,
)
from .errors import ApiAuthenticationError, ApiAuthorizationError, ApplicationError
from .security import ApiPrincipal, ApiRole

Clock = Callable[[], datetime]


class ApiRateLimitError(ApplicationError):
    def __init__(self, *, scope: str, retry_after_seconds: int) -> None:
        super().__init__(
            "API_RATE_LIMITED",
            "the local control-plane request rate limit was exceeded",
            True,
            "api_admission",
            {
                "scope": scope,
                "retry_after_seconds": retry_after_seconds,
            },
        )


class ApiSecurityAuditUnavailableError(ApplicationError):
    def __init__(self) -> None:
        super().__init__(
            "API_SECURITY_AUDIT_UNAVAILABLE",
            "the security audit store is unavailable",
            True,
            "api_security_audit",
        )


class ApiSecurityStateConflictError(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(
            "API_SECURITY_STATE_CONFLICT",
            message,
            False,
            "api_security_state",
        )


class ApiSecurityTokenNotFoundError(ApplicationError):
    def __init__(self, token_id: str) -> None:
        super().__init__(
            "API_SECURITY_TOKEN_NOT_FOUND",
            "the requested token ID was not found",
            False,
            "api_security_state",
            {"token_id": token_id},
        )


@dataclass(frozen=True, slots=True)
class RequestSecurityContext:
    correlation_id: str
    direct_peer: str

    def __post_init__(self) -> None:
        _validate_bounded_text(self.correlation_id, name="correlation_id", max_length=128)
        _validate_bounded_text(self.direct_peer, name="direct_peer", max_length=255)


class ControlPlaneSecurityController:
    """Audited authentication, bounded admission and atomic token-state mutation."""

    def __init__(
        self,
        *,
        authenticator: ReloadableApiTokenAuthenticator,
        store: SQLiteControlPlaneStore,
        limits: ControlPlaneLimits | None = None,
        peer_limiter: BoundedTokenBucketLimiter | None = None,
        principal_limiter: BoundedTokenBucketLimiter | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.authenticator = authenticator
        self.store = store
        self.limits = limits or ControlPlaneLimits()
        self.peer_limiter = peer_limiter or BoundedTokenBucketLimiter(
            capacity=120,
            refill_per_second=2.0,
            max_entries=4096,
        )
        self.principal_limiter = principal_limiter or BoundedTokenBucketLimiter(
            capacity=60,
            refill_per_second=1.0,
            max_entries=4096,
        )
        self.sse_limiter = SseAdmissionLimiter(self.limits)
        self._clock = clock
        self._mutation_lock = RLock()

    @property
    def state(self) -> TokenSnapshotState:
        return self.authenticator.state

    def authorize(
        self,
        token: str,
        *,
        required_role: ApiRole,
        context: RequestSecurityContext,
    ) -> ApiPrincipal:
        peer_decision = self.peer_limiter.consume(f"peer:{context.direct_peer}")
        if not peer_decision.allowed:
            self._audit_or_fail(
                AuditContext(
                    timestamp=self._clock(),
                    correlation_id=context.correlation_id,
                    direct_peer=context.direct_peer,
                    action="security.rate_limit",
                    outcome="denied",
                    details={
                        "scope": "peer",
                        "retry_after_seconds": peer_decision.retry_after_seconds,
                    },
                )
            )
            raise ApiRateLimitError(
                scope="peer",
                retry_after_seconds=peer_decision.retry_after_seconds or 1,
            )

        try:
            principal = self.authenticator.authenticate(token, required_role=required_role)
        except ApiAuthorizationError as error:
            principal_id = _optional_detail(error.details, "principal_id")
            self._audit_or_fail(
                AuditContext(
                    timestamp=self._clock(),
                    correlation_id=context.correlation_id,
                    direct_peer=context.direct_peer,
                    principal_id=principal_id,
                    token_id=principal_id,
                    action="security.authorization",
                    outcome="denied",
                    details={
                        "required_role": required_role.value,
                        "actual_role": _optional_detail(error.details, "actual_role"),
                    },
                )
            )
            raise
        except (ApiAuthenticationError, TypeError, ValueError):
            self._audit_or_fail(
                AuditContext(
                    timestamp=self._clock(),
                    correlation_id=context.correlation_id,
                    direct_peer=context.direct_peer,
                    action="security.authentication",
                    outcome="denied",
                    details={"required_role": required_role.value},
                )
            )
            raise ApiAuthenticationError() from None

        principal_decision = self.principal_limiter.consume(f"principal:{principal.principal_id}")
        if not principal_decision.allowed:
            self._audit_or_fail(
                AuditContext(
                    timestamp=self._clock(),
                    correlation_id=context.correlation_id,
                    direct_peer=context.direct_peer,
                    principal_id=principal.principal_id,
                    token_id=principal.token_id,
                    action="security.rate_limit",
                    outcome="denied",
                    details={
                        "scope": "principal",
                        "retry_after_seconds": principal_decision.retry_after_seconds,
                    },
                )
            )
            raise ApiRateLimitError(
                scope="principal",
                retry_after_seconds=principal_decision.retry_after_seconds or 1,
            )
        return principal

    def acquire_sse(
        self,
        principal: ApiPrincipal,
        *,
        context: RequestSecurityContext,
    ) -> SseAdmissionLease:
        try:
            return self.sse_limiter.acquire(principal.principal_id, context.direct_peer)
        except RuntimeError:
            self._audit_or_fail(
                AuditContext(
                    timestamp=self._clock(),
                    correlation_id=context.correlation_id,
                    direct_peer=context.direct_peer,
                    principal_id=principal.principal_id,
                    token_id=principal.token_id,
                    action="security.sse_admission",
                    outcome="denied",
                )
            )
            raise ApiRateLimitError(scope="sse", retry_after_seconds=1) from None

    def reload_tokens(
        self,
        request: TokenReloadRequest,
        *,
        principal: ApiPrincipal,
        context: RequestSecurityContext,
    ) -> TokenSnapshotState:
        now = self._clock()
        snapshot = ApiTokenSnapshot(
            generation=request.generation,
            created_at=now,
            records=tuple(token.to_record(default_not_before=now) for token in request.tokens),
        )
        with self._mutation_lock:
            current = self.authenticator.state
            candidate = ReloadableApiTokenAuthenticator(current, clock=self._clock)
            try:
                new_state = candidate.reload(
                    snapshot,
                    overlap_seconds=request.overlap_seconds,
                    now=now,
                )
                self.store.replace_state(
                    new_state,
                    expected_generation=current.current.generation,
                    audit=AuditContext(
                        timestamp=now,
                        correlation_id=context.correlation_id,
                        direct_peer=context.direct_peer,
                        principal_id=principal.principal_id,
                        token_id=principal.token_id,
                        action="security.token_reload",
                        outcome="accepted",
                        details={
                            "generation": request.generation,
                            "record_count": len(snapshot.records),
                            "overlap_seconds": request.overlap_seconds,
                        },
                    ),
                )
            except AuditIntegrityError as error:
                raise ApiSecurityAuditUnavailableError() from error
            except (ControlPlaneStateError, ValueError) as error:
                raise ApiSecurityStateConflictError(str(error)) from error
            self.authenticator.replace_state(new_state)
            return new_state

    def revoke_token(
        self,
        token_id: str,
        request: TokenRevocationRequest,
        *,
        principal: ApiPrincipal,
        context: RequestSecurityContext,
    ) -> TokenSnapshotState:
        now = self._clock()
        with self._mutation_lock:
            current = self.authenticator.state
            target = _find_token_record(current, token_id)
            if target is None:
                raise ApiSecurityTokenNotFoundError(token_id)
            records = list(current.current.records)
            replaced = False
            for index, record in enumerate(records):
                if record.token_id == token_id:
                    records[index] = _revoked_record(record)
                    replaced = True
                    break
            if not replaced:
                records.append(_revoked_record(target))
            try:
                snapshot = ApiTokenSnapshot(
                    generation=request.generation,
                    created_at=now,
                    records=tuple(records),
                )
                new_state = TokenSnapshotState(
                    current=snapshot,
                    previous=current.previous,
                    previous_expires_at=current.previous_expires_at,
                )
                self.store.replace_state(
                    new_state,
                    expected_generation=current.current.generation,
                    audit=AuditContext(
                        timestamp=now,
                        correlation_id=context.correlation_id,
                        direct_peer=context.direct_peer,
                        principal_id=principal.principal_id,
                        token_id=principal.token_id,
                        action="security.token_revoke",
                        outcome="accepted",
                        details={
                            "generation": request.generation,
                            "revoked_token_id": token_id,
                        },
                    ),
                )
            except AuditIntegrityError as error:
                raise ApiSecurityAuditUnavailableError() from error
            except (ControlPlaneStateError, ValueError) as error:
                raise ApiSecurityStateConflictError(str(error)) from error
            self.authenticator.replace_state(new_state)
            return new_state

    def audit_records(self, *, limit: int) -> tuple[AuditRecord, ...]:
        try:
            return self.store.audit_records(limit=limit)
        except AuditIntegrityError as error:
            raise ApiSecurityAuditUnavailableError() from error

    def _audit_or_fail(self, audit: AuditContext) -> None:
        try:
            self.store.append_audit(audit)
        except Exception as error:
            raise ApiSecurityAuditUnavailableError() from error


class _BodyLimitExceeded(RuntimeError):
    pass


class ControlPlaneAdmissionMiddleware:
    """ASGI admission boundary using direct peer data and bounded request buffering."""

    def __init__(self, app: ASGIApp, *, limits: ControlPlaneLimits) -> None:
        self.app = app
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_values = _header_values(scope, b"x-correlation-id")
        generated_correlation_id = f"corr_api_{uuid4().hex[:20]}"
        if len(correlation_values) > 1:
            await _send_admission_error(
                scope,
                receive,
                send,
                status_code=400,
                code="INVALID_CORRELATION_ID",
                message="X-Correlation-ID must appear at most once",
                correlation_id=generated_correlation_id,
            )
            return
        correlation_id = (
            correlation_values[0].decode("utf-8", errors="strict").strip()
            if correlation_values
            else generated_correlation_id
        )
        try:
            _validate_bounded_text(correlation_id, name="correlation_id", max_length=128)
        except (UnicodeError, ValueError):
            await _send_admission_error(
                scope,
                receive,
                send,
                status_code=400,
                code="INVALID_CORRELATION_ID",
                message="X-Correlation-ID is invalid",
                correlation_id=generated_correlation_id,
            )
            return

        direct_peer = _direct_peer(scope)
        state = scope.setdefault("state", {})
        state["control_plane_correlation_id"] = correlation_id
        state["control_plane_direct_peer"] = direct_peer

        header_bytes = sum(len(name) + len(value) + 4 for name, value in scope.get("headers", ()))
        if header_bytes > self.limits.max_header_bytes:
            await _send_admission_error(
                scope,
                receive,
                send,
                status_code=431,
                code="REQUEST_HEADERS_TOO_LARGE",
                message="request headers exceed the local control-plane limit",
                correlation_id=correlation_id,
            )
            return

        content_lengths = _header_values(scope, b"content-length")
        if len(content_lengths) > 1:
            await _send_admission_error(
                scope,
                receive,
                send,
                status_code=400,
                code="INVALID_CONTENT_LENGTH",
                message="Content-Length must appear at most once",
                correlation_id=correlation_id,
            )
            return
        if content_lengths:
            try:
                declared_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeError, ValueError):
                declared_length = -1
            if declared_length < 0:
                await _send_admission_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="INVALID_CONTENT_LENGTH",
                    message="Content-Length is invalid",
                    correlation_id=correlation_id,
                )
                return
            if declared_length > self.limits.max_body_bytes:
                await _send_admission_error(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    code="REQUEST_BODY_TOO_LARGE",
                    message="request body exceeds the local control-plane limit",
                    correlation_id=correlation_id,
                )
                return

        consumed = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                consumed += len(body)
                if consumed > self.limits.max_body_bytes:
                    raise _BodyLimitExceeded
            return message

        async def correlated_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers", ()))
                if not any(name.lower() == b"x-correlation-id" for name, _value in headers):
                    headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, limited_receive, correlated_send)
        except _BodyLimitExceeded:
            if response_started:
                raise
            await _send_admission_error(
                scope,
                receive,
                correlated_send,
                status_code=413,
                code="REQUEST_BODY_TOO_LARGE",
                message="request body exceeds the local control-plane limit",
                correlation_id=correlation_id,
            )


def request_security_context(request: Request) -> RequestSecurityContext:
    correlation_id = getattr(
        request.state,
        "control_plane_correlation_id",
        request.headers.get("X-Correlation-ID") or f"corr_api_{uuid4().hex[:20]}",
    )
    direct_peer = getattr(request.state, "control_plane_direct_peer", _direct_peer(request.scope))
    return RequestSecurityContext(
        correlation_id=correlation_id,
        direct_peer=direct_peer,
    )


def _find_token_record(state: TokenSnapshotState, token_id: str) -> ApiTokenRecord | None:
    for snapshot in (state.current, state.previous):
        if snapshot is None:
            continue
        record = snapshot.by_id().get(token_id)
        if record is not None:
            return record
    return None


def _revoked_record(record: ApiTokenRecord) -> ApiTokenRecord:
    return ApiTokenRecord(
        token_id=record.token_id,
        digest_sha256=record.digest_sha256,
        role=record.role,
        not_before=record.not_before,
        expires_at=record.expires_at,
        revoked=True,
    )


def _optional_detail(details: dict[str, Any], key: str) -> str | None:
    value = details.get(key)
    return value if isinstance(value, str) else None


def _validate_bounded_text(value: str, *, name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{name} must contain between 1 and {max_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for header_name, value in scope.get("headers", ()) if header_name.lower() == name]


def _direct_peer(scope: Scope) -> str:
    client = scope.get("client")
    if client is None:
        return "unknown-local-peer"
    return _validate_bounded_text(str(client[0]), name="direct_peer", max_length=255)


async def _send_admission_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
    correlation_id: str,
) -> None:
    payload = {
        "error": {
            "code": code,
            "message": message,
            "details": {},
            "correlation_id": correlation_id,
        }
    }
    response = JSONResponse(
        status_code=status_code,
        content=json.loads(json.dumps(payload, sort_keys=True)),
        headers={"X-Correlation-ID": correlation_id},
    )
    await response(scope, receive, send)
