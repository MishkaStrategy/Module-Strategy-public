from __future__ import annotations

import asyncio
import json
import time

from test_runtime import build_runner

from modular_trading_runner import (
    LifecycleMutationRequest,
    LifecycleMutationResult,
    TradingRunnerLifecycleHttpServer,
    TradingRunnerOperatorLifecycle,
)

OPERATOR = "operator-token-0123456789"
READER = "reader-token-0123456789"


class DelayedFirstOwner(TradingRunnerOperatorLifecycle):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.transport_calls = 0

    async def execute(self, request: LifecycleMutationRequest) -> LifecycleMutationResult:
        self.transport_calls += 1
        if self.transport_calls == 1:
            await asyncio.sleep(0.2)
        return await super().execute(request)


def decode(body: bytes) -> dict[str, object]:
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def test_retry_after_uncertain_transport_result_replays_without_second_start() -> None:
    runner, calls, _ = build_runner()
    owner = DelayedFirstOwner(runner)
    server = TradingRunnerLifecycleHttpServer(
        owner,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
        mutation_timeout_seconds=0.1,
    )
    body = b'{"request_id":"uncertain-http-start"}'
    try:
        first_code, _, first_body = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=body,
        )
        assert first_code == 503
        assert decode(first_body) == {
            "error": "RECOVERY_REQUIRED",
            "request_id": "uncertain-http-start",
        }

        deadline = time.monotonic() + 2
        while calls.count("market:start") == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls.count("market:start") == 1

        retry_code, _, retry_body = server.response(
            method="POST",
            path="/lifecycle/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=body,
        )
        retry = decode(retry_body)
        assert retry_code == 200
        assert retry["replayed"] is True
        assert retry["new_state"] == "running"
        assert calls.count("market:start") == 1
    finally:
        server.close()
