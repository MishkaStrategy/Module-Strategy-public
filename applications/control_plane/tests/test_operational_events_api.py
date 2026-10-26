from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from modular_control_plane.adapters.trading_runner_events_http import (
    TradingRunnerOperationalEventsHttpAdapter,
)
from modular_control_plane.operational_events import OperationalEventsControlService
from modular_control_plane.server import ServerSettings, create_server, settings_from_environment
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 8, 9, 15, 30, tzinfo=UTC)
READER = "control-reader-token"
DOWNSTREAM_READER = "events-reader-token-0123456789"
CURSOR = "trr-v1:0123456789abcdef:3"
NEXT_CURSOR = "trr-v1:0123456789abcdef:7"


def response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


def event() -> dict[str, object]:
    return {
        "event_id": "trr-000000000007-0123456789abcdef",
        "source": "trading_runner",
        "resource": "paper-runtime-1",
        "timestamp": NOW.isoformat(),
        "event_type": "operator.lifecycle",
        "severity": "INFO",
        "state": "running",
        "payload": {
            "operation": "START",
            "request_id": "dashboard-paper-start-1",
            "result": "SUCCEEDED",
            "replayed": False,
        },
        "cursor": NEXT_CURSOR,
    }


def page() -> dict[str, object]:
    return {
        "source": "trading_runner",
        "events": [event()],
        "next_cursor": NEXT_CURSOR,
        "has_more": False,
        "scanned_count": 1,
        "omitted_count": 0,
        "retention_limit": 1000,
        "truncated": False,
    }


def application(handler: httpx.MockTransport) -> TestClient:
    adapter = TradingRunnerOperationalEventsHttpAdapter(
        "http://127.0.0.1:8782",
        reader_token=DOWNSTREAM_READER,
        transport=handler,
    )
    control = OperationalEventsControlService(adapter)
    return TestClient(
        create_server(
            ServerSettings(reader_token=READER),
            service=ControlPlaneService(clock=lambda: NOW),
            operational_events_control=control,
        )
    )


def test_reader_query_projects_versioned_sanitized_page_and_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/events/v1/query"
        assert request.headers["authorization"] == f"Bearer {DOWNSTREAM_READER}"
        assert parse_qs(request.url.query.decode()) == {
            "cursor": [CURSOR],
            "limit": ["25"],
            "event_type": ["operator.lifecycle"],
            "severity": ["INFO"],
        }
        return response(page())

    client = application(httpx.MockTransport(handler))
    unauthenticated = client.get("/api/control/v1/events/trading-runner")
    accepted = client.get(
        "/api/control/v1/events/trading-runner",
        headers={"Authorization": f"Bearer {READER}"},
        params={
            "cursor": CURSOR,
            "limit": "25",
            "event_type": "operator.lifecycle",
            "severity": "INFO",
        },
    )

    assert unauthenticated.status_code == 401
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["schema_version"] == 1
    assert payload["source"] == "trading_runner"
    assert payload["next_cursor"] == NEXT_CURSOR
    assert payload["events"][0]["event_type"] == "operator.lifecycle"
    assert payload["events"][0]["payload"]["operation"] == "START"
    assert "/Users/private" not in accepted.text
    assert DOWNSTREAM_READER not in accepted.text


def test_path_tail_file_and_shell_queries_are_rejected_before_owner() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.query.decode())
        return response(page())

    client = application(httpx.MockTransport(handler))
    dangerous_queries = (
        {"path": "/Users/private/runtime.log"},
        {"file": "/tmp/runtime.log"},
        {"tail": "100"},
        {"follow": "true"},
        {"command": "tail -f"},
        {"shell": "cat /tmp/runtime.log"},
        {"cwd": "/Users/private"},
    )
    for params in dangerous_queries:
        rejected = client.get(
            "/api/control/v1/events/trading-runner",
            headers={"Authorization": f"Bearer {READER}"},
            params=params,
        )
        assert rejected.status_code == 400
        error = rejected.json()["error"]
        assert error["code"] == "CONTROL_EVENTS_INVALID_REQUEST"
        assert error["retryable"] is False
        assert "/Users/private" not in rejected.text
        assert "/tmp/runtime.log" not in rejected.text
        assert "tail -f" not in rejected.text
        assert "cat /tmp" not in rejected.text

    assert seen == []


def test_malformed_cursor_and_unavailable_source_are_deterministic() -> None:
    client = application(httpx.MockTransport(lambda _request: response(page())))
    malformed = client.get(
        "/api/control/v1/events/trading-runner",
        headers={"Authorization": f"Bearer {READER}"},
        params={"cursor": "/tmp/runtime.log"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "CONTROL_EVENTS_INVALID_CURSOR"

    unavailable = application(
        httpx.MockTransport(lambda _request: response({"error": "SOURCE_UNAVAILABLE"}, 503))
    ).get(
        "/api/control/v1/events/trading-runner",
        headers={"Authorization": f"Bearer {READER}"},
    )
    assert unavailable.status_code == 503
    error = unavailable.json()["error"]
    assert error["code"] == "CONTROL_EVENTS_SOURCE_UNAVAILABLE"
    assert error["retryable"] is True


def test_unconfigured_source_is_typed_503() -> None:
    client = TestClient(
        create_server(
            ServerSettings(reader_token=READER),
            service=ControlPlaneService(clock=lambda: NOW),
            operational_events_control=None,
        )
    )
    result = client.get(
        "/api/control/v1/events/trading-runner",
        headers={"Authorization": f"Bearer {READER}"},
    )
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "CONTROL_EVENTS_NOT_CONFIGURED"


def test_events_server_settings_require_complete_loopback_reader_configuration() -> None:
    settings = settings_from_environment(
        {
            "MODULAR_CONTROL_TRADING_RUNNER_EVENTS_URL": "http://127.0.0.1:8782",
            "MODULAR_CONTROL_TRADING_RUNNER_EVENTS_READER_TOKEN": DOWNSTREAM_READER,
        }
    )
    assert settings.trading_runner_events_url == "http://127.0.0.1:8782"
    assert settings.trading_runner_events_reader_token == DOWNSTREAM_READER

    for environment in (
        {"MODULAR_CONTROL_TRADING_RUNNER_EVENTS_URL": "http://127.0.0.1:8782"},
        {"MODULAR_CONTROL_TRADING_RUNNER_EVENTS_READER_TOKEN": DOWNSTREAM_READER},
        {
            "MODULAR_CONTROL_TRADING_RUNNER_EVENTS_URL": "http://example.com:8782",
            "MODULAR_CONTROL_TRADING_RUNNER_EVENTS_READER_TOKEN": DOWNSTREAM_READER,
        },
    ):
        try:
            settings_from_environment(environment)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete or non-loopback events configuration must fail closed")
