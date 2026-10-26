from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from modular_control_plane.adapters.signal_bot_control_http import SignalBotControlHttpAdapter
from modular_control_plane.notifications import NotificationsControlService, NotificationStatus
from modular_control_plane.server import ServerSettings, create_server, settings_from_environment
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
READER = "browser-reader-token"
OPERATOR = "browser-operator-token"
DOWNSTREAM_READER = "signal-reader-token"
DOWNSTREAM_OPERATOR = "signal-operator-token"


def owner_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


def application(handler: httpx.MockTransport) -> TestClient:
    adapter = SignalBotControlHttpAdapter(
        "http://127.0.0.1:8081",
        reader_token=DOWNSTREAM_READER,
        operator_token=DOWNSTREAM_OPERATOR,
        transport=handler,
    )
    control = NotificationsControlService(adapter, clock=lambda: NOW)
    return TestClient(
        create_server(
            ServerSettings(reader_token=READER, operator_token=OPERATOR),
            service=ControlPlaneService(clock=lambda: NOW),
            notifications_control=control,
        )
    )


def status_payload() -> dict[str, object]:
    return {
        "configured": True,
        "reachable": True,
        "notifications_enabled": True,
        "status": "ok",
        "cycles": 9,
        "signals_sent": 4,
        "last_success_at": NOW.isoformat(),
        "last_error_type": None,
    }


def test_reader_auth_guards_owner_status_projection() -> None:
    client = application(httpx.MockTransport(lambda _request: owner_response(status_payload())))

    denied = client.get("/api/control/v1/notifications/status")
    accepted = client.get(
        "/api/control/v1/notifications/status",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "CONTROL_READER_AUTH_REQUIRED"
    assert accepted.status_code == 200
    expected = NotificationStatus.model_validate({"schema_version": 1, **status_payload()})
    assert accepted.json() == json.loads(expected.model_dump_json())


def test_operator_auth_is_required_and_browser_can_send_only_request_id() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/control/v1/test-notification"
        assert request.headers["authorization"] == f"Bearer {DOWNSTREAM_OPERATOR}"
        assert json.loads(request.content) == {"request_id": "notification-1"}
        return owner_response(
            {
                "request_id": "notification-1",
                "operator": "control-plane:local-operator",
                "completed_at": NOW.isoformat(),
                "replayed": False,
            }
        )

    client = application(httpx.MockTransport(handler))
    denied = client.post(
        "/api/control/v1/notifications/test",
        json={"schema_version": 1, "request_id": "notification-1"},
    )
    accepted = client.post(
        "/api/control/v1/notifications/test",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"schema_version": 1, "request_id": "notification-1"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "CONTROL_OPERATOR_AUTH_REQUIRED"
    assert accepted.status_code == 200
    assert accepted.json()["operator"] == "control-plane:local-operator"
    assert calls == 1

    for forbidden in (
        {"message": "arbitrary"},
        {"operator": "browser-admin"},
        {"chat_id": "private-chat"},
        {"bot_token": "private-token"},
        {"path": "/Users/private/signal-bot.sqlite"},
    ):
        response = client.post(
            "/api/control/v1/notifications/test",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json={"schema_version": 1, "request_id": "notification-2", **forbidden},
        )
        assert response.status_code == 422

    assert calls == 1


def test_owner_replay_is_returned_without_second_control_plane_state() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return owner_response(
            {
                "request_id": "same-request",
                "operator": "control-plane:local-operator",
                "completed_at": NOW.isoformat(),
                "replayed": calls > 1,
            }
        )

    client = application(httpx.MockTransport(handler))
    headers = {"Authorization": f"Bearer {OPERATOR}"}
    body = {"schema_version": 1, "request_id": "same-request"}

    first = client.post("/api/control/v1/notifications/test", headers=headers, json=body)
    second = client.post("/api/control/v1/notifications/test", headers=headers, json=body)

    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert calls == 2


def test_rate_limit_and_recovery_errors_are_safe_and_preserve_retry_semantics() -> None:
    cases = (
        (429, "RATE_LIMITED", 429, True),
        (503, "RECOVERY_REQUIRED", 503, False),
        (409, "IDEMPOTENCY_CONFLICT", 409, False),
        (503, "UNREACHABLE", 503, True),
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
            "/api/control/v1/notifications/test",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json={"schema_version": 1, "request_id": "safe-error"},
        )

        assert response.status_code == expected_status
        error = response.json()["error"]
        assert error["code"] == f"CONTROL_NOTIFICATIONS_{owner_code}"
        assert error["retryable"] is retryable
        rendered = json.dumps(error)
        assert DOWNSTREAM_READER not in rendered
        assert DOWNSTREAM_OPERATOR not in rendered
        assert "/Users/private" not in rendered


def test_server_settings_require_complete_distinct_owner_control_credentials() -> None:
    environment = {
        "MODULAR_CONTROL_SIGNAL_BOT_CONTROL_URL": "http://127.0.0.1:8081",
        "MODULAR_CONTROL_SIGNAL_BOT_CONTROL_READER_TOKEN": DOWNSTREAM_READER,
        "MODULAR_CONTROL_SIGNAL_BOT_CONTROL_OPERATOR_TOKEN": DOWNSTREAM_OPERATOR,
    }
    settings = settings_from_environment(environment)
    assert settings.signal_bot_control_url == "http://127.0.0.1:8081"
    assert settings.signal_bot_control_reader_token == DOWNSTREAM_READER
    assert settings.signal_bot_control_operator_token == DOWNSTREAM_OPERATOR

    try:
        settings_from_environment(
            {
                "MODULAR_CONTROL_SIGNAL_BOT_CONTROL_URL": "http://127.0.0.1:8081",
                "MODULAR_CONTROL_SIGNAL_BOT_CONTROL_READER_TOKEN": DOWNSTREAM_READER,
            }
        )
    except ValueError as exc:
        assert "reader/operator tokens" in str(exc)
    else:
        raise AssertionError("partial owner credentials must fail closed")

    try:
        ServerSettings(
            signal_bot_control_url="http://127.0.0.1:8081",
            signal_bot_control_reader_token=DOWNSTREAM_READER,
            signal_bot_control_operator_token=DOWNSTREAM_READER,
        )
    except ValueError as exc:
        assert "distinct" in str(exc)
    else:
        raise AssertionError("identical downstream credentials must fail closed")
