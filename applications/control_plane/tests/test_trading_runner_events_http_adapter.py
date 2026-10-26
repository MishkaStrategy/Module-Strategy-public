from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from modular_control_plane.adapters.trading_runner_events_http import (
    TradingRunnerEventsHttpError,
    TradingRunnerEventsSeverity,
    TradingRunnerEventsTransportErrorCode,
    TradingRunnerOperationalEventsHttpAdapter,
)

READER = "events-reader-token-0123456789"
CURSOR = "trr-v1:0123456789abcdef:3"
NEXT_CURSOR = "trr-v1:0123456789abcdef:7"


def response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


def event(*, cursor: str = NEXT_CURSOR, payload: object | None = None) -> dict[str, object]:
    return {
        "event_id": "trr-000000000007-0123456789abcdef",
        "source": "trading_runner",
        "resource": "paper-runtime-1",
        "timestamp": "2026-08-09T14:55:00+00:00",
        "event_type": "operator.lifecycle",
        "severity": "INFO",
        "state": "running",
        "payload": (
            {
                "operation": "START",
                "request_id": "dashboard-paper-start-1",
                "operator": "control-plane:local-operator",
                "result": "SUCCEEDED",
                "replayed": False,
            }
            if payload is None
            else payload
        ),
        "cursor": cursor,
    }


def page(
    *, events: list[object] | None = None, next_cursor: str = NEXT_CURSOR
) -> dict[str, object]:
    rows = [event()] if events is None else events
    return {
        "source": "trading_runner",
        "events": rows,
        "next_cursor": next_cursor,
        "has_more": True,
        "scanned_count": 4,
        "omitted_count": 3,
        "retention_limit": 1_000_000,
        "truncated": False,
    }


def adapter(transport: httpx.MockTransport) -> TradingRunnerOperationalEventsHttpAdapter:
    return TradingRunnerOperationalEventsHttpAdapter(
        "http://127.0.0.1:8782",
        reader_token=READER,
        transport=transport,
    )


def test_query_sends_only_allowlisted_reader_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/events/v1/query"
        assert request.headers["authorization"] == f"Bearer {READER}"
        assert parse_qs(request.url.query.decode()) == {
            "cursor": [CURSOR],
            "limit": ["25"],
            "event_type": ["operator.lifecycle", "runtime.failed"],
            "severity": ["INFO", "ERROR"],
        }
        return response(page())

    result = adapter(httpx.MockTransport(handler)).query(
        cursor=CURSOR,
        limit=25,
        event_types=("operator.lifecycle", "runtime.failed"),
        severities=(TradingRunnerEventsSeverity.INFO, TradingRunnerEventsSeverity.ERROR),
    )

    assert result.source == "trading_runner"
    assert result.events[0].event_type == "operator.lifecycle"
    assert result.events[0].payload["operation"] == "START"
    assert result.next_cursor == NEXT_CURSOR
    assert result.omitted_count == 3
    assert result.retention_limit == 1_000_000


def test_owner_truncation_and_retention_projection_is_preserved() -> None:
    payload = page()
    payload["has_more"] = False
    payload["retention_limit"] = 512
    payload["truncated"] = True

    result = adapter(httpx.MockTransport(lambda _request: response(payload))).query()

    assert result.has_more is False
    assert result.retention_limit == 512
    assert result.truncated is True


@pytest.mark.parametrize(
    ("status", "owner_code", "expected"),
    [
        (400, "INVALID_REQUEST", TradingRunnerEventsTransportErrorCode.INVALID_REQUEST),
        (400, "INVALID_CURSOR", TradingRunnerEventsTransportErrorCode.INVALID_CURSOR),
        (503, "SOURCE_UNAVAILABLE", TradingRunnerEventsTransportErrorCode.SOURCE_UNAVAILABLE),
        (401, "UNAUTHORIZED", TradingRunnerEventsTransportErrorCode.UNAUTHORIZED),
    ],
)
def test_owner_error_taxonomy_is_preserved_without_raw_detail(
    status: int,
    owner_code: str,
    expected: TradingRunnerEventsTransportErrorCode,
) -> None:
    current = adapter(httpx.MockTransport(lambda _request: response({"error": owner_code}, status)))

    with pytest.raises(TradingRunnerEventsHttpError) as raised:
        current.query()

    assert raised.value.code is expected
    assert raised.value.status_code == status
    assert READER not in str(raised.value)


def test_transport_error_does_not_expose_private_paths_or_tokens() -> None:
    def failed(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"failed {READER} /Users/private/runtime-events.json",
            request=request,
        )

    with pytest.raises(TradingRunnerEventsHttpError) as raised:
        adapter(httpx.MockTransport(failed)).query()

    assert raised.value.code is TradingRunnerEventsTransportErrorCode.UNREACHABLE
    assert READER not in str(raised.value)
    assert "/Users/private" not in str(raised.value)


def test_response_rejects_private_extra_nested_or_wrong_stream_payloads() -> None:
    private = event()
    private["private_path"] = "/Users/private/journal.json"
    cases = [
        page(events=[private]),
        page(events=[event(payload={"raw_stack": ["private", "stack"]})]),
        page(events=[event(cursor="trr-v1:fedcba9876543210:7")]),
        {**page(), "owner_path": "/Users/private/journal.json"},
    ]
    for payload in cases:
        current = adapter(httpx.MockTransport(lambda _request, body=payload: response(body)))
        with pytest.raises(TradingRunnerEventsHttpError) as raised:
            current.query()
        assert raised.value.code is TradingRunnerEventsTransportErrorCode.OWNER_FAILURE
        assert "/Users/private" not in str(raised.value)


def test_invalid_cursor_filters_and_limit_fail_before_transport() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(page())

    current = adapter(httpx.MockTransport(handler))
    invalid_calls = (
        lambda: current.query(cursor="/tmp/runtime.log"),
        lambda: current.query(limit=0),
        lambda: current.query(event_types=("runtime.started", "runtime.started")),
        lambda: current.query(event_types=("unknown.owner.event",)),
        lambda: current.query(
            severities=(TradingRunnerEventsSeverity.INFO, TradingRunnerEventsSeverity.INFO)
        ),
    )
    for invoke in invalid_calls:
        with pytest.raises(TradingRunnerEventsHttpError):
            invoke()
    assert calls == 0


def test_oversized_or_invalid_json_response_fails_closed() -> None:
    oversized = adapter(
        httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"{" + b"x" * (512 * 1024) + b"}")
        )
    )
    with pytest.raises(TradingRunnerEventsHttpError) as too_large:
        oversized.query()
    assert too_large.value.code is TradingRunnerEventsTransportErrorCode.OWNER_FAILURE

    invalid = adapter(
        httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not-json"))
    )
    with pytest.raises(TradingRunnerEventsHttpError) as invalid_json:
        invalid.query()
    assert invalid_json.value.code is TradingRunnerEventsTransportErrorCode.OWNER_FAILURE


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8782",
        "http://example.com:8782",
        "http://user:pass@127.0.0.1:8782",
        "http://127.0.0.1:8782/private",
        "http://127.0.0.1:8782?path=/tmp/runtime.log",
    ],
)
def test_events_transport_rejects_non_loopback_or_decorated_urls(url: str) -> None:
    with pytest.raises(ValueError):
        TradingRunnerOperationalEventsHttpAdapter(url, reader_token=READER)


def test_repr_is_secret_safe() -> None:
    current = TradingRunnerOperationalEventsHttpAdapter(
        "http://127.0.0.1:8782",
        reader_token=READER,
    )
    rendered = repr(current)
    assert READER not in rendered
    assert "reader_token_configured=True" in rendered
