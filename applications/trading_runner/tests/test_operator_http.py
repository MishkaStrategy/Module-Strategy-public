from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from test_runtime import build_runner

from modular_trading_runner import (
    TradingRunnerLifecycleHttpServer,
    TradingRunnerOperatorLifecycle,
)

READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"


def build_server():  # type: ignore[no-untyped-def]
    runner, calls, executor = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    server = TradingRunnerLifecycleHttpServer(
        owner,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
    )
    return server, runner, calls, executor


def decode(body: bytes) -> dict[str, object]:
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def test_transport_rejects_remote_bind_equal_tokens_and_closes_before_start() -> None:
    server, _, _, _ = build_server()
    server.close()

    runner, _, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    with pytest.raises(ValueError, match="loopback"):
        TradingRunnerLifecycleHttpServer(
            owner,
            reader_token=READER,
            operator_token=OPERATOR,
            host="0.0.0.0",
            port=0,
        )
    with pytest.raises(ValueError, match="distinct"):
        TradingRunnerLifecycleHttpServer(
            owner,
            reader_token="same-token-012345",
            operator_token="same-token-012345",
            port=0,
        )


def test_reader_token_cannot_mutate_and_operator_duplicate_replays_once() -> None:
    server, _, calls, _ = build_server()
    body = b'{"request_id":"transport-start"}'
    try:
        denied_code, _, denied_body = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {READER}",
            body=body,
        )
        assert denied_code == 401
        assert decode(denied_body) == {"error": "UNAUTHORIZED"}
        assert calls.count("market:start") == 0

        first_code, _, first_body = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=body,
        )
        replay_code, _, replay_body = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=body,
        )
        first = decode(first_body)
        replay = decode(replay_body)

        assert first_code == replay_code == 200
        assert first["operator"] == replay["operator"] == "control-plane:local-operator"
        assert first["operation"] == replay["operation"] == "START"
        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert calls.count("market:start") == 1
        assert READER not in repr(server)
        assert OPERATOR not in repr(server)
    finally:
        server.close()


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"request_id":"x","reason":"browser reason"}',
        b'{"request_id":"x","operator":"browser"}',
        b'{"request_id":1}',
        b"not-json",
    ],
)
def test_transport_rejects_arbitrary_reason_operator_and_malformed_payload(body: bytes) -> None:
    server, _, calls, _ = build_server()
    try:
        code, _, response_body = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=body,
        )
        assert code == 400
        assert decode(response_body) == {"error": "INVALID_REQUEST"}
        assert calls.count("market:start") == 0
    finally:
        server.close()


def test_operator_transport_start_stop_kill_and_reset() -> None:
    server, runner, calls, executor = build_server()
    try:
        start_code, _, _ = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=b'{"request_id":"start-e2e"}',
        )
        kill_code, _, _ = server.response(
            method="POST",
            path="/lifecycle/v1/kill-switch/activate",
            authorization=f"Bearer {OPERATOR}",
            body=b'{"request_id":"kill-e2e"}',
        )
        reset_code, _, _ = server.response(
            method="POST",
            path="/lifecycle/v1/kill-switch/reset",
            authorization=f"Bearer {OPERATOR}",
            body=b'{"request_id":"reset-e2e"}',
        )
        stop_code, _, stop_body = server.response(
            method="POST",
            path="/lifecycle/v1/stop",
            authorization=f"Bearer {OPERATOR}",
            body=b'{"request_id":"stop-e2e"}',
        )

        assert (start_code, kill_code, reset_code, stop_code) == (200, 200, 200, 200)
        assert decode(stop_body)["new_state"] == "stopped"
        assert runner.state.value == "stopped"
        assert executor.cancel_calls == 1
        assert calls.count("market:start") == 1
        assert calls.count("market:stop") == 1
    finally:
        server.close()


def test_real_loopback_transport_preserves_operator_boundary() -> None:
    server, _, calls, _ = build_server()
    server.start()
    host, port = server.address
    base = f"http://{host}:{port}"
    try:
        denied = urllib.request.Request(
            f"{base}/lifecycle/v1/start",
            data=b'{"request_id":"reader-http"}',
            headers={"Authorization": f"Bearer {READER}"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(denied, timeout=2)
        assert raised.value.code == 401
        assert calls.count("market:start") == 0

        allowed = urllib.request.Request(
            f"{base}/lifecycle/v1/start",
            data=b'{"request_id":"operator-http"}',
            headers={"Authorization": f"Bearer {OPERATOR}"},
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=5) as response:
            assert response.status == 200
            assert decode(response.read())["new_state"] == "running"
        assert calls.count("market:start") == 1
    finally:
        server.close()
