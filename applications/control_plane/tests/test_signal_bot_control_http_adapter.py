from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from modular_control_plane.adapters.signal_bot_control_http import (
    SignalBotControlHttpAdapter,
    SignalBotControlHttpError,
    SignalBotControlTransportErrorCode,
)

READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"
NOW = datetime(2026, 8, 9, 8, 30, tzinfo=UTC)


def _response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    )


def _adapter(handler: httpx.MockTransport) -> SignalBotControlHttpAdapter:
    return SignalBotControlHttpAdapter(
        "http://127.0.0.1:8081",
        reader_token=READER,
        operator_token=OPERATOR,
        transport=handler,
    )


def test_status_uses_reader_credential_and_projects_only_owner_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/control/v1/status"
        assert request.headers["authorization"] == f"Bearer {READER}"
        assert request.headers["accept"] == "application/json"
        return _response(
            {
                "configured": True,
                "reachable": True,
                "notifications_enabled": True,
                "status": "ok",
                "cycles": 12,
                "signals_sent": 5,
                "last_success_at": NOW.isoformat(),
                "last_error_type": None,
            }
        )

    status = _adapter(httpx.MockTransport(handler)).status()

    assert status.configured is True
    assert status.reachable is True
    assert status.notifications_enabled is True
    assert status.status == "ok"
    assert status.cycles == 12
    assert status.signals_sent == 5
    assert status.last_success_at == NOW


def test_test_notification_sends_exact_request_id_with_operator_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/control/v1/test-notification"
        assert request.headers["authorization"] == f"Bearer {OPERATOR}"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"request_id": "control-ui-test-1"}
        return _response(
            {
                "request_id": "control-ui-test-1",
                "operator": "control-plane:local-operator",
                "completed_at": NOW.isoformat(),
                "replayed": False,
            }
        )

    result = _adapter(httpx.MockTransport(handler)).test_notification("control-ui-test-1")

    assert result.request_id == "control-ui-test-1"
    assert result.operator == "control-plane:local-operator"
    assert result.completed_at == NOW
    assert result.replayed is False


def test_owner_replay_is_preserved_without_control_plane_deduplication() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(
            {
                "request_id": "retry-1",
                "operator": "control-plane:local-operator",
                "completed_at": NOW.isoformat(),
                "replayed": calls > 1,
            }
        )

    adapter = _adapter(httpx.MockTransport(handler))
    first = adapter.test_notification("retry-1")
    second = adapter.test_notification("retry-1")

    assert first.replayed is False
    assert second.replayed is True
    assert calls == 2


@pytest.mark.parametrize(
    ("status", "owner_code", "expected"),
    [
        (400, "INVALID_REQUEST", SignalBotControlTransportErrorCode.INVALID_REQUEST),
        (409, "IDEMPOTENCY_CONFLICT", SignalBotControlTransportErrorCode.IDEMPOTENCY_CONFLICT),
        (429, "RATE_LIMITED", SignalBotControlTransportErrorCode.RATE_LIMITED),
        (502, "REJECTED", SignalBotControlTransportErrorCode.REJECTED),
        (503, "NOT_CONFIGURED", SignalBotControlTransportErrorCode.NOT_CONFIGURED),
        (503, "RECOVERY_REQUIRED", SignalBotControlTransportErrorCode.RECOVERY_REQUIRED),
    ],
)
def test_safe_owner_error_taxonomy_is_preserved(
    status: int,
    owner_code: str,
    expected: SignalBotControlTransportErrorCode,
) -> None:
    adapter = _adapter(
        httpx.MockTransport(lambda _request: _response({"error": owner_code}, status))
    )

    with pytest.raises(SignalBotControlHttpError) as raised:
        adapter.test_notification("request-1")

    assert raised.value.code is expected
    assert raised.value.status_code == status
    assert owner_code in str(raised.value)


def test_transport_failure_is_sanitized_without_url_or_raw_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "secret transport detail /Users/private/signal-bot.sqlite",
            request=request,
        )

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(SignalBotControlHttpError) as raised:
        adapter.status()

    assert raised.value.code is SignalBotControlTransportErrorCode.UNREACHABLE
    rendered = str(raised.value)
    assert "/Users/private" not in rendered
    assert "signal-bot.sqlite" not in rendered
    assert READER not in rendered
    assert OPERATOR not in rendered


def test_status_contract_rejects_private_or_unknown_owner_fields() -> None:
    payload = {
        "configured": True,
        "reachable": True,
        "notifications_enabled": True,
        "status": "ok",
        "cycles": 1,
        "signals_sent": 1,
        "last_success_at": NOW.isoformat(),
        "last_error_type": None,
        "bot_token": "PRIVATE-BOT-TOKEN",
    }
    adapter = _adapter(httpx.MockTransport(lambda _request: _response(payload)))

    with pytest.raises(SignalBotControlHttpError) as raised:
        adapter.status()

    assert raised.value.code is SignalBotControlTransportErrorCode.OWNER_FAILURE
    assert "PRIVATE-BOT-TOKEN" not in str(raised.value)


def test_mutation_response_must_echo_exact_request_identity() -> None:
    adapter = _adapter(
        httpx.MockTransport(
            lambda _request: _response(
                {
                    "request_id": "different-request",
                    "operator": "control-plane:local-operator",
                    "completed_at": NOW.isoformat(),
                    "replayed": False,
                }
            )
        )
    )

    with pytest.raises(SignalBotControlHttpError) as raised:
        adapter.test_notification("expected-request")

    assert raised.value.code is SignalBotControlTransportErrorCode.OWNER_FAILURE


def test_invalid_request_id_fails_before_network_call() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response({})

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(SignalBotControlHttpError) as raised:
        adapter.test_notification("bad request with spaces")

    assert raised.value.code is SignalBotControlTransportErrorCode.INVALID_REQUEST
    assert called is False


def test_response_size_is_bounded() -> None:
    adapter = _adapter(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"{" + b"x" * (64 * 1024) + b"}",
            )
        )
    )

    with pytest.raises(SignalBotControlHttpError) as raised:
        adapter.status()

    assert raised.value.code is SignalBotControlTransportErrorCode.OWNER_FAILURE


def test_configuration_is_loopback_only_and_credentials_are_distinct() -> None:
    with pytest.raises(ValueError, match="loopback"):
        SignalBotControlHttpAdapter(
            "http://example.com:8081",
            reader_token=READER,
            operator_token=OPERATOR,
        )
    with pytest.raises(ValueError, match="distinct"):
        SignalBotControlHttpAdapter(
            "http://127.0.0.1:8081",
            reader_token=READER,
            operator_token=READER,
        )


def test_repr_never_contains_owner_credentials() -> None:
    adapter = SignalBotControlHttpAdapter(
        "http://127.0.0.1:8081",
        reader_token=READER,
        operator_token=OPERATOR,
    )

    rendered = repr(adapter)
    assert READER not in rendered
    assert OPERATOR not in rendered
    assert "token_configured=True" in rendered
