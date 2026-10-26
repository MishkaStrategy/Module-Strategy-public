from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from modular_control_plane.adapters.trading_runner_lifecycle_http import (
    TradingRunnerLifecycleHttpAdapter,
)
from modular_control_plane.paper_lifecycle import PaperLifecycleControlService
from modular_control_plane.server import ServerSettings, create_server, settings_from_environment
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
READER = "browser-reader-token"
OPERATOR = "browser-operator-token"
DOWNSTREAM_READER = "runner-reader-token-012345"
DOWNSTREAM_OPERATOR = "runner-operator-token-012345"
CONFIG_HASH = "a" * 64


def owner_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


def owner_result(
    *,
    request_id: str,
    operation: str,
    previous_state: str,
    new_state: str,
    replayed: bool = False,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "operator": "control-plane:local-operator",
        "operation": operation,
        "previous_state": previous_state,
        "new_state": new_state,
        "config_hash": CONFIG_HASH,
        "completed_at": NOW.isoformat(),
        "replayed": replayed,
    }


def application(handler: httpx.MockTransport) -> TestClient:
    adapter = TradingRunnerLifecycleHttpAdapter(
        "http://127.0.0.1:8780",
        reader_token=DOWNSTREAM_READER,
        operator_token=DOWNSTREAM_OPERATOR,
        transport=handler,
    )
    control = PaperLifecycleControlService(adapter)
    return TestClient(
        create_server(
            ServerSettings(reader_token=READER, operator_token=OPERATOR),
            service=ControlPlaneService(clock=lambda: NOW),
            paper_lifecycle_control=control,
        )
    )


def test_lifecycle_capability_reports_only_composition_availability_and_paper_scope() -> None:
    attached = application(httpx.MockTransport(lambda _request: owner_response({})))
    attached_response = attached.get("/api/control/v1/paper/lifecycle-capability")

    detached = TestClient(
        create_server(
            ServerSettings(reader_token=READER, operator_token=OPERATOR),
            service=ControlPlaneService(clock=lambda: NOW),
            paper_lifecycle_control=None,
        )
    )
    detached_response = detached.get("/api/control/v1/paper/lifecycle-capability")

    assert attached_response.status_code == 200
    assert attached_response.json() == {"schema_version": 1, "available": True, "paper_only": True}
    assert detached_response.status_code == 200
    assert detached_response.json() == {"schema_version": 1, "available": False, "paper_only": True}
    assert "runtime" not in attached_response.text.lower()
    assert DOWNSTREAM_READER not in attached_response.text
    assert DOWNSTREAM_OPERATOR not in attached_response.text


def test_operator_auth_and_exact_browser_payload_for_all_mutations() -> None:
    cases = (
        ("/api/control/v1/paper/start", "/lifecycle/v1/start", "START", "configured", "running"),
        ("/api/control/v1/paper/stop", "/lifecycle/v1/stop", "STOP", "running", "stopped"),
        (
            "/api/control/v1/paper/kill-switch/activate",
            "/lifecycle/v1/kill-switch/activate",
            "ACTIVATE_KILL_SWITCH",
            "running",
            "kill_switch_active",
        ),
        (
            "/api/control/v1/paper/kill-switch/reset",
            "/lifecycle/v1/kill-switch/reset",
            "RESET_KILL_SWITCH",
            "kill_switch_active",
            "stopped",
        ),
    )
    for api_path, owner_path, operation, previous_state, new_state in cases:
        calls = 0

        def handler(
            request: httpx.Request,
            expected_owner_path: str = owner_path,
            expected_operation: str = operation,
            expected_previous_state: str = previous_state,
            expected_new_state: str = new_state,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert request.url.path == expected_owner_path
            assert request.headers["authorization"] == f"Bearer {DOWNSTREAM_OPERATOR}"
            assert json.loads(request.content) == {"request_id": "lifecycle-1"}
            return owner_response(
                owner_result(
                    request_id="lifecycle-1",
                    operation=expected_operation,
                    previous_state=expected_previous_state,
                    new_state=expected_new_state,
                )
            )

        client = application(httpx.MockTransport(handler))
        denied = client.post(
            api_path,
            headers={"Authorization": f"Bearer {READER}"},
            json={"schema_version": 1, "request_id": "lifecycle-1"},
        )
        accepted = client.post(
            api_path,
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json={"schema_version": 1, "request_id": "lifecycle-1"},
        )

        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "CONTROL_OPERATOR_AUTH_REQUIRED"
        assert accepted.status_code == 200
        assert accepted.json()["mutation"] == operation
        assert accepted.json()["previous_state"] == previous_state
        assert accepted.json()["new_state"] == new_state
        assert calls == 1


def test_browser_cannot_supply_operator_reason_command_path_or_mode() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return owner_response(
            owner_result(
                request_id="safe",
                operation="START",
                previous_state="configured",
                new_state="running",
            )
        )

    client = application(httpx.MockTransport(handler))
    for forbidden in (
        {"operator": "browser-admin"},
        {"reason": "arbitrary"},
        {"command": "start --live"},
        {"path": "/Users/private/runtime.sqlite"},
        {"mode": "live"},
    ):
        response = client.post(
            "/api/control/v1/paper/start",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json={"schema_version": 1, "request_id": "safe", **forbidden},
        )
        assert response.status_code == 422

    assert calls == 0


def test_duplicate_request_is_forwarded_for_owner_replay_without_local_ledger() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return owner_response(
            owner_result(
                request_id="duplicate-start",
                operation="START",
                previous_state="configured",
                new_state="running",
                replayed=calls > 1,
            )
        )

    client = application(httpx.MockTransport(handler))
    headers = {"Authorization": f"Bearer {OPERATOR}"}
    body = {"schema_version": 1, "request_id": "duplicate-start"}

    first = client.post("/api/control/v1/paper/start", headers=headers, json=body)
    second = client.post("/api/control/v1/paper/start", headers=headers, json=body)

    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert calls == 2


def test_owner_errors_are_safe_and_preserve_retry_semantics() -> None:
    cases = (
        (400, "INVALID_REQUEST", 400, False),
        (400, "PAPER_ONLY", 400, False),
        (409, "IDEMPOTENCY_CONFLICT", 409, False),
        (422, "INVALID_TRANSITION", 422, False),
        (503, "RECOVERY_REQUIRED", 503, True),
        (503, "OWNER_FAILURE", 503, True),
    )
    for owner_status, owner_code, expected_status, retryable in cases:
        client = application(
            httpx.MockTransport(
                lambda _request, status=owner_status, code=owner_code: owner_response(
                    {"error": code}, status
                )
            )
        )
        response = client.post(
            "/api/control/v1/paper/start",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json={"schema_version": 1, "request_id": "safe-error"},
        )

        assert response.status_code == expected_status
        error = response.json()["error"]
        assert error["code"] == f"CONTROL_PAPER_{owner_code}"
        assert error["retryable"] is retryable
        rendered = json.dumps(error)
        assert DOWNSTREAM_READER not in rendered
        assert DOWNSTREAM_OPERATOR not in rendered
        assert "/Users/private" not in rendered


def test_server_settings_require_complete_distinct_lifecycle_credentials() -> None:
    environment = {
        "MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_URL": "http://127.0.0.1:8780",
        "MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_READER_TOKEN": DOWNSTREAM_READER,
        "MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_OPERATOR_TOKEN": DOWNSTREAM_OPERATOR,
    }
    settings = settings_from_environment(environment)
    assert settings.trading_runner_lifecycle_url == "http://127.0.0.1:8780"
    assert settings.trading_runner_lifecycle_reader_token == DOWNSTREAM_READER
    assert settings.trading_runner_lifecycle_operator_token == DOWNSTREAM_OPERATOR

    try:
        settings_from_environment(
            {
                "MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_URL": "http://127.0.0.1:8780",
                "MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_READER_TOKEN": DOWNSTREAM_READER,
            }
        )
    except ValueError as exc:
        assert "reader/operator tokens" in str(exc)
    else:
        raise AssertionError("partial owner credentials must fail closed")

    try:
        ServerSettings(
            trading_runner_lifecycle_url="http://127.0.0.1:8780",
            trading_runner_lifecycle_reader_token=DOWNSTREAM_READER,
            trading_runner_lifecycle_operator_token=DOWNSTREAM_READER,
        )
    except ValueError as exc:
        assert "distinct" in str(exc)
    else:
        raise AssertionError("identical downstream credentials must fail closed")
