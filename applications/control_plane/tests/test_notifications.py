from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from modular_control_plane.adapters.signal_bot_control_http import SignalBotControlHttpAdapter
from modular_control_plane.contracts import HealthStatus
from modular_control_plane.notifications import NotificationsControlService
from modular_control_plane.notifications import (
    TestNotificationRequest as NotificationTestRequest,
)

NOW = datetime(2026, 8, 9, 9, 45, tzinfo=UTC)
READER = "notifications-reader-token"
OPERATOR = "notifications-operator-token"


def response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


def service(handler: httpx.MockTransport) -> NotificationsControlService:
    return NotificationsControlService(
        SignalBotControlHttpAdapter(
            "http://127.0.0.1:8081",
            reader_token=READER,
            operator_token=OPERATOR,
            transport=handler,
        ),
        clock=lambda: NOW,
    )


def test_status_and_health_use_authoritative_owner_projection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/control/v1/status"
        return response(
            {
                "configured": True,
                "reachable": False,
                "notifications_enabled": True,
                "status": "degraded",
                "cycles": 42,
                "signals_sent": 7,
                "last_success_at": NOW.isoformat(),
                "last_error_type": "UNREACHABLE",
            }
        )

    current = service(httpx.MockTransport(handler))
    status = current.status()
    health = current.health()

    assert status.configured is True
    assert status.reachable is False
    assert status.notifications_enabled is True
    assert status.status == "degraded"
    assert health.status is HealthStatus.DEGRADED
    assert health.checked_at == NOW
    assert health.details == {
        "configured": True,
        "reachable": False,
        "notifications_enabled": True,
        "cycles": 42,
        "signals_sent": 7,
        "last_success_at": NOW.isoformat(),
        "last_error_type": "UNREACHABLE",
        "test_notification_available": True,
    }


def test_owner_unavailable_status_maps_to_unavailable_component_health() -> None:
    current = service(
        httpx.MockTransport(
            lambda _request: response(
                {
                    "configured": False,
                    "reachable": None,
                    "notifications_enabled": None,
                    "status": "unavailable",
                    "cycles": 0,
                    "signals_sent": 0,
                    "last_success_at": None,
                    "last_error_type": None,
                }
            )
        )
    )

    health = current.health()

    assert health.status is HealthStatus.UNAVAILABLE
    assert health.details["configured"] is False
    assert health.details["test_notification_available"] is True


def test_test_notification_preserves_owner_replay_without_local_ledger() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert json.loads(request.content) == {"request_id": "dashboard-test-1"}
        return response(
            {
                "request_id": "dashboard-test-1",
                "operator": "control-plane:local-operator",
                "completed_at": NOW.isoformat(),
                "replayed": calls > 1,
            }
        )

    current = service(httpx.MockTransport(handler))
    request = NotificationTestRequest(request_id="dashboard-test-1")

    first = current.test_notification(request)
    second = current.test_notification(request)

    assert first.replayed is False
    assert second.replayed is True
    assert first.operator == "control-plane:local-operator"
    assert calls == 2
