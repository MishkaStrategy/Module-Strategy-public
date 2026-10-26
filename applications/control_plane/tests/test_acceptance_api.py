from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from modular_control_plane.acceptance import (
    AcceptanceControlService,
    AcceptanceCurrent,
    AcceptanceMutation,
    AcceptanceMutationResult,
    AcceptanceOwnerResponseError,
    AcceptancePhase,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
    AcceptanceStatus,
)
from modular_control_plane.server import ServerSettings, create_server, settings_from_environment
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 8, 9, 6, 45, tzinfo=UTC)
READER = "browser-reader-token"
OPERATOR = "browser-operator-token"
DOWNSTREAM_READER = "acceptance-reader-token-123"
DOWNSTREAM_OPERATOR = "acceptance-operator-token-123"
DATASET = "b" * 64
STRATEGY = "c" * 64
CONFIG = "d" * 64


class FakeAcceptanceClient:
    def __init__(self) -> None:
        self.operator_available = True
        self.calls: list[tuple[str, str]] = []
        self.error: AcceptanceOwnerResponseError | None = None

    def current(self) -> AcceptanceCurrent:
        self.calls.append(("current", ""))
        if self.error is not None:
            raise self.error
        return AcceptanceCurrent(run=None)

    def start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult:
        return self._mutation(AcceptanceMutation.START, request.request_id)

    def stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._mutation(AcceptanceMutation.STOP, request.request_id)

    def cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._mutation(AcceptanceMutation.CANCEL, request.request_id)

    def retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._mutation(AcceptanceMutation.RETRY, request.request_id)

    def _mutation(self, mutation: AcceptanceMutation, request_id: str) -> AcceptanceMutationResult:
        self.calls.append((mutation.value.lower(), request_id))
        if self.error is not None:
            raise self.error
        return AcceptanceMutationResult(
            request_id=request_id,
            operator="control-plane:local-operator",
            mutation=mutation,
            acceptance_run_id="acceptance-v1",
            status=AcceptanceStatus.RUNNING,
            phase=AcceptancePhase.SOAK_2H,
            completed_at=NOW,
            replayed=False,
        )


def application(fake: FakeAcceptanceClient, *, operator_token: str | None = OPERATOR) -> TestClient:
    settings = ServerSettings(reader_token=READER, operator_token=operator_token)
    control = AcceptanceControlService(fake, clock=lambda: NOW)
    return TestClient(
        create_server(
            settings,
            service=ControlPlaneService(clock=lambda: NOW),
            acceptance_control=control,
        )
    )


def start_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": "acceptance-start-1",
        "dataset_id": "dataset-v1",
        "dataset_digest": DATASET,
        "strategy_id": "strategy-v1",
        "strategy_digest": STRATEGY,
        "config_digest": CONFIG,
        "enable_soak_24h": True,
        "enable_soak_72h": False,
    }


def test_reader_auth_guards_current_projection() -> None:
    fake = FakeAcceptanceClient()
    client = application(fake)

    denied = client.get("/api/control/v1/acceptance/current")
    accepted = client.get(
        "/api/control/v1/acceptance/current",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "CONTROL_READER_AUTH_REQUIRED"
    assert accepted.status_code == 200
    assert accepted.json() == {"schema_version": 1, "run": None}
    assert fake.calls == [("current", "")]


def test_operator_auth_is_required_for_mutations_and_actor_is_not_browser_payload() -> None:
    fake = FakeAcceptanceClient()
    client = application(fake)

    denied = client.post("/api/control/v1/acceptance/start", json=start_payload())
    accepted = client.post(
        "/api/control/v1/acceptance/start",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json=start_payload(),
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "CONTROL_OPERATOR_AUTH_REQUIRED"
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["mutation"] == "START"
    assert payload["request_id"] == "acceptance-start-1"
    assert payload["operator"] == "control-plane:local-operator"
    assert fake.calls == [("start", "acceptance-start-1")]


def test_mutation_route_preserves_request_and_run_id() -> None:
    fake = FakeAcceptanceClient()
    client = application(fake)

    response = client.post(
        "/api/control/v1/acceptance/retry",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={
            "schema_version": 1,
            "request_id": "retry-1",
            "acceptance_run_id": "acceptance-v1",
        },
    )

    assert response.status_code == 200
    assert response.json()["mutation"] == "RETRY"
    assert fake.calls == [("retry", "retry-1")]


def test_browser_cannot_supply_full_rc_mode_or_operator() -> None:
    fake = FakeAcceptanceClient()
    client = application(fake)
    headers = {"Authorization": f"Bearer {OPERATOR}"}

    for forbidden in (
        {"full_rc_sha": "a" * 40},
        {"mode": "PAPER"},
        {"operator": "browser-admin"},
        {"path": "/Users/private/acceptance.sqlite3"},
    ):
        response = client.post(
            "/api/control/v1/acceptance/start",
            headers=headers,
            json={**start_payload(), **forbidden},
        )
        assert response.status_code == 422

    assert fake.calls == []


def test_owner_idempotency_conflict_maps_without_raw_exception_text() -> None:
    fake = FakeAcceptanceClient()
    fake.error = AcceptanceOwnerResponseError(
        "IDEMPOTENCY_CONFLICT",
        request_id="retry-1",
        acceptance_run_id="acceptance-v1",
    )
    client = application(fake)

    response = client.post(
        "/api/control/v1/acceptance/retry",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={
            "schema_version": 1,
            "request_id": "retry-1",
            "acceptance_run_id": "acceptance-v1",
        },
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONTROL_ACCEPTANCE_IDEMPOTENCY_CONFLICT"
    assert error["technical_details"] == {
        "acceptance_run_id": "acceptance-v1",
        "owner_code": "IDEMPOTENCY_CONFLICT",
        "request_id": "retry-1",
    }
    assert "/Users/" not in response.text
    assert "token=" not in response.text


def test_unconfigured_acceptance_fails_lazily_without_creating_owner_state() -> None:
    settings = ServerSettings(reader_token=READER)
    client = TestClient(create_server(settings, service=ControlPlaneService(clock=lambda: NOW)))

    response = client.get(
        "/api/control/v1/acceptance/current",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_ACCEPTANCE_NOT_CONFIGURED"


def test_control_operator_auth_must_be_configured_even_when_owner_client_can_mutate() -> None:
    fake = FakeAcceptanceClient()
    client = application(fake, operator_token=None)

    response = client.post("/api/control/v1/acceptance/start", json=start_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_OPERATOR_AUTH_NOT_CONFIGURED"
    assert fake.calls == []


def test_environment_settings_keep_downstream_acceptance_credentials_private() -> None:
    settings = settings_from_environment(
        {
            "MODULAR_CONTROL_SYSTEM_ACCEPTANCE_URL": "http://127.0.0.1:8781",
            "MODULAR_CONTROL_SYSTEM_ACCEPTANCE_READER_TOKEN": DOWNSTREAM_READER,
            "MODULAR_CONTROL_SYSTEM_ACCEPTANCE_OPERATOR_TOKEN": DOWNSTREAM_OPERATOR,
        }
    )

    assert settings.system_acceptance_url == "http://127.0.0.1:8781"
    assert settings.system_acceptance_reader_token == DOWNSTREAM_READER
    assert settings.system_acceptance_operator_token == DOWNSTREAM_OPERATOR
    rendered = repr(settings)
    assert DOWNSTREAM_READER not in rendered
    assert DOWNSTREAM_OPERATOR not in rendered


def test_acceptance_url_requires_reader_token_and_rejects_orphan_tokens() -> None:
    try:
        ServerSettings(system_acceptance_url="http://127.0.0.1:8781")
    except ValueError as exc:
        assert "reader_token" in str(exc)
    else:
        raise AssertionError("Acceptance URL without reader token must fail")

    try:
        ServerSettings(system_acceptance_reader_token=DOWNSTREAM_READER)
    except ValueError as exc:
        assert "require system_acceptance_url" in str(exc)
    else:
        raise AssertionError("orphan downstream token must fail")
