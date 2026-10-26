from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pytest

from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.operational_events import TradingRunnerOperationalEventQuery
from modular_trading_runner.operational_events_http import TradingRunnerOperationalEventsHttpServer

NOW = datetime(2026, 8, 8, 22, 0, tzinfo=UTC)
READER = "reader-token-0123456789"


def build_server() -> TradingRunnerOperationalEventsHttpServer:
    journal = RuntimeEventJournal(run_id="events-http-run")
    journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={"state": "running", "private_path": "/Users/private/state.db"},
    )
    journal.append(
        timestamp=NOW,
        event_type="runtime.failed",
        aggregate_id="runner-1",
        payload={
            "error_type": "BootstrapError",
            "error": "token=TOPSECRET /Users/private/state.db",
        },
    )
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")
    return TradingRunnerOperationalEventsHttpServer(query, reader_token=READER, port=0)


def decode(body: bytes) -> dict[str, object]:
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def test_transport_is_loopback_only_and_closes_before_start() -> None:
    server = build_server()
    server.close()
    server.close()
    with pytest.raises(RuntimeError, match="closed"):
        server.start()

    journal = RuntimeEventJournal(run_id="reject-run")
    query = TradingRunnerOperationalEventQuery(journal, resource="runner-1")
    with pytest.raises(ValueError, match="loopback"):
        TradingRunnerOperationalEventsHttpServer(
            query,
            reader_token=READER,
            host="0.0.0.0",
            port=0,
        )


def test_reader_auth_and_privacy_safe_projection() -> None:
    server = build_server()
    try:
        denied, _, denied_body = server.response(
            path="/events/v1/query",
            authorization="Bearer wrong-reader-token",
        )
        assert denied == 401
        assert decode(denied_body) == {"error": "UNAUTHORIZED"}

        code, headers, body = server.response(
            path="/events/v1/query?limit=10",
            authorization=f"Bearer {READER}",
        )
        payload = decode(body)
        assert code == 200
        assert headers["Cache-Control"] == "no-store"
        events = payload["events"]
        assert isinstance(events, list)
        assert [event["event_type"] for event in events] == ["runtime.started", "runtime.failed"]
        text = body.decode().lower()
        assert "topsecret" not in text
        assert "/users/" not in text
        assert "private_path" not in text
        assert READER.lower() not in text
    finally:
        server.close()


def test_query_filters_and_cursor_are_typed_and_bounded() -> None:
    server = build_server()
    try:
        path = "/events/v1/query?limit=1&event_type=runtime.failed&severity=ERROR"
        code, _, body = server.response(path=path, authorization=f"Bearer {READER}")
        payload = decode(body)
        assert code == 200
        events = payload["events"]
        assert isinstance(events, list)
        assert len(events) == 1
        assert events[0]["event_type"] == "runtime.failed"
        cursor = payload["next_cursor"]
        assert isinstance(cursor, str)

        encoded_cursor = urllib.parse.quote(cursor, safe="")
        next_code, _, next_body = server.response(
            path=f"/events/v1/query?cursor={encoded_cursor}&limit=1",
            authorization=f"Bearer {READER}",
        )
        assert next_code == 200
        assert decode(next_body)["events"] == []
    finally:
        server.close()


@pytest.mark.parametrize(
    "path",
    [
        "/events/v1/query?path=/tmp/private",
        "/events/v1/query?command=cat",
        "/events/v1/query?limit=0",
        "/events/v1/query?limit=201",
        "/events/v1/query?cursor=bad-cursor",
        "/events/v1/query?severity=NOT_A_LEVEL",
        "/events/v1/query?event_type=unknown.raw.event",
        "/events/v1/query?limit=1&limit=2",
        "/events/v1/query?cursor=",
    ],
)
def test_transport_rejects_arbitrary_path_command_and_malformed_filters(path: str) -> None:
    server = build_server()
    try:
        code, _, body = server.response(path=path, authorization=f"Bearer {READER}")
        assert code == 400
        assert decode(body)["error"] in {"INVALID_REQUEST", "INVALID_CURSOR"}
    finally:
        server.close()


def test_real_loopback_query() -> None:
    server = build_server()
    server.start()
    host, port = server.address
    try:
        request = urllib.request.Request(
            f"http://{host}:{port}/events/v1/query?event_type=runtime.started",
            headers={"Authorization": f"Bearer {READER}"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
            payload = decode(response.read())
            events = payload["events"]
            assert isinstance(events, list)
            assert [event["event_type"] for event in events] == ["runtime.started"]

        denied = urllib.request.Request(f"http://{host}:{port}/events/v1/query")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(denied, timeout=2)
        assert raised.value.code == 401
    finally:
        server.close()
