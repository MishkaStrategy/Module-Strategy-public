from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from modular_control_plane.adapters.trading_runner_lifecycle_http import (
    TradingRunnerLifecycleHttpAdapter,
    TradingRunnerLifecycleHttpError,
    TradingRunnerLifecycleOperation,
    TradingRunnerLifecycleTransportErrorCode,
)

READER = "runner-reader-token-012345"
OPERATOR = "runner-operator-token-012345"
NOW = datetime(2026, 8, 9, 11, 15, tzinfo=UTC)
CONFIG_HASH = "a" * 64


def response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


def result_payload(
    *,
    request_id: str,
    operation: TradingRunnerLifecycleOperation,
    previous_state: str,
    new_state: str,
    replayed: bool = False,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "operator": "control-plane:local-operator",
        "operation": operation.value,
        "previous_state": previous_state,
        "new_state": new_state,
        "config_hash": CONFIG_HASH,
        "completed_at": NOW.isoformat(),
        "replayed": replayed,
    }


def adapter(handler: httpx.MockTransport) -> TradingRunnerLifecycleHttpAdapter:
    return TradingRunnerLifecycleHttpAdapter(
        "http://127.0.0.1:8780",
        reader_token=READER,
        operator_token=OPERATOR,
        transport=handler,
    )


@pytest.mark.parametrize(
    ("operation", "path", "previous_state", "new_state"),
    [
        (TradingRunnerLifecycleOperation.START, "/lifecycle/v1/start", "configured", "running"),
        (TradingRunnerLifecycleOperation.STOP, "/lifecycle/v1/stop", "running", "stopped"),
        (
            TradingRunnerLifecycleOperation.ACTIVATE_KILL_SWITCH,
            "/lifecycle/v1/kill-switch/activate",
            "running",
            "kill_switch_active",
        ),
        (
            TradingRunnerLifecycleOperation.RESET_KILL_SWITCH,
            "/lifecycle/v1/kill-switch/reset",
            "kill_switch_active",
            "stopped",
        ),
    ],
)
def test_mutations_send_exact_request_identity_to_owner(
    operation: TradingRunnerLifecycleOperation,
    path: str,
    previous_state: str,
    new_state: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == path
        assert request.headers["authorization"] == f"Bearer {OPERATOR}"
        assert json.loads(request.content) == {"request_id": "dashboard-lifecycle-1"}
        return response(
            result_payload(
                request_id="dashboard-lifecycle-1",
                operation=operation,
                previous_state=previous_state,
                new_state=new_state,
            )
        )

    result = adapter(httpx.MockTransport(handler)).mutate(operation, "dashboard-lifecycle-1")

    assert result.request_id == "dashboard-lifecycle-1"
    assert result.operator == "control-plane:local-operator"
    assert result.operation is operation
    assert result.new_state == new_state
    assert result.replayed is False


def test_owner_replay_is_preserved_without_control_plane_deduplication() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(
            result_payload(
                request_id="start-replay-1",
                operation=TradingRunnerLifecycleOperation.START,
                previous_state="configured",
                new_state="running",
                replayed=calls > 1,
            )
        )

    current = adapter(httpx.MockTransport(handler))
    first = current.mutate(TradingRunnerLifecycleOperation.START, "start-replay-1")
    second = current.mutate(TradingRunnerLifecycleOperation.START, "start-replay-1")

    assert first.replayed is False
    assert second.replayed is True
    assert calls == 2


@pytest.mark.parametrize(
    ("status", "owner_code", "expected"),
    [
        (400, "INVALID_REQUEST", TradingRunnerLifecycleTransportErrorCode.INVALID_REQUEST),
        (400, "PAPER_ONLY", TradingRunnerLifecycleTransportErrorCode.PAPER_ONLY),
        (
            409,
            "IDEMPOTENCY_CONFLICT",
            TradingRunnerLifecycleTransportErrorCode.IDEMPOTENCY_CONFLICT,
        ),
        (422, "INVALID_TRANSITION", TradingRunnerLifecycleTransportErrorCode.INVALID_TRANSITION),
        (503, "RECOVERY_REQUIRED", TradingRunnerLifecycleTransportErrorCode.RECOVERY_REQUIRED),
        (503, "OWNER_FAILURE", TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE),
    ],
)
def test_owner_error_taxonomy_is_preserved(
    status: int,
    owner_code: str,
    expected: TradingRunnerLifecycleTransportErrorCode,
) -> None:
    current = adapter(httpx.MockTransport(lambda _request: response({"error": owner_code}, status)))

    with pytest.raises(TradingRunnerLifecycleHttpError) as raised:
        current.mutate(TradingRunnerLifecycleOperation.START, "safe-error-1")

    assert raised.value.code is expected
    assert raised.value.status_code == status


def test_unauthorized_and_transport_failures_are_safe() -> None:
    unauthorized = adapter(
        httpx.MockTransport(lambda _request: response({"error": "UNAUTHORIZED"}, 401))
    )
    with pytest.raises(TradingRunnerLifecycleHttpError) as denied:
        unauthorized.mutate(TradingRunnerLifecycleOperation.START, "unauthorized-1")
    assert denied.value.code is TradingRunnerLifecycleTransportErrorCode.UNAUTHORIZED

    def failed(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "private transport path /Users/private/trading-runner.sqlite",
            request=request,
        )

    unreachable = adapter(httpx.MockTransport(failed))
    with pytest.raises(TradingRunnerLifecycleHttpError) as raised:
        unreachable.mutate(TradingRunnerLifecycleOperation.START, "unreachable-1")
    rendered = str(raised.value)
    assert raised.value.code is TradingRunnerLifecycleTransportErrorCode.UNREACHABLE
    assert "/Users/private" not in rendered
    assert READER not in rendered
    assert OPERATOR not in rendered


def test_response_contract_rejects_private_fields_and_identity_mismatch() -> None:
    private = result_payload(
        request_id="private-response",
        operation=TradingRunnerLifecycleOperation.START,
        previous_state="configured",
        new_state="running",
    )
    private["runtime_path"] = "/Users/private/runtime.json"
    current = adapter(httpx.MockTransport(lambda _request: response(private)))
    with pytest.raises(TradingRunnerLifecycleHttpError) as raised:
        current.mutate(TradingRunnerLifecycleOperation.START, "private-response")
    assert raised.value.code is TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE
    assert "/Users/private" not in str(raised.value)

    mismatch = adapter(
        httpx.MockTransport(
            lambda _request: response(
                result_payload(
                    request_id="different-request",
                    operation=TradingRunnerLifecycleOperation.START,
                    previous_state="configured",
                    new_state="running",
                )
            )
        )
    )
    with pytest.raises(TradingRunnerLifecycleHttpError) as mismatch_error:
        mismatch.mutate(TradingRunnerLifecycleOperation.START, "expected-request")
    assert mismatch_error.value.code is TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE


def test_invalid_request_id_and_oversized_response_fail_before_unsafe_use() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response({})

    current = adapter(httpx.MockTransport(handler))
    with pytest.raises(TradingRunnerLifecycleHttpError) as invalid:
        current.mutate(TradingRunnerLifecycleOperation.START, "bad request with spaces")
    assert invalid.value.code is TradingRunnerLifecycleTransportErrorCode.INVALID_REQUEST
    assert called is False

    oversized = adapter(
        httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"{" + b"x" * (64 * 1024) + b"}")
        )
    )
    with pytest.raises(TradingRunnerLifecycleHttpError) as too_large:
        oversized.mutate(TradingRunnerLifecycleOperation.START, "oversized-1")
    assert too_large.value.code is TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8780",
        "http://example.com:8780",
        "http://user:pass@127.0.0.1:8780",
        "http://127.0.0.1:8780/private",
        "http://127.0.0.1:8780?token=private",
    ],
)
def test_lifecycle_transport_rejects_non_loopback_or_decorated_urls(url: str) -> None:
    with pytest.raises(ValueError):
        TradingRunnerLifecycleHttpAdapter(
            url,
            reader_token=READER,
            operator_token=OPERATOR,
        )


def test_credentials_must_be_distinct_and_repr_is_secret_safe() -> None:
    with pytest.raises(ValueError, match="distinct"):
        TradingRunnerLifecycleHttpAdapter(
            "http://127.0.0.1:8780",
            reader_token=READER,
            operator_token=READER,
        )

    current = TradingRunnerLifecycleHttpAdapter(
        "http://127.0.0.1:8780",
        reader_token=READER,
        operator_token=OPERATOR,
    )
    rendered = repr(current)
    assert READER not in rendered
    assert OPERATOR not in rendered
