from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from modular_control_plane import recovery_profiles
from modular_control_plane.api import (
    OperatorAuthorizer,
    ReaderAuthorizer,
    create_api,
    operator_auth_required,
    reader_auth_required,
)
from modular_control_plane.recovery_profile_api import create_recovery_profile_router
from modular_control_plane.recovery_profiles import (
    BackupPackageProposal,
    ExistingRecoveryPackageEvidence,
    NamedRecoveryProfileService,
    OwnerConsistentCaptureBoundary,
    OwnerReadinessSnapshot,
    RecoveryAuthorizationGrant,
    RecoveryBackupIntent,
    RecoveryOperation,
    RecoveryProfileCapability,
    RecoveryProfileDefinition,
    RecoveryProfileRequest,
    RecoveryProfileService,
    RecoveryResolutionSummary,
    RecoveryResolutionUnavailable,
    RecoveryRestoreIntent,
    RecoveryTargetReference,
    RecoveryVerifyIntent,
    UnconfiguredRecoveryProfileService,
    manifest_digest_matches,
    recovery_intent_fingerprint,
    request_envelope_fingerprint,
)
from modular_control_plane.server import ServerSettings, create_server
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)
READER = "recovery-reader-token"
OPERATOR = "recovery-operator-token"
RECOVERY_BODY_LIMIT = 16 * 1024


class SpyService:
    def __init__(self) -> None:
        self.submitted: list[RecoveryProfileRequest] = []
        self.capability_calls = 0
        self.resolution_calls = 0

    def capability(self) -> RecoveryProfileCapability:
        self.capability_calls += 1
        return RecoveryProfileCapability()

    def resolve(
        self,
        request: RecoveryProfileRequest,
        *,
        actor_id: str,
    ) -> RecoveryResolutionSummary:
        del request, actor_id
        self.resolution_calls += 1
        raise RecoveryResolutionUnavailable


class RecoveryPorts:
    def __init__(self) -> None:
        self.profile: RecoveryProfileDefinition | None = RecoveryProfileDefinition(
            profile_id="paper-profile-1",
            profile_version=2,
            candidate_id="candidate-1",
            source_commit="1" * 40,
            platform_release_id="release-1",
            owner_ids=("owner-a", "owner-b"),
            recovery_target=RecoveryTargetReference(
                target_id="restore-target-1",
                target_generation_id="generation-2",
            ),
            backup_target=RecoveryTargetReference(
                target_id="backup-target-1",
                target_generation_id="backup-generation-3",
            ),
        )
        self.grant: RecoveryAuthorizationGrant | None = None
        self.readiness: OwnerReadinessSnapshot | None = None
        self.proposal: BackupPackageProposal | None = None
        self.evidence: ExistingRecoveryPackageEvidence | None = None
        self.evidence_package_id: str | None = None
        self.authorization_allowed = True
        self.readiness_missing = False
        self.proposal_missing = False
        self.evidence_missing = False
        self.authorization_calls = 0
        self.profile_lookup_calls = 0
        self.readiness_calls = 0
        self.proposal_calls = 0
        self.evidence_calls = 0
        self.readiness_state: Literal["READY", "STALE", "DENIED"] = "READY"
        self.mismatch: str | None = None

    def get_profile(self, profile_id: str) -> RecoveryProfileDefinition | None:
        del profile_id
        self.profile_lookup_calls += 1
        return self.profile

    def authorize(
        self,
        actor_id: str,
        request: RecoveryProfileRequest,
    ) -> RecoveryAuthorizationGrant | None:
        self.authorization_calls += 1
        if not self.authorization_allowed:
            return None
        if self.grant is not None:
            return self.grant
        profile = self.profile
        if profile is None:
            return None
        return RecoveryAuthorizationGrant(
            actor_id=actor_id,
            request_envelope_sha256=request_envelope_fingerprint(request),
            profile_id=request.profile_id,
            profile_version=profile.profile_version,
            operation=request.operation,
            allowed=True,
            evidence_ref="auth-evidence-secret",
        )

    def resolve(self, profile: RecoveryProfileDefinition) -> OwnerReadinessSnapshot | None:
        self.readiness_calls += 1
        if self.readiness_missing:
            return None
        if self.readiness is not None:
            return self.readiness
        owner_ids = profile.owner_ids
        boundary_owner_ids = owner_ids
        readiness_profile_version = profile.profile_version
        readiness_candidate_id = profile.candidate_id
        readiness_source_commit = profile.source_commit
        readiness_release_id = profile.platform_release_id
        if self.mismatch == "partial_owners":
            owner_ids = (profile.owner_ids[0],)
            boundary_owner_ids = owner_ids
        elif self.mismatch == "boundary_owners":
            boundary_owner_ids = (profile.owner_ids[0],)
        elif self.mismatch == "stale_profile_version":
            readiness_profile_version += 1
        elif self.mismatch == "candidate":
            readiness_candidate_id = "other-candidate"
        elif self.mismatch == "source":
            readiness_source_commit = "2" * 40
        elif self.mismatch == "release":
            readiness_release_id = "other-release"
        return OwnerReadinessSnapshot(
            state=self.readiness_state,
            profile_id=profile.profile_id,
            profile_version=readiness_profile_version,
            candidate_id=readiness_candidate_id,
            source_commit=readiness_source_commit,
            platform_release_id=readiness_release_id,
            owner_ids=owner_ids,
            capture_boundary=OwnerConsistentCaptureBoundary(
                boundary_id="capture-boundary-1",
                owner_ids=boundary_owner_ids,
            ),
        )

    def propose(
        self,
        profile: RecoveryProfileDefinition,
        request: RecoveryProfileRequest,
    ) -> BackupPackageProposal | None:
        del request
        self.proposal_calls += 1
        if self.proposal_missing:
            return None
        if self.proposal is not None:
            return self.proposal
        assert profile.backup_target is not None
        return BackupPackageProposal(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            candidate_id=profile.candidate_id,
            source_commit=profile.source_commit,
            platform_release_id=profile.platform_release_id,
            package_id="proposed-package-1",
            destination=profile.backup_target,
        )

    def get_evidence(
        self,
        package_id: str,
        profile: RecoveryProfileDefinition,
        operation: RecoveryOperation,
    ) -> ExistingRecoveryPackageEvidence | None:
        del operation
        self.evidence_calls += 1
        self.evidence_package_id = package_id
        if self.evidence_missing:
            return None
        if self.evidence is not None:
            return self.evidence
        return ExistingRecoveryPackageEvidence(
            state="TRUSTED",
            package_id=package_id,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            candidate_id=profile.candidate_id,
            source_commit=profile.source_commit,
            platform_release_id=profile.platform_release_id,
            owner_ids=profile.owner_ids,
            capture_boundary=OwnerConsistentCaptureBoundary(
                boundary_id="capture-boundary-1",
                owner_ids=profile.owner_ids,
            ),
            expected_manifest_sha256="a" * 64,
            manifest_evidence_ref="manifest-evidence-secret",
            target=RecoveryTargetReference(
                target_id=profile.recovery_target.target_id,
                target_generation_id=profile.recovery_target.target_generation_id,
            ),
        )


def _named_service(ports: RecoveryPorts | None = None) -> NamedRecoveryProfileService:
    selected = ports or RecoveryPorts()
    return NamedRecoveryProfileService(
        profile_catalog=selected,
        authorization_policy=selected,
        owner_readiness=selected,
        backup_proposals=selected,
        package_evidence=selected,
    )


def _app(
    service: RecoveryProfileService | None,
    *,
    authorize_reader: ReaderAuthorizer | None = None,
    authorize_operator: OperatorAuthorizer | None = None,
) -> TestClient:
    app = create_api(
        ControlPlaneService(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    app.include_router(
        create_recovery_profile_router(
            service,
            authorize_reader=authorize_reader,
            authorize_operator=authorize_operator,
            clock=lambda: NOW,
        )
    )
    return TestClient(app)


def _reader(request: Request) -> None:
    if request.headers.get("Authorization") != f"Bearer {READER}":
        raise reader_auth_required(clock=lambda: NOW)


def _operator(request: Request) -> str:
    if request.headers.get("Authorization") != f"Bearer {OPERATOR}":
        raise operator_auth_required(clock=lambda: NOW)
    return "recovery-test-operator"


def _call_asgi(
    client: TestClient,
    *,
    method: str = "POST",
    headers: list[tuple[bytes, bytes]],
    events: list[Message],
) -> tuple[list[Message], int]:
    async def run() -> tuple[list[Message], int]:
        sent: list[Message] = []
        consumed = 0

        async def receive() -> Message:
            nonlocal consumed
            if consumed >= len(events):
                return {"type": "http.disconnect"}
            event = events[consumed]
            consumed += 1
            return event

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/control/v1/recovery/requests",
            "raw_path": b"/api/control/v1/recovery/requests",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        await client.app(scope, receive, send)
        return sent, consumed

    return asyncio.run(run())


def _request_events(*chunks: bytes) -> list[Message]:
    return [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]


def _response_payload(messages: list[Message]) -> tuple[int, dict[str, object]]:
    start = next(message for message in messages if message.get("type") == "http.response.start")
    body_message = next(
        message for message in messages if message.get("type") == "http.response.body"
    )
    status = start.get("status")
    body = body_message.get("body", b"{}")
    assert isinstance(status, int)
    assert isinstance(body, bytes)
    payload = json.loads(body)
    assert isinstance(payload, dict)
    assert all(isinstance(key, str) for key in payload)
    return status, cast(dict[str, object], payload)


def _payload_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def _request(
    *,
    operation: RecoveryOperation = RecoveryOperation.BACKUP,
    request_id: str = "request-1",
    profile_id: str = "paper-profile-1",
    package_id: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "operation": operation.value,
        "request_id": request_id,
        "profile_id": profile_id,
    }
    if package_id is not None:
        result["package_id"] = package_id
    return result


def _backup_intent_fields() -> dict[str, object]:
    owners = ("owner-a", "owner-b")
    return {
        "operation": "backup",
        "actor_id": "recovery-test-operator",
        "request_envelope_sha256": "b" * 64,
        "request_id": "request-1",
        "profile_id": "profile-1",
        "profile_version": 2,
        "candidate_id": "candidate-1",
        "source_commit": "1" * 40,
        "platform_release_id": "release-1",
        "owner_ids": owners,
        "capture_boundary": {"boundary_id": "capture-1", "owner_ids": owners},
        "authorization_evidence_ref": "auth-evidence-1",
        "package_id": "package-new-1",
        "destination": {"target_id": "destination-1", "target_generation_id": "generation-1"},
    }


def _restore_intent_fields(operation: str = "restore") -> dict[str, object]:
    fields = _backup_intent_fields()
    fields["operation"] = operation
    fields.pop("destination")
    fields["package_id"] = "package-existing-1"
    fields["expected_manifest_sha256"] = "a" * 64
    fields["manifest_evidence_ref"] = "manifest-evidence-1"
    fields["target"] = {"target_id": "restore-target-1", "target_generation_id": "generation-2"}
    return fields


def test_authorized_capability_reports_unconfigured_without_readiness_claim() -> None:
    service = SpyService()
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)

    response = client.get(
        "/api/control/v1/recovery/capability",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "schema_version": 1,
        "status": "NOT_CONFIGURED",
        "resolution_enabled": False,
        "profile_catalog_configured": False,
        "owner_readiness_source_configured": False,
        "recovery_authorization_policy_configured": False,
        "backup_package_proposal_source_configured": False,
        "existing_package_evidence_source_configured": False,
        "durable_intent_store_configured": False,
        "recovery_effects_enabled": False,
        "execution_admission_enabled": False,
    }
    assert "readiness" not in payload
    assert service.capability_calls == 1


def test_recovery_specific_authorizers_are_required_and_unauthorized_calls_do_not_reach_service() -> (
    None
):
    service = SpyService()
    unconfigured = _app(service)
    no_reader = unconfigured.get("/api/control/v1/recovery/capability")
    no_operator = unconfigured.post(
        "/api/control/v1/recovery/requests",
        json=_request(),
    )

    assert no_reader.status_code == 503
    assert no_reader.json()["error"]["code"] == "CONTROL_RECOVERY_AUTH_NOT_CONFIGURED"
    assert no_operator.status_code == 503
    assert no_operator.json()["error"]["code"] == "CONTROL_RECOVERY_AUTH_NOT_CONFIGURED"

    unmounted = _app(None, authorize_reader=_reader, authorize_operator=_operator)
    no_service = unmounted.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(),
    )
    assert no_service.status_code == 503
    assert no_service.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"

    denied = _app(service, authorize_reader=_reader, authorize_operator=_operator)
    bad_read = denied.get("/api/control/v1/recovery/capability")
    bad_write = denied.post(
        "/api/control/v1/recovery/requests",
        json=_request(),
    )

    assert bad_read.status_code == 401
    assert bad_read.json()["error"]["code"] == "CONTROL_READER_AUTH_REQUIRED"
    assert bad_write.status_code == 401
    assert bad_write.json()["error"]["code"] == "CONTROL_OPERATOR_AUTH_REQUIRED"
    assert service.capability_calls == 0
    assert service.resolution_calls == 0
    assert service.submitted == []


def test_recovery_post_body_limit_accepts_exact_cap_and_replays_buffer_once() -> None:
    service = SpyService()
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)
    prefix = json.dumps(_request(), separators=(",", ":")).encode()
    body = prefix + b" " * (RECOVERY_BODY_LIMIT - len(prefix))
    split = len(body) // 2
    messages, consumed = _call_asgi(
        client,
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", f"Bearer {OPERATOR}".encode()),
        ],
        events=_request_events(body[:split], body[split:]),
    )

    status, _payload = _response_payload(messages)
    assert status == 503  # parsed and reached the intentionally unconfigured service
    assert consumed == 2
    assert service.resolution_calls == 1


@pytest.mark.parametrize(
    ("headers", "events", "expected_status", "expected_consumed"),
    [
        ([], _request_events(b"x" * (RECOVERY_BODY_LIMIT + 1)), 413, 1),
        (
            [(b"content-length", str(RECOVERY_BODY_LIMIT).encode())],
            _request_events(b"x" * RECOVERY_BODY_LIMIT, b"y", b"unread"),
            413,
            2,
        ),
        (
            [(b"content-length", str(RECOVERY_BODY_LIMIT + 1).encode())],
            _request_events(b"unread"),
            413,
            0,
        ),
        ([(b"content-length", b"9" * 5000)], _request_events(b"unread"), 413, 0),
        ([(b"content-length", b"-1")], _request_events(b"unread"), 400, 0),
        ([(b"content-length", b"nope")], _request_events(b"unread"), 400, 0),
        (
            [(b"content-length", b"1"), (b"content-length", b"1")],
            _request_events(b"unread"),
            400,
            0,
        ),
        (
            [(b"content-length", b"1")],
            _request_events(b"{}"),
            400,
            1,
        ),
    ],
)
def test_recovery_post_body_limit_rejects_oversize_or_bad_lengths_before_authorization(
    headers: list[tuple[bytes, bytes]],
    events: list[Message],
    expected_status: int,
    expected_consumed: int,
) -> None:
    service = SpyService()
    authorization_calls = 0

    def authorize_operator(request: Request) -> str:
        nonlocal authorization_calls
        authorization_calls += 1
        return _operator(request)

    client = _app(service, authorize_reader=_reader, authorize_operator=authorize_operator)
    messages, consumed = _call_asgi(client, headers=headers, events=events)

    status, payload = _response_payload(messages)
    assert status == expected_status
    assert consumed == expected_consumed
    assert authorization_calls == 0
    assert service.resolution_calls == 0
    error = _payload_object(payload["error"])
    assert error["technical_details"] == {}
    assert error["message"] == "The recovery request body is invalid or exceeds the allowed size."
    assert (
        dict(
            next(message for message in messages if message["type"] == "http.response.start")[
                "headers"
            ]
        )[b"cache-control"]
        == b"no-store"
    )


@pytest.mark.parametrize(
    "chunks", [(b'{"secret":', b"x" * RECOVERY_BODY_LIMIT), (b"x" * (RECOVERY_BODY_LIMIT + 1),)]
)
def test_recovery_post_oversize_malformed_body_is_rejected_without_reading_ahead(
    chunks: tuple[bytes, ...],
) -> None:
    service = SpyService()
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)
    messages, consumed = _call_asgi(
        client,
        headers=[(b"authorization", f"Bearer {OPERATOR}".encode())],
        events=_request_events(*chunks, b"unread"),
    )

    status, payload = _response_payload(messages)
    assert status == 413
    assert consumed == len(chunks)
    assert "secret" not in json.dumps(payload)
    assert service.resolution_calls == 0


def test_recovery_post_small_invalid_json_remains_sanitized_422() -> None:
    client = _app(SpyService(), authorize_reader=_reader, authorize_operator=_operator)
    messages, consumed = _call_asgi(
        client,
        headers=[(b"authorization", f"Bearer {OPERATOR}".encode())],
        events=_request_events(b'{"private":"secret"'),
    )
    status, payload = _response_payload(messages)

    assert status == 422
    assert consumed == 1
    assert "secret" not in json.dumps(payload)
    assert _payload_object(payload["error"])["technical_details"] == {}


def test_recovery_post_rejects_unexpected_receive_event_without_reading_ahead() -> None:
    service = SpyService()
    router = create_recovery_profile_router(
        service,
        authorize_reader=_reader,
        authorize_operator=_operator,
        clock=lambda: NOW,
    )
    route = next(
        route for route in router.routes if getattr(route, "path", "").endswith("/requests")
    )
    events: list[Message] = [{"type": "unexpected.event"}, *_request_events(b"{}")]
    consumed = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal consumed
        event = events[consumed]
        consumed += 1
        return event

    async def send(message: Message) -> None:
        messages.append(message)

    async def run() -> None:
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/control/v1/recovery/requests",
            "raw_path": b"/api/control/v1/recovery/requests",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        await route.handle(scope, receive, send)

    asyncio.run(run())
    status, payload = _response_payload(messages)

    assert status == 400
    assert consumed == 1
    assert _payload_object(payload["error"])["technical_details"] == {}
    assert service.resolution_calls == 0


def test_recovery_post_replay_delegates_subsequent_reads_to_original_receive() -> None:
    router = create_recovery_profile_router(
        SpyService(),
        authorize_reader=_reader,
        authorize_operator=_operator,
        clock=lambda: NOW,
    )
    route = next(
        route for route in router.routes if getattr(route, "path", "").endswith("/requests")
    )
    body = json.dumps(_request(), separators=(",", ":")).encode()
    events = _request_events(body) + [{"type": "http.request", "body": b"original-next"}]
    consumed = 0
    observed: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        first = await receive()
        second = await receive()
        observed.extend((first, second))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive() -> Message:
        nonlocal consumed
        event = events[consumed]
        consumed += 1
        return event

    async def send(_message: Message) -> None:
        return None

    async def run() -> None:
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/control/v1/recovery/requests",
            "raw_path": b"/api/control/v1/recovery/requests",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        cast(Route, route).app = cast(ASGIApp, downstream)
        await route.handle(scope, receive, send)

    asyncio.run(run())
    assert consumed == 2
    assert observed == [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.request", "body": b"original-next"},
    ]


@pytest.mark.parametrize("disconnect", [True, False])
def test_recovery_post_disconnect_or_cancellation_never_reaches_downstream(
    disconnect: bool,
) -> None:
    router = create_recovery_profile_router(
        SpyService(),
        authorize_reader=_reader,
        authorize_operator=_operator,
        clock=lambda: NOW,
    )
    route = next(
        route for route in router.routes if getattr(route, "path", "").endswith("/requests")
    )

    async def run() -> list[Message]:
        sent: list[Message] = []

        async def receive() -> Message:
            if disconnect:
                return {"type": "http.disconnect"}
            raise asyncio.CancelledError

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/control/v1/recovery/requests",
            "raw_path": b"/api/control/v1/recovery/requests",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        await route.handle(scope, receive, send)
        return sent

    if disconnect:
        assert asyncio.run(run()) == []
    else:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())


def test_recovery_get_route_is_not_subject_to_post_body_limit() -> None:
    client = _app(SpyService(), authorize_reader=_reader, authorize_operator=_operator)
    messages, consumed = _call_asgi(
        client,
        method="GET",
        headers=[(b"content-length", str(RECOVERY_BODY_LIMIT + 1).encode())],
        events=_request_events(b"ignored"),
    )
    status, _payload = _response_payload(messages)

    assert status == 405
    assert consumed == 0


def test_unknown_profile_or_package_stays_generic_and_mutation_is_disabled() -> None:
    service = SpyService()
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)
    headers = {"Authorization": f"Bearer {OPERATOR}"}

    unknown_profile = client.post(
        "/api/control/v1/recovery/requests",
        headers=headers,
        json=_request(profile_id="unknown-profile"),
    )
    unknown_package = client.post(
        "/api/control/v1/recovery/requests",
        headers=headers,
        json=_request(
            operation=RecoveryOperation.RESTORE,
            request_id="request-2",
            profile_id="unknown-profile",
            package_id="unknown-package",
        ),
    )

    assert unknown_profile.status_code == unknown_package.status_code == 503
    assert unknown_profile.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"
    assert unknown_package.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"
    assert unknown_profile.json()["error"]["message"] == unknown_package.json()["error"]["message"]
    assert service.submitted == []


@pytest.mark.parametrize(
    ("operation", "package_id"),
    [
        (RecoveryOperation.BACKUP, None),
        (RecoveryOperation.VERIFY, "existing-package-1"),
        (RecoveryOperation.RESTORE, "existing-package-1"),
    ],
)
def test_named_service_resolves_each_operation_to_sanitized_summary(
    operation: RecoveryOperation,
    package_id: str | None,
) -> None:
    ports = RecoveryPorts()
    service = _named_service(ports)
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)

    response = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(operation=operation, package_id=package_id),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "RESOLUTION_ONLY"
    assert payload["operation"] == operation.value
    assert len(payload["fingerprint"]) == 64
    assert set(payload) == {"schema_version", "status", "operation", "fingerprint"}
    assert "auth-evidence-secret" not in response.text
    assert "manifest-evidence-secret" not in response.text
    assert "existing-package-1" not in response.text
    assert "owner-a" not in response.text
    if operation is not RecoveryOperation.BACKUP:
        assert ports.evidence_package_id == package_id


def test_named_service_capability_reports_resolution_without_effect_admission() -> None:
    client = _app(_named_service(), authorize_reader=_reader, authorize_operator=_operator)

    response = client.get(
        "/api/control/v1/recovery/capability",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "status": "RESOLUTION_ONLY",
        "resolution_enabled": True,
        "profile_catalog_configured": True,
        "owner_readiness_source_configured": True,
        "recovery_authorization_policy_configured": True,
        "backup_package_proposal_source_configured": True,
        "existing_package_evidence_source_configured": True,
        "durable_intent_store_configured": False,
        "recovery_effects_enabled": False,
        "execution_admission_enabled": False,
    }


@pytest.mark.parametrize(
    ("missing_source", "operation"),
    [
        ("profile", RecoveryOperation.BACKUP),
        ("authorization", RecoveryOperation.BACKUP),
        ("readiness", RecoveryOperation.BACKUP),
        ("stale_readiness", RecoveryOperation.BACKUP),
        ("proposal", RecoveryOperation.BACKUP),
        ("package_evidence", RecoveryOperation.VERIFY),
    ],
)
def test_named_service_fails_closed_when_required_source_is_missing_or_stale(
    missing_source: str,
    operation: RecoveryOperation,
) -> None:
    ports = RecoveryPorts()
    if missing_source == "profile":
        ports.profile = None
    elif missing_source == "authorization":
        ports.authorization_allowed = False
    elif missing_source == "readiness":
        ports.readiness_missing = True
    elif missing_source == "stale_readiness":
        ports.readiness_state = "STALE"
    elif missing_source == "proposal":
        ports.proposal_missing = True
    elif missing_source == "package_evidence":
        ports.evidence_missing = True
    client = _app(_named_service(ports), authorize_reader=_reader, authorize_operator=_operator)

    response = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(
            operation=operation,
            package_id="existing-package-1" if operation is RecoveryOperation.VERIFY else None,
        ),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"
    assert "auth-evidence-secret" not in response.text
    assert "existing-package-1" not in response.text


@pytest.mark.parametrize(
    "mismatch",
    [
        "partial_owners",
        "boundary_owners",
        "stale_profile_version",
        "candidate",
        "source",
        "release",
    ],
)
def test_named_service_rejects_stale_or_partial_owner_readiness(mismatch: str) -> None:
    ports = RecoveryPorts()
    ports.mismatch = mismatch
    client = _app(_named_service(ports), authorize_reader=_reader, authorize_operator=_operator)

    response = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"


def test_profile_without_backup_target_fails_backup_but_keeps_verify_restore_resolution() -> None:
    ports = RecoveryPorts()
    configured_profile = ports.profile
    assert configured_profile is not None
    # Profiles serialized before backup_target existed remain loadable.
    ports.profile = RecoveryProfileDefinition.model_validate(
        configured_profile.model_dump(exclude={"backup_target"})
    )
    assert ports.profile.backup_target is None
    client = _app(_named_service(ports), authorize_reader=_reader, authorize_operator=_operator)
    headers = {"Authorization": f"Bearer {OPERATOR}"}

    backup = client.post(
        "/api/control/v1/recovery/requests",
        headers=headers,
        json=_request(operation=RecoveryOperation.BACKUP),
    )
    assert backup.status_code == 503
    assert backup.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"
    assert "fingerprint" not in backup.json()
    assert ports.proposal_calls == 0

    for operation in (RecoveryOperation.VERIFY, RecoveryOperation.RESTORE):
        response = client.post(
            "/api/control/v1/recovery/requests",
            headers=headers,
            json=_request(
                operation=operation,
                request_id=f"request-{operation.value}",
                package_id="existing-package-1",
            ),
        )
        assert response.status_code == 200
        assert response.json()["operation"] == operation.value
        assert "fingerprint" in response.json()
    assert ports.proposal_calls == 0
    assert ports.evidence_calls == 2


@pytest.mark.parametrize(
    ("target_id", "target_generation_id"),
    [
        ("other-backup-target", "backup-generation-3"),
        ("backup-target-1", "other-backup-generation"),
    ],
)
def test_named_service_rejects_backup_proposal_target_mismatch(
    target_id: str,
    target_generation_id: str,
) -> None:
    ports = RecoveryPorts()
    profile = ports.profile
    assert profile is not None
    ports.proposal = BackupPackageProposal(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        candidate_id=profile.candidate_id,
        source_commit=profile.source_commit,
        platform_release_id=profile.platform_release_id,
        package_id="proposed-package-1",
        destination=RecoveryTargetReference(
            target_id=target_id,
            target_generation_id=target_generation_id,
        ),
    )
    client = _app(_named_service(ports), authorize_reader=_reader, authorize_operator=_operator)

    response = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(operation=RecoveryOperation.BACKUP),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"
    assert "fingerprint" not in response.json()
    assert ports.proposal_calls == 1


def test_named_service_rejects_authorization_and_proposal_binding_mismatches() -> None:
    ports = RecoveryPorts()
    profile = ports.profile
    assert profile is not None
    ports.grant = RecoveryAuthorizationGrant(
        actor_id="another-actor",
        request_envelope_sha256=request_envelope_fingerprint(
            RecoveryProfileRequest.model_validate(_request())
        ),
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        operation=RecoveryOperation.BACKUP,
        allowed=True,
        evidence_ref="auth-evidence-1",
    )
    client = _app(_named_service(ports), authorize_reader=_reader, authorize_operator=_operator)

    unauthorized_binding = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(),
    )
    assert unauthorized_binding.status_code == 503

    ports.grant = None
    assert profile.backup_target is not None
    ports.proposal = BackupPackageProposal(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        candidate_id="another-candidate",
        source_commit=profile.source_commit,
        platform_release_id=profile.platform_release_id,
        package_id="proposed-package-1",
        destination=profile.backup_target,
    )
    proposal_mismatch = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(),
    )
    assert proposal_mismatch.status_code == 503


@pytest.mark.parametrize(
    "mismatch",
    [
        "package",
        "profile",
        "version",
        "candidate",
        "source",
        "release",
        "owners",
        "target",
        "state",
    ],
)
def test_named_service_rejects_existing_package_evidence_binding_mismatches(
    mismatch: str,
) -> None:
    ports = RecoveryPorts()
    profile = ports.profile
    assert profile is not None
    package_id = "existing-package-1"
    fields: dict[str, object] = {
        "state": "TRUSTED",
        "package_id": package_id,
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "candidate_id": profile.candidate_id,
        "source_commit": profile.source_commit,
        "platform_release_id": profile.platform_release_id,
        "owner_ids": profile.owner_ids,
        "capture_boundary": {
            "boundary_id": "capture-boundary-1",
            "owner_ids": profile.owner_ids,
        },
        "expected_manifest_sha256": "a" * 64,
        "manifest_evidence_ref": "manifest-evidence-1",
        "target": {
            "target_id": profile.recovery_target.target_id,
            "target_generation_id": profile.recovery_target.target_generation_id,
        },
    }
    if mismatch == "package":
        fields["package_id"] = "other-package"
    elif mismatch == "profile":
        fields["profile_id"] = "other-profile"
    elif mismatch == "version":
        fields["profile_version"] = profile.profile_version + 1
    elif mismatch == "candidate":
        fields["candidate_id"] = "other-candidate"
    elif mismatch == "source":
        fields["source_commit"] = "2" * 40
    elif mismatch == "release":
        fields["platform_release_id"] = "other-release"
    elif mismatch == "owners":
        fields["owner_ids"] = ("owner-a",)
        fields["capture_boundary"] = {
            "boundary_id": "capture-boundary-1",
            "owner_ids": ("owner-a",),
        }
    elif mismatch == "target":
        fields["target"] = {"target_id": "other-target", "target_generation_id": "generation-2"}
    elif mismatch == "state":
        fields["state"] = "STALE"
    ports.evidence = ExistingRecoveryPackageEvidence.model_validate(fields)
    client = _app(_named_service(ports), authorize_reader=_reader, authorize_operator=_operator)

    response = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(operation=RecoveryOperation.RESTORE, package_id=package_id),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"path": "/Users/private/recovery.sqlite3"},
        {"host": "runner-internal"},
        {"url": "https://internal.invalid"},
        {"command": "restore-anything"},
        {"restore_target": "arbitrary-target"},
        {"expected_manifest_sha256": "a" * 64},
    ],
)
def test_path_authority_and_digest_injection_are_rejected_without_echo(
    forbidden: dict[str, object],
) -> None:
    service = SpyService()
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)
    response = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={**_request(), **forbidden},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTROL_RECOVERY_REQUEST_INVALID"
    assert all(str(value) not in response.text for value in forbidden.values())
    assert service.resolution_calls == 0
    assert service.submitted == []


def test_unsafe_identifier_and_manifest_digest_mismatch_fail_closed() -> None:
    service = SpyService()
    client = _app(service, authorize_reader=_reader, authorize_operator=_operator)
    unsafe_id = client.post(
        "/api/control/v1/recovery/requests",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=_request(profile_id="../private/path"),
    )

    assert unsafe_id.status_code == 422
    assert "../private/path" not in unsafe_id.text
    assert service.resolution_calls == 0
    assert manifest_digest_matches("a" * 64, "a" * 64)
    assert not manifest_digest_matches("a" * 64, "b" * 64)
    assert not manifest_digest_matches("not-a-digest", "b" * 64)
    assert service.submitted == []


def test_request_envelope_fingerprint_is_deterministic_but_not_replay_storage() -> None:
    original = RecoveryProfileRequest.model_validate(_request())
    exact_retry = RecoveryProfileRequest.model_validate(_request())
    conflicting_retry = RecoveryProfileRequest.model_validate(
        _request(profile_id="another-profile")
    )

    assert request_envelope_fingerprint(original) == request_envelope_fingerprint(exact_retry)
    assert request_envelope_fingerprint(original) != request_envelope_fingerprint(conflicting_retry)
    assert not hasattr(UnconfiguredRecoveryProfileService(), "ledger")


def test_request_envelope_fingerprint_canonicalizes_omitted_default_package_id() -> None:
    omitted = RecoveryProfileRequest.model_validate(_request())
    explicit_default = RecoveryProfileRequest.model_validate({**_request(), "package_id": None})

    assert request_envelope_fingerprint(omitted) == request_envelope_fingerprint(explicit_default)


def test_authorization_grant_requires_a_well_formed_envelope_digest() -> None:
    fields: dict[str, object] = {
        "actor_id": "recovery-test-operator",
        "request_envelope_sha256": "b" * 64,
        "profile_id": "paper-profile-1",
        "profile_version": 2,
        "operation": RecoveryOperation.BACKUP,
        "allowed": True,
        "evidence_ref": "auth-evidence-1",
    }

    missing_digest = dict(fields)
    missing_digest.pop("request_envelope_sha256")
    with pytest.raises(ValidationError):
        RecoveryAuthorizationGrant.model_validate(missing_digest)

    malformed_digest = {**fields, "request_envelope_sha256": "not-a-digest"}
    with pytest.raises(ValidationError):
        RecoveryAuthorizationGrant.model_validate(malformed_digest)


@pytest.mark.parametrize(
    ("authorized_payload", "submitted_payload"),
    [
        (
            _request(request_id="prior-request"),
            _request(request_id="current-request"),
        ),
        (
            _request(operation=RecoveryOperation.VERIFY, package_id="package-a"),
            _request(operation=RecoveryOperation.VERIFY, package_id="package-b"),
        ),
    ],
)
def test_authorization_grant_is_bound_to_the_complete_request_envelope(
    authorized_payload: dict[str, object],
    submitted_payload: dict[str, object],
) -> None:
    ports = RecoveryPorts()
    profile = ports.profile
    assert profile is not None
    authorized_request = RecoveryProfileRequest.model_validate(authorized_payload)
    ports.grant = RecoveryAuthorizationGrant(
        actor_id="recovery-test-operator",
        request_envelope_sha256=request_envelope_fingerprint(authorized_request),
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        operation=authorized_request.operation,
        allowed=True,
        evidence_ref="auth-evidence-1",
    )
    service = _named_service(ports)
    submitted_request = RecoveryProfileRequest.model_validate(submitted_payload)

    with pytest.raises(RecoveryResolutionUnavailable):
        service.resolve(submitted_request, actor_id="recovery-test-operator")

    assert ports.authorization_calls == 1
    assert ports.profile_lookup_calls == 0
    assert ports.readiness_calls == 0
    assert ports.evidence_calls == 0


@pytest.mark.parametrize("mismatch", ["actor", "profile", "operation", "profile_version"])
def test_authorization_grant_rejects_actor_profile_operation_and_version_mismatches(
    mismatch: str,
) -> None:
    ports = RecoveryPorts()
    profile = ports.profile
    assert profile is not None
    request = RecoveryProfileRequest.model_validate(_request())
    grant_fields: dict[str, object] = {
        "actor_id": "recovery-test-operator",
        "request_envelope_sha256": request_envelope_fingerprint(request),
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "operation": request.operation,
        "allowed": True,
        "evidence_ref": "auth-evidence-1",
    }
    if mismatch == "actor":
        grant_fields["actor_id"] = "another-operator"
    elif mismatch == "profile":
        grant_fields["profile_id"] = "another-profile"
    elif mismatch == "operation":
        grant_fields["operation"] = RecoveryOperation.VERIFY
    else:
        grant_fields["profile_version"] = profile.profile_version + 1
    ports.grant = RecoveryAuthorizationGrant.model_validate(grant_fields)

    with pytest.raises(RecoveryResolutionUnavailable):
        _named_service(ports).resolve(request, actor_id="recovery-test-operator")

    assert ports.readiness_calls == 0
    assert ports.proposal_calls == 0
    assert ports.evidence_calls == 0
    assert ports.profile_lookup_calls == (1 if mismatch == "profile_version" else 0)


def test_resolution_reauthorizes_identical_retries_and_retains_actor_binding() -> None:
    ports = RecoveryPorts()
    service = _named_service(ports)
    request = RecoveryProfileRequest.model_validate(_request())

    first = service.resolve(request, actor_id="recovery-test-operator")
    retry = service.resolve(request, actor_id="recovery-test-operator")
    other_actor = service.resolve(request, actor_id="another-operator")

    assert first == retry
    assert other_actor.fingerprint != first.fingerprint
    assert ports.authorization_calls == 3


@pytest.mark.parametrize(
    "missing_field",
    [
        "actor_id",
        "request_envelope_sha256",
        "profile_version",
        "candidate_id",
        "source_commit",
        "platform_release_id",
        "owner_ids",
        "capture_boundary",
        "authorization_evidence_ref",
        "package_id",
        "destination",
    ],
)
def test_backup_intent_requires_server_bound_fields(missing_field: str) -> None:
    fields = _backup_intent_fields()
    fields.pop(missing_field)

    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(fields)


def test_public_request_cannot_be_promoted_to_intent_without_server_bindings() -> None:
    public_request = RecoveryProfileRequest.model_validate(_request())

    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(public_request.model_dump(mode="python"))


def test_intent_contract_rejects_coerced_profile_versions() -> None:
    fields = _backup_intent_fields()
    fields["profile_version"] = "2"

    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(fields)


def test_backup_intent_excludes_future_digest_and_restore_requires_existing_evidence() -> None:
    backup_fields = _backup_intent_fields()
    backup_fields["expected_manifest_sha256"] = "a" * 64
    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(backup_fields)

    for model, operation in (
        (RecoveryRestoreIntent, "restore"),
        (RecoveryVerifyIntent, "verify"),
    ):
        for required_field in (
            "package_id",
            "expected_manifest_sha256",
            "manifest_evidence_ref",
            "target",
        ):
            fields = _restore_intent_fields(operation)
            fields.pop(required_field)
            with pytest.raises(ValidationError):
                model.model_validate(fields)

    invalid_digest = _restore_intent_fields()
    invalid_digest["expected_manifest_sha256"] = "not-a-digest"
    with pytest.raises(ValidationError):
        RecoveryRestoreIntent.model_validate(invalid_digest)


def test_recovery_intents_are_frozen_and_owner_sets_are_canonical_and_complete() -> None:
    intent = RecoveryBackupIntent.model_validate(_backup_intent_fields())
    with pytest.raises(ValidationError):
        intent.profile_id = "other-profile"

    for owners in ((), ("owner-a", "owner-a"), ("owner-b", "owner-a")):
        fields = _backup_intent_fields()
        fields["owner_ids"] = owners
        fields["capture_boundary"] = {"boundary_id": "capture-1", "owner_ids": owners}
        with pytest.raises(ValidationError):
            RecoveryBackupIntent.model_validate(fields)

    mismatched_boundary = _backup_intent_fields()
    mismatched_boundary["capture_boundary"] = {
        "boundary_id": "capture-1",
        "owner_ids": ("owner-a",),
    }
    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(mismatched_boundary)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("request_id", "../private/backup"),
        ("source_commit", "/tmp/source"),
        ("authorization_evidence_ref", "file:///private/evidence"),
        ("package_id", "../../package"),
    ],
)
def test_intent_rejects_path_like_opaque_identifiers(field: str, bad_value: str) -> None:
    fields = _backup_intent_fields()
    fields[field] = bad_value

    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(fields)


def test_source_commit_requires_exact_lowercase_git_sha1() -> None:
    for invalid_commit in ("1234abcd", "A" * 40, "g" * 40):
        with pytest.raises(ValidationError):
            RecoveryProfileDefinition.model_validate(
                {
                    "profile_id": "paper-profile-1",
                    "profile_version": 2,
                    "candidate_id": "candidate-1",
                    "source_commit": invalid_commit,
                    "platform_release_id": "release-1",
                    "owner_ids": ("owner-a", "owner-b"),
                    "recovery_target": {
                        "target_id": "restore-target-1",
                        "target_generation_id": "generation-2",
                    },
                }
            )

        intent_fields = _backup_intent_fields()
        intent_fields["source_commit"] = invalid_commit
        with pytest.raises(ValidationError):
            RecoveryBackupIntent.model_validate(intent_fields)


def test_intent_rejects_path_in_owner_and_typed_target_references() -> None:
    unsafe_owner = _backup_intent_fields()
    owners = ("../owner", "owner-b")
    unsafe_owner["owner_ids"] = owners
    unsafe_owner["capture_boundary"] = {"boundary_id": "capture-1", "owner_ids": owners}
    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(unsafe_owner)

    unsafe_destination = _backup_intent_fields()
    unsafe_destination["destination"] = {
        "target_id": "/var/backups",
        "target_generation_id": "generation-1",
    }
    with pytest.raises(ValidationError):
        RecoveryBackupIntent.model_validate(unsafe_destination)

    unsafe_generation = _restore_intent_fields()
    unsafe_generation["target"] = {
        "target_id": "restore-target-1",
        "target_generation_id": "../../generation",
    }
    with pytest.raises(ValidationError):
        RecoveryRestoreIntent.model_validate(unsafe_generation)

    unsafe_evidence = _restore_intent_fields("verify")
    unsafe_evidence["manifest_evidence_ref"] = "/private/manifest-proof"
    with pytest.raises(ValidationError):
        RecoveryVerifyIntent.model_validate(unsafe_evidence)


def test_recovery_intent_fingerprint_is_deterministic_and_binding_sensitive() -> None:
    original = RecoveryBackupIntent.model_validate(_backup_intent_fields())
    exact_copy = RecoveryBackupIntent.model_validate(_backup_intent_fields())
    changed = _backup_intent_fields()
    changed["authorization_evidence_ref"] = "auth-evidence-2"
    changed_binding = RecoveryBackupIntent.model_validate(changed)
    changed_actor = _backup_intent_fields()
    changed_actor["actor_id"] = "another-operator"
    changed_actor_intent = RecoveryBackupIntent.model_validate(changed_actor)
    changed_envelope = _backup_intent_fields()
    changed_envelope["request_envelope_sha256"] = "c" * 64
    changed_envelope_intent = RecoveryBackupIntent.model_validate(changed_envelope)

    assert recovery_intent_fingerprint(original) == recovery_intent_fingerprint(exact_copy)
    assert recovery_intent_fingerprint(original) != recovery_intent_fingerprint(changed_binding)
    assert recovery_intent_fingerprint(original) != recovery_intent_fingerprint(
        changed_actor_intent
    )
    assert recovery_intent_fingerprint(original) != recovery_intent_fingerprint(
        changed_envelope_intent
    )


def test_disabled_service_has_no_direct_377_effect_dependency() -> None:
    service = UnconfiguredRecoveryProfileService()

    assert not hasattr(recovery_profiles, "create_backup_package")
    assert not hasattr(recovery_profiles, "load_backup_manifest")
    assert not hasattr(recovery_profiles, "restore_manifest_entries")
    assert not hasattr(recovery_profiles, "restore_snapshot_entry")
    assert not hasattr(service, "submit")


def test_serving_control_plane_mounts_recovery_routes_fail_closed() -> None:
    app = create_server(
        ServerSettings(
            reader_token=READER,
            operator_token=OPERATOR,
            market_data_url="http://127.0.0.1:8000",
        ),
        service=ControlPlaneService(clock=lambda: NOW),
    )
    route_paths = set(app.openapi()["paths"])

    assert "/api/control/v1/recovery/capability" in route_paths
    assert "/api/control/v1/recovery/requests" in route_paths

    with TestClient(app) as client:
        denied = client.get("/api/control/v1/recovery/capability")
        assert denied.status_code == 401

        capability = client.get(
            "/api/control/v1/recovery/capability",
            headers={"Authorization": f"Bearer {READER}"},
        )
        assert capability.status_code == 503
        assert capability.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"

        request = client.post(
            "/api/control/v1/recovery/requests",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json=_request(),
        )
        assert request.status_code == 503
        assert request.json()["error"]["code"] == "CONTROL_RECOVERY_NOT_CONFIGURED"


def test_serving_control_plane_enforces_recovery_request_body_limit() -> None:
    app = create_server(
        ServerSettings(
            reader_token=READER,
            operator_token=OPERATOR,
            market_data_url="http://127.0.0.1:8000",
        ),
        service=ControlPlaneService(clock=lambda: NOW),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/control/v1/recovery/requests",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            content=b"{" + b" " * RECOVERY_BODY_LIMIT,
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONTROL_RECOVERY_BODY_TOO_LARGE"
