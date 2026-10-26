from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
)
from modular_system_acceptance.native_executor import NativeOwnerPhaseExecutor

NOW = datetime(2026, 8, 9, 1, 30, tzinfo=UTC)


def context(phase: AcceptancePhase, *, recovering: bool = False) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-native-test",
        phase=phase,
        attempt_id="attempt-native-test",
        recovering=recovering,
        full_rc_sha="a" * 40,
        dataset_id="dataset-v1",
        dataset_digest="b" * 64,
        strategy_id="strategy-v1",
        strategy_digest="c" * 64,
        config_digest="d" * 64,
        active_test_time=0.0,
        checkpoint_token=None,
        checkpoint_digest=None,
    )


def test_execute_and_recover_keep_same_typed_phase_context() -> None:
    seen: list[tuple[str, bool]] = []

    def execute(value: PhaseExecutionContext) -> PhaseOutcome:
        seen.append((value.attempt_id, value.recovering))
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(value: PhaseExecutionContext) -> PhaseOutcome:
        seen.append((value.attempt_id, value.recovering))
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    executor = NativeOwnerPhaseExecutor(
        AcceptancePhase.DATASET,
        execute=execute,
        recover=recover,
    )
    executor.execute(context(AcceptancePhase.DATASET))
    executor.recover(context(AcceptancePhase.DATASET, recovering=True))

    assert seen == [("attempt-native-test", False), ("attempt-native-test", True)]


def test_wrong_phase_is_blocked_before_callback() -> None:
    calls = 0

    def callback(value: PhaseExecutionContext) -> PhaseOutcome:
        nonlocal calls
        calls += 1
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    executor = NativeOwnerPhaseExecutor(
        AcceptancePhase.RESEARCH,
        execute=callback,
        recover=callback,
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        executor.execute(context(AcceptancePhase.DATASET))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert calls == 0


def test_raw_native_exception_is_reduced_to_safe_owner_error() -> None:
    def failure(value: PhaseExecutionContext) -> PhaseOutcome:
        raise RuntimeError("token=TOPSECRET /Users/private/evidence")

    executor = NativeOwnerPhaseExecutor(
        AcceptancePhase.RUNTIME_SMOKE,
        execute=failure,
        recover=failure,
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        executor.execute(context(AcceptancePhase.RUNTIME_SMOKE))

    assert raised.value.code is AcceptanceErrorCode.OWNER_FAILURE
    assert raised.value.__cause__ is None
    text = str(raised.value).lower()
    assert "topsecret" not in text
    assert "/users/" not in text


def test_invalid_native_return_is_phase_failure() -> None:
    executor = NativeOwnerPhaseExecutor(
        AcceptancePhase.FINAL_VERIFICATION,
        execute=lambda _: "bad",  # type: ignore[arg-type,return-value]
        recover=lambda _: "bad",  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        executor.execute(context(AcceptancePhase.FINAL_VERIFICATION))
    assert raised.value.code is AcceptanceErrorCode.PHASE_FAILURE
