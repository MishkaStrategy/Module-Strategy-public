from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceExecutorRegistry,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceRunSpec,
    AcceptanceStatus,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    SQLiteAcceptanceStore,
    SystemAcceptanceOwner,
    SystemAcceptanceQuery,
    TrustedOperator,
)

NOW = datetime(2026, 8, 8, 19, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def run_spec() -> AcceptanceRunSpec:
    return AcceptanceRunSpec(
        full_rc_sha="1" * 40,
        dataset_id="dataset-v1",
        dataset_digest="2" * 64,
        strategy_id="strategy-v1",
        strategy_digest="3" * 64,
        config_digest="4" * 64,
    )


def start_request(request_id: str = "start") -> AcceptanceMutationRequest:
    return AcceptanceMutationRequest(
        request_id=request_id,
        operator=TrustedOperator("control-plane:operator"),
        mutation=AcceptanceMutation.START,
        spec=run_spec(),
    )


class RecoverBlockedExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.recovered: list[str] = []

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.executed.append(context.attempt_id)
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.recovered.append(context.attempt_id)
        return PhaseOutcome(
            status=PhaseOutcomeStatus.BLOCKED,
            error_code="STORAGE_RECOVERY_REQUIRED",
            next_action="OWNER_REVIEW",
        )


def registry(executor: object) -> AcceptanceExecutorRegistry:
    return AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase})


class FailCompletionStore(SQLiteAcceptanceStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.save_calls = 0
        self.fail_on_call = 3

    def save_run(self, stored, *, connection):  # type: ignore[no-untyped-def,override]
        self.save_calls += 1
        if self.save_calls == self.fail_on_call:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance store write failed",
                run_id=stored.snapshot.acceptance_run_id,
            )
        return super().save_run(stored, connection=connection)


def test_phase_completion_storage_failure_recovers_same_attempt_to_blocked(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    clock = Clock()
    executor = RecoverBlockedExecutor()
    failing = FailCompletionStore(path)
    first = SystemAcceptanceOwner(
        failing,
        registry(executor),
        instance_id="before-storage-failure",
        clock=clock.now,
        lease_seconds=5,
    )
    started = first.mutate(start_request("storage-failure"))

    with pytest.raises(AcceptanceOwnerError) as raised:
        first.step()
    assert raised.value.code is AcceptanceErrorCode.STORAGE_FAILURE
    assert len(executor.executed) == 1
    original_attempt = executor.executed[0]
    failing.close()

    clock.advance(6)
    reopened_store = SQLiteAcceptanceStore(path)
    reopened = SystemAcceptanceOwner(
        reopened_store,
        registry(executor),
        instance_id="after-storage-failure",
        clock=clock.now,
        lease_seconds=5,
    )
    recovered = reopened.recover_after_restart()
    assert recovered is not None
    assert recovered.acceptance_run_id == started.acceptance_run_id
    final = reopened.step()

    assert final is not None
    assert final.status is AcceptanceStatus.BLOCKED
    assert final.error == "STORAGE_RECOVERY_REQUIRED"
    assert executor.recovered == [original_attempt]
    assert len(executor.executed) == 1
    reopened_store.close()


def test_audit_persists_operator_request_status_without_private_values(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    clock = Clock()
    executor = RecoverBlockedExecutor()
    store = SQLiteAcceptanceStore(path)
    current = SystemAcceptanceOwner(
        store,
        registry(executor),
        instance_id="audit-owner",
        clock=clock.now,
        lease_seconds=5,
    )
    start = current.mutate(start_request("audit-start"))
    current.mutate(
        AcceptanceMutationRequest(
            request_id="audit-cancel",
            operator=TrustedOperator("control-plane:operator"),
            mutation=AcceptanceMutation.CANCEL,
            run_id=start.acceptance_run_id,
        )
    )

    entries = SystemAcceptanceQuery(store).audit_entries()

    assert [entry.mutation for entry in entries[:2]] == [
        AcceptanceMutation.CANCEL,
        AcceptanceMutation.START,
    ]
    assert all(entry.operator == "control-plane:operator" for entry in entries[:2])
    text = repr(entries).lower()
    assert "token=" not in text
    assert "/users/" not in text
    assert "environment" in text
    store.close()


def test_raw_operator_and_live_mode_are_rejected_before_owner_mutation() -> None:
    with pytest.raises(AcceptanceOwnerError) as raw_operator:
        AcceptanceMutationRequest(
            request_id="raw-reader",
            operator="reader-token",  # type: ignore[arg-type]
            mutation=AcceptanceMutation.START,
            spec=run_spec(),
        )
    assert raw_operator.value.code is AcceptanceErrorCode.INVALID_REQUEST

    with pytest.raises(AcceptanceOwnerError) as live:
        AcceptanceRunSpec(
            full_rc_sha="1" * 40,
            dataset_id="dataset-v1",
            dataset_digest="2" * 64,
            strategy_id="strategy-v1",
            strategy_digest="3" * 64,
            config_digest="4" * 64,
            mode="LIVE",
        )
    assert live.value.code is AcceptanceErrorCode.PAPER_ONLY
