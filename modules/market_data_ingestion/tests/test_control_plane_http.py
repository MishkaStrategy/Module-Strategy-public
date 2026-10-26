from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.types import Message, Receive, Scope, Send

from market_data_application.control_plane_contracts import (
    AuditRecordResponse,
    TokenReloadRequest,
    TokenRevocationRequest,
    TokenStateResponse,
)
from market_data_application.control_plane_http import (
    ApiRateLimitError,
    ApiSecurityAuditUnavailableError,
    ApiSecurityStateConflictError,
    ApiSecurityTokenNotFoundError,
    ControlPlaneAdmissionMiddleware,
    ControlPlaneSecurityController,
    RequestSecurityContext,
)
from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import AuditContext, SQLiteControlPlaneStore
from market_data_application.errors import ApiAuthenticationError, ApiAuthorizationError
from market_data_application.security import ApiPrincipal, ApiRole

NOW = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)
ADMIN_OLD = "http-old-admin-token-0000000000000000000000"
ADMIN_NEW = "http-new-admin-token-0000000000000000000000"
READER = "http-reader-token-0000000000000000000000000"
AUDIT_KEY = b"http-security-audit-key-at-least-thirty-two-bytes"


def _record(token_id: str, token: str, role: ApiRole) -> ApiTokenRecord:
    return ApiTokenRecord.from_plaintext(
        token_id=token_id,
        token=token,
        role=role,
        not_before=NOW,
    )


def _state() -> TokenSnapshotState:
    return TokenSnapshotState(
        current=ApiTokenSnapshot(
            generation=1,
            created_at=NOW,
            records=(
                _record("admin-old", ADMIN_OLD, ApiRole.ADMIN),
                _record("reader", READER, ApiRole.READER),
            ),
        )
    )


def _context(peer: str = "127.0.0.1") -> RequestSecurityContext:
    return RequestSecurityContext(correlation_id="corr-http-test", direct_peer=peer)


def _controller(
    tmp_path: Path,
    *,
    peer_capacity: int = 100,
    principal_capacity: int = 100,
    limits: ControlPlaneLimits | None = None,
) -> tuple[ControlPlaneSecurityController, SQLiteControlPlaneStore]:
    state = _state()
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3", audit_key=AUDIT_KEY)
    store.bootstrap(
        state,
        audit=AuditContext(
            timestamp=NOW,
            correlation_id="corr-bootstrap",
            direct_peer="127.0.0.1",
            principal_id="bootstrap",
            token_id="bootstrap",
            action="security.bootstrap",
            outcome="accepted",
        ),
    )
    controller = ControlPlaneSecurityController(
        authenticator=ReloadableApiTokenAuthenticator(state, clock=lambda: NOW),
        store=store,
        limits=limits,
        peer_limiter=BoundedTokenBucketLimiter(
            capacity=peer_capacity,
            refill_per_second=1.0,
            max_entries=32,
            clock=lambda: 0.0,
        ),
        principal_limiter=BoundedTokenBucketLimiter(
            capacity=principal_capacity,
            refill_per_second=1.0,
            max_entries=32,
            clock=lambda: 0.0,
        ),
        clock=lambda: NOW,
    )
    return controller, store


def test_contracts_mask_plaintext_and_render_non_secret_state(tmp_path: Path) -> None:
    request = TokenReloadRequest.model_validate(
        {
            "generation": 2,
            "overlap_seconds": 10,
            "tokens": [
                {
                    "token_id": "admin-new",
                    "token": ADMIN_NEW,
                    "role": "admin",
                }
            ],
        }
    )
    assert ADMIN_NEW not in repr(request)
    record = request.tokens[0].to_record(default_not_before=NOW)
    state = TokenSnapshotState(
        current=ApiTokenSnapshot(generation=2, created_at=NOW, records=(record,))
    )
    response = TokenStateResponse.from_state(state)
    assert response.generation == 2
    assert response.records[0].token_id == "admin-new"
    assert ADMIN_NEW not in response.model_dump_json()

    _controller_instance, store = _controller(tmp_path)
    audit_response = AuditRecordResponse.from_record(store.audit_records(limit=1)[0])
    assert audit_response.action == "security.bootstrap"


def test_controller_audits_authentication_and_authorization_denials(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)

    with pytest.raises(ApiAuthenticationError):
        controller.authorize("short", required_role=ApiRole.READER, context=_context())
    with pytest.raises(ApiAuthorizationError):
        controller.authorize(READER, required_role=ApiRole.ADMIN, context=_context())

    records = store.audit_records()
    assert [record.action for record in records[-2:]] == [
        "security.authentication",
        "security.authorization",
    ]
    assert all(record.outcome == "denied" for record in records[-2:])
    assert READER not in repr(records)


def test_controller_enforces_peer_and_principal_rate_limits(tmp_path: Path) -> None:
    peer_controller, peer_store = _controller(tmp_path / "peer", peer_capacity=1)
    assert peer_controller.authorize(
        ADMIN_OLD,
        required_role=ApiRole.ADMIN,
        context=_context(),
    )
    with pytest.raises(ApiRateLimitError) as peer_error:
        peer_controller.authorize(
            ADMIN_OLD,
            required_role=ApiRole.ADMIN,
            context=_context(),
        )
    assert peer_error.value.details["scope"] == "peer"
    assert peer_store.audit_records()[-1].details["scope"] == "peer"

    principal_controller, principal_store = _controller(
        tmp_path / "principal",
        principal_capacity=1,
    )
    assert principal_controller.authorize(
        ADMIN_OLD,
        required_role=ApiRole.ADMIN,
        context=_context("127.0.0.1"),
    )
    with pytest.raises(ApiRateLimitError) as principal_error:
        principal_controller.authorize(
            ADMIN_OLD,
            required_role=ApiRole.ADMIN,
            context=_context("::1"),
        )
    assert principal_error.value.details["scope"] == "principal"
    assert principal_store.audit_records()[-1].details["scope"] == "principal"


def test_controller_fails_closed_when_auth_audit_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, store = _controller(tmp_path)

    def fail_audit(_audit: AuditContext) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "append_audit", fail_audit)
    with pytest.raises(ApiSecurityAuditUnavailableError):
        controller.authorize("short", required_role=ApiRole.READER, context=_context())


def test_controller_reload_overlap_revocation_and_persistence(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)
    admin = controller.authorize(
        ADMIN_OLD,
        required_role=ApiRole.ADMIN,
        context=_context(),
    )
    reload_request = TokenReloadRequest.model_validate(
        {
            "generation": 2,
            "overlap_seconds": 30,
            "tokens": [
                {
                    "token_id": "admin-new",
                    "token": ADMIN_NEW,
                    "role": "admin",
                }
            ],
        }
    )

    reloaded = controller.reload_tokens(
        reload_request,
        principal=admin,
        context=_context(),
    )
    assert reloaded.current.generation == 2
    assert (
        controller.authorize(
            ADMIN_OLD,
            required_role=ApiRole.READER,
            context=_context("::1"),
        ).token_generation
        == 1
    )
    new_admin = controller.authorize(
        ADMIN_NEW,
        required_role=ApiRole.ADMIN,
        context=_context("localhost"),
    )

    revoked = controller.revoke_token(
        "admin-old",
        TokenRevocationRequest(generation=3),
        principal=new_admin,
        context=_context(),
    )
    assert revoked.current.generation == 3
    with pytest.raises(ApiAuthenticationError):
        controller.authorize(
            ADMIN_OLD,
            required_role=ApiRole.READER,
            context=_context("127.0.0.2"),
        )
    assert store.load_state() == revoked
    actions = [record.action for record in store.audit_records()]
    assert actions[-3:] == [
        "security.token_reload",
        "security.token_revoke",
        "security.authentication",
    ]

    persisted = b"".join(
        candidate.read_bytes() for candidate in sorted(tmp_path.iterdir()) if candidate.is_file()
    )
    assert ADMIN_NEW.encode() not in persisted


def test_controller_rejects_token_mutation_conflicts_and_missing_id(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    admin = ApiPrincipal(
        principal_id="admin-old",
        role=ApiRole.ADMIN,
        token_id="admin-old",
        token_generation=1,
    )
    with pytest.raises(ApiSecurityStateConflictError):
        controller.reload_tokens(
            TokenReloadRequest.model_validate(
                {
                    "generation": 1,
                    "tokens": [
                        {
                            "token_id": "admin-new",
                            "token": ADMIN_NEW,
                            "role": "admin",
                        }
                    ],
                }
            ),
            principal=admin,
            context=_context(),
        )
    with pytest.raises(ApiSecurityTokenNotFoundError):
        controller.revoke_token(
            "missing",
            TokenRevocationRequest(generation=2),
            principal=admin,
            context=_context(),
        )


def test_controller_sse_admission_is_audited(tmp_path: Path) -> None:
    controller, store = _controller(
        tmp_path,
        limits=ControlPlaneLimits(
            max_sse_global=2,
            max_sse_per_principal=1,
            max_sse_per_peer=2,
        ),
    )
    principal = controller.authorize(
        ADMIN_OLD,
        required_role=ApiRole.READER,
        context=_context(),
    )
    lease = controller.acquire_sse(principal, context=_context())
    with pytest.raises(ApiRateLimitError):
        controller.acquire_sse(principal, context=_context())
    assert store.audit_records()[-1].action == "security.sse_admission"
    lease.release()


async def _downstream(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/octet-stream")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _asgi_call(
    app: Callable[[Scope, Receive, Send], Awaitable[None]],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    chunks: tuple[bytes, ...] = (b"",),
) -> list[Message]:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }
    requests = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    responses: list[Message] = []

    async def receive() -> Message:
        if requests:
            return requests.pop(0)  # type: ignore[return-value]
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        responses.append(message)

    asyncio.run(app(scope, receive, send))
    return responses


def _response_status(messages: list[Message]) -> int:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return int(start["status"])


def _response_headers(messages: list[Message]) -> dict[bytes, bytes]:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return dict(start.get("headers", []))


def test_admission_middleware_echoes_correlation_and_accepts_bounded_body() -> None:
    middleware = ControlPlaneAdmissionMiddleware(
        _downstream,
        limits=ControlPlaneLimits(max_body_bytes=8),
    )
    messages = _asgi_call(
        middleware,
        headers=[(b"x-correlation-id", b"corr-client")],
        chunks=(b"abcd",),
    )
    assert _response_status(messages) == 200
    assert _response_headers(messages)[b"x-correlation-id"] == b"corr-client"


def test_admission_middleware_rejects_invalid_headers_and_declared_size() -> None:
    middleware = ControlPlaneAdmissionMiddleware(
        _downstream,
        limits=ControlPlaneLimits(max_header_bytes=32, max_body_bytes=4),
    )
    duplicate_correlation = _asgi_call(
        middleware,
        headers=[
            (b"x-correlation-id", b"one"),
            (b"x-correlation-id", b"two"),
        ],
    )
    assert _response_status(duplicate_correlation) == 400

    oversized_header = _asgi_call(middleware, headers=[(b"x-large", b"x" * 64)])
    assert _response_status(oversized_header) == 431

    invalid_length = _asgi_call(middleware, headers=[(b"content-length", b"invalid")])
    assert _response_status(invalid_length) == 400

    oversized_length = _asgi_call(middleware, headers=[(b"content-length", b"5")])
    assert _response_status(oversized_length) == 413


def test_admission_middleware_rejects_chunked_body_over_limit() -> None:
    middleware = ControlPlaneAdmissionMiddleware(
        _downstream,
        limits=ControlPlaneLimits(max_body_bytes=4),
    )
    messages = _asgi_call(middleware, chunks=(b"abc", b"de"))
    assert _response_status(messages) == 413
    assert b"corr_api_" in _response_headers(messages)[b"x-correlation-id"]
