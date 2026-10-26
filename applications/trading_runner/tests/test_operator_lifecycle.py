from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from test_runtime import Store, build_runner, config

from modular_trading_runner import (
    LifecycleMutationError,
    LifecycleMutationErrorCode,
    LifecycleMutationRequest,
    LifecycleOperation,
    RuntimeState,
    TradingRunnerOperatorLifecycle,
    TrustedOperatorIdentity,
)


def request(request_id: str, operation: LifecycleOperation) -> LifecycleMutationRequest:
    return LifecycleMutationRequest(
        request_id=request_id,
        operator=TrustedOperatorIdentity("control-plane:operator"),
        operation=operation,
    )


@pytest.mark.asyncio
async def test_start_exact_duplicate_replays_without_second_side_effect() -> None:
    runner, calls, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    mutation = request("start-001", LifecycleOperation.START)

    first = await owner.execute(mutation)
    second = await owner.execute(mutation)

    assert first.replayed is False
    assert second.replayed is True
    assert first.request_id == second.request_id
    assert first.completed_at == second.completed_at
    assert first.previous_state is RuntimeState.CREATED
    assert first.new_state is RuntimeState.RUNNING
    assert calls.count("market:start") == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_has_one_authoritative_execution() -> None:
    runner, calls, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    mutation = request("start-concurrent", LifecycleOperation.START)

    results = await asyncio.gather(owner.execute(mutation), owner.execute(mutation))

    assert sum(result.replayed for result in results) == 1
    assert calls.count("market:start") == 1


@pytest.mark.asyncio
async def test_same_request_id_with_different_operation_conflicts() -> None:
    runner, calls, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("same-id", LifecycleOperation.START))

    with pytest.raises(LifecycleMutationError) as raised:
        await owner.execute(request("same-id", LifecycleOperation.STOP))

    assert raised.value.code is LifecycleMutationErrorCode.IDEMPOTENCY_CONFLICT
    assert runner.state is RuntimeState.RUNNING
    assert calls.count("market:stop") == 0


@pytest.mark.asyncio
async def test_same_request_id_with_different_operator_conflicts() -> None:
    runner, _, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    first = request("operator-conflict", LifecycleOperation.START)
    await owner.execute(first)
    second = LifecycleMutationRequest(
        request_id="operator-conflict",
        operator=TrustedOperatorIdentity("control-plane:other-operator"),
        operation=LifecycleOperation.START,
    )

    with pytest.raises(LifecycleMutationError) as raised:
        await owner.execute(second)

    assert raised.value.code is LifecycleMutationErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_stop_exact_duplicate_replays_without_second_shutdown() -> None:
    runner, calls, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("start-before-stop", LifecycleOperation.START))
    mutation = request("stop-001", LifecycleOperation.STOP)

    first = await owner.execute(mutation)
    second = await owner.execute(mutation)

    assert first.new_state is RuntimeState.STOPPED
    assert second.replayed is True
    assert calls.count("market:stop") == 1
    assert calls.count("checkpoint") == 1


@pytest.mark.asyncio
async def test_duplicate_kill_switch_does_not_recancel_or_rewrite_activation() -> None:
    runner, _, executor = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("start-before-kill", LifecycleOperation.START))
    mutation = request("kill-001", LifecycleOperation.ACTIVATE_KILL_SWITCH)

    first = await owner.execute(mutation)
    activated_at = runner.kill_switch.activated_at
    second = await owner.execute(mutation)

    assert first.new_state is RuntimeState.KILL_SWITCH_ACTIVE
    assert second.replayed is True
    assert executor.cancel_calls == 1
    assert runner.kill_switch.activated_at == activated_at
    assert runner.kill_switch.reason == "operator_control"


@pytest.mark.asyncio
async def test_new_kill_request_while_active_is_native_noop() -> None:
    runner, _, executor = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("start-before-new-kill", LifecycleOperation.START))
    await owner.execute(request("kill-first", LifecycleOperation.ACTIVATE_KILL_SWITCH))
    activated_at = runner.kill_switch.activated_at

    result = await owner.execute(request("kill-second", LifecycleOperation.ACTIVATE_KILL_SWITCH))

    assert result.previous_state is RuntimeState.KILL_SWITCH_ACTIVE
    assert result.new_state is RuntimeState.KILL_SWITCH_ACTIVE
    assert executor.cancel_calls == 1
    assert runner.kill_switch.activated_at == activated_at


@pytest.mark.asyncio
async def test_reset_exact_duplicate_replays_without_second_reconciliation() -> None:
    runner, _, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("start-before-reset", LifecycleOperation.START))
    await owner.execute(request("kill-before-reset", LifecycleOperation.ACTIVATE_KILL_SWITCH))
    reconciliation = runner.reconciliation
    before = reconciliation.calls
    mutation = request("reset-001", LifecycleOperation.RESET_KILL_SWITCH)

    first = await owner.execute(mutation)
    second = await owner.execute(mutation)

    assert first.new_state is RuntimeState.RUNNING
    assert second.replayed is True
    assert reconciliation.calls == before + 1


@pytest.mark.asyncio
async def test_completed_request_replays_after_process_restart_without_side_effect() -> None:
    store = Store()
    runner, calls, _ = build_runner(store=store)
    owner = TradingRunnerOperatorLifecycle(runner)
    mutation = request("restart-replay", LifecycleOperation.START)
    first = await owner.execute(mutation)
    assert calls.count("market:start") == 1

    restarted_runner, restarted_calls, _ = build_runner(store=store)
    restarted_owner = TradingRunnerOperatorLifecycle(restarted_runner)
    replay = await restarted_owner.execute(mutation)

    assert replay.replayed is True
    assert replay.completed_at == first.completed_at
    assert replay.new_state is RuntimeState.RUNNING
    assert restarted_calls.count("market:start") == 0


@pytest.mark.asyncio
async def test_in_progress_restart_fails_closed_without_reexecuting_side_effect() -> None:
    current_config = config()
    store = Store(
        {
            "runner-1:operator-lifecycle:v1": {
                "schema_version": "1",
                "runtime_id": "runner-1",
                "config_hash": current_config.canonical_hash,
                "records": [
                    {
                        "request_id": "uncertain-start",
                        "operator": "control-plane:operator",
                        "operation": "START",
                        "status": "IN_PROGRESS",
                        "previous_state": "created",
                        "new_state": None,
                        "created_at": "2026-07-26T00:00:00+00:00",
                        "completed_at": None,
                        "error_code": None,
                    }
                ],
            }
        }
    )
    runner, calls, _ = build_runner(store=store)
    owner = TradingRunnerOperatorLifecycle(runner)

    with pytest.raises(LifecycleMutationError) as raised:
        await owner.execute(request("uncertain-start", LifecycleOperation.START))

    assert raised.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert calls.count("market:start") == 0


class FailOnSaveStore(Store):
    def __init__(self, *, fail_on_call: int) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call
        self.save_calls = 0

    def save(self, key: str, value: object) -> None:
        self.save_calls += 1
        if self.save_calls == self.fail_on_call:
            raise OSError("simulated storage failure")
        super().save(key, value)


@pytest.mark.asyncio
async def test_terminal_persistence_failure_leaves_replay_recovery_required() -> None:
    store = FailOnSaveStore(fail_on_call=2)
    runner, calls, _ = build_runner(store=store)
    owner = TradingRunnerOperatorLifecycle(runner)
    mutation = request("uncertain-terminal", LifecycleOperation.START)

    with pytest.raises(LifecycleMutationError) as first_error:
        await owner.execute(mutation)
    assert first_error.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert calls.count("market:start") == 1

    with pytest.raises(LifecycleMutationError) as replay_error:
        await owner.execute(mutation)
    assert replay_error.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert calls.count("market:start") == 1


@pytest.mark.asyncio
async def test_partial_owner_failure_is_durable_and_not_retried() -> None:
    runner, calls, _ = build_runner(ready=False)
    owner = TradingRunnerOperatorLifecycle(runner)
    mutation = request("failed-start", LifecycleOperation.START)

    with pytest.raises(LifecycleMutationError) as first_error:
        await owner.execute(mutation)
    call_snapshot = tuple(calls)
    with pytest.raises(LifecycleMutationError) as replay_error:
        await owner.execute(mutation)

    assert first_error.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert replay_error.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert tuple(calls) == call_snapshot


@pytest.mark.asyncio
async def test_start_from_stopped_is_deterministic_invalid_transition() -> None:
    runner, _, _ = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("start-before-terminal", LifecycleOperation.START))
    await owner.execute(request("stop-before-terminal", LifecycleOperation.STOP))

    with pytest.raises(LifecycleMutationError) as raised:
        await owner.execute(request("start-after-stop", LifecycleOperation.START))

    assert raised.value.code is LifecycleMutationErrorCode.INVALID_TRANSITION


def test_malformed_and_oversized_identity_fields_fail_closed() -> None:
    with pytest.raises(LifecycleMutationError) as request_error:
        LifecycleMutationRequest(
            request_id="x" * 129,
            operator=TrustedOperatorIdentity("control-plane:operator"),
            operation=LifecycleOperation.START,
        )
    assert request_error.value.code is LifecycleMutationErrorCode.INVALID_REQUEST

    with pytest.raises(LifecycleMutationError) as operator_error:
        TrustedOperatorIdentity("bad operator with spaces")
    assert operator_error.value.code is LifecycleMutationErrorCode.INVALID_REQUEST

    with pytest.raises(LifecycleMutationError) as raw_operator_error:
        LifecycleMutationRequest(
            request_id="typed-boundary",
            operator="reader-token",  # type: ignore[arg-type]
            operation=LifecycleOperation.START,
        )
    assert raw_operator_error.value.code is LifecycleMutationErrorCode.INVALID_REQUEST


def test_stale_idempotency_configuration_fails_closed_on_owner_construction() -> None:
    store = Store(
        {
            "runner-1:operator-lifecycle:v1": {
                "schema_version": "1",
                "runtime_id": "runner-1",
                "config_hash": "f" * 64,
                "records": [],
            }
        }
    )
    runner, _, _ = build_runner(store=store)

    with pytest.raises(LifecycleMutationError) as raised:
        TradingRunnerOperatorLifecycle(runner)

    assert raised.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED


class RecordingJournal:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> None:
        self.entries.append(dict(kwargs))


@pytest.mark.asyncio
async def test_operator_audit_is_bounded_and_privacy_safe() -> None:
    runner, _, _ = build_runner()
    journal = RecordingJournal()
    runner.journal = journal
    owner = TradingRunnerOperatorLifecycle(runner)
    await owner.execute(request("audit-start", LifecycleOperation.START))
    await owner.execute(request("audit-kill", LifecycleOperation.ACTIVATE_KILL_SWITCH))

    operator_entries = [
        entry for entry in journal.entries if entry.get("event_type") == "operator.lifecycle"
    ]
    assert operator_entries
    allowed = {
        "operation",
        "resource",
        "request_id",
        "operator",
        "result",
        "previous_state",
        "new_state",
        "config_hash",
        "replayed",
        "error_code",
    }
    for entry in operator_entries:
        payload = entry["payload"]
        assert isinstance(payload, dict)
        assert set(payload) <= allowed
        assert payload["resource"] == "runner-1"
        assert payload["config_hash"] == runner.config.canonical_hash
        text = repr(payload).lower()
        assert "telegram" not in text
        assert "secret" not in text
        assert "/home/" not in text
        assert "/users/" not in text
        assert "operator_control" not in text


class SecretFailingStore(Store):
    def load(self, key: str) -> dict[str, object] | None:
        if key.endswith(":operator-lifecycle:v1"):
            raise OSError("PRIVATE_PATH_SENTINEL CREDENTIAL_SENTINEL")
        return super().load(key)


def test_storage_exception_details_are_suppressed() -> None:
    runner, _, _ = build_runner(store=SecretFailingStore())

    with pytest.raises(LifecycleMutationError) as raised:
        TradingRunnerOperatorLifecycle(runner)

    assert raised.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    text = str(raised.value).lower()
    assert "private_path_sentinel" not in text
    assert "credential_sentinel" not in text


class SecretFailingJournal:
    def append(self, **kwargs: Any) -> None:
        raise RuntimeError("PRIVATE_PATH_SENTINEL CREDENTIAL_SENTINEL")


@pytest.mark.asyncio
async def test_audit_failure_is_safe_and_prevents_unaudited_side_effect() -> None:
    runner, calls, _ = build_runner()
    runner.journal = SecretFailingJournal()
    owner = TradingRunnerOperatorLifecycle(runner)

    with pytest.raises(LifecycleMutationError) as raised:
        await owner.execute(request("audit-failure", LifecycleOperation.START))

    assert raised.value.code is LifecycleMutationErrorCode.RECOVERY_REQUIRED
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert calls.count("market:start") == 0
    text = str(raised.value).lower()
    assert "private_path_sentinel" not in text
    assert "credential_sentinel" not in text


def test_runtime_identity_cannot_be_private_path_or_free_form_text() -> None:
    runner, _, _ = build_runner()
    runner.config = replace(runner.config, runtime_id="/Users/private/runtime")

    with pytest.raises(LifecycleMutationError) as raised:
        TradingRunnerOperatorLifecycle(runner)

    assert raised.value.code is LifecycleMutationErrorCode.OWNER_FAILURE
    assert "/Users/private/runtime" not in str(raised.value)
