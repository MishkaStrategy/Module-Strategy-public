from __future__ import annotations

from datetime import UTC, datetime
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
    TrustedOperator,
)

NOW = datetime(2026, 8, 8, 21, 0, tzinfo=UTC)


def spec() -> AcceptanceRunSpec:
    return AcceptanceRunSpec(
        full_rc_sha="a" * 40,
        dataset_id="dataset-owner-e2e",
        dataset_digest="b" * 64,
        strategy_id="strategy-owner-e2e",
        strategy_digest="c" * 64,
        config_digest="d" * 64,
        enable_soak_24h=True,
        enable_soak_72h=True,
    )


def start(request_id: str) -> AcceptanceMutationRequest:
    return AcceptanceMutationRequest(
        request_id=request_id,
        operator=TrustedOperator("control-plane:operator"),
        mutation=AcceptanceMutation.START,
        spec=spec(),
    )


class SuccessExecutor:
    def __init__(self) -> None:
        self.phases: list[AcceptancePhase] = []
        self.attempt_ids: list[str] = []

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.phases.append(context.phase)
        self.attempt_ids.append(context.attempt_id)
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED, active_seconds=1.0)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        raise AssertionError("clean end-to-end run must not recover")


def make_owner(
    path: Path,
    executor: SuccessExecutor,
    *,
    instance_id: str,
) -> tuple[SystemAcceptanceOwner, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id=instance_id,
        clock=lambda: NOW,
        lease_seconds=5,
    )
    return owner, store


def test_full_enabled_phase_graph_reaches_success_with_one_run_id(tmp_path: Path) -> None:
    executor = SuccessExecutor()
    owner, store = make_owner(tmp_path / "acceptance.db", executor, instance_id="e2e-owner")
    started = owner.mutate(start("full-run"))

    snapshots = []
    for _ in spec().phases:
        current = owner.step()
        assert current is not None
        snapshots.append(current)

    final = snapshots[-1]
    assert final.acceptance_run_id == started.acceptance_run_id
    assert final.status is AcceptanceStatus.SUCCEEDED
    assert final.phase is AcceptancePhase.FINAL_VERIFICATION
    assert final.completed_at == NOW
    assert final.active_test_time == float(len(spec().phases))
    assert executor.phases == list(spec().phases)
    assert len(set(executor.attempt_ids)) == len(spec().phases)
    assert all(snapshot.acceptance_run_id == started.acceptance_run_id for snapshot in snapshots)
    store.close()


def test_failed_start_mutation_replays_after_restart_without_second_run(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    executor = SuccessExecutor()
    owner, store = make_owner(path, executor, instance_id="first-owner")
    active = owner.mutate(start("first-start"))
    blocked_request = start("second-start")

    with pytest.raises(AcceptanceOwnerError) as first_failure:
        owner.mutate(blocked_request)
    assert first_failure.value.code is AcceptanceErrorCode.RUN_ALREADY_ACTIVE
    store.close()

    restarted, restarted_store = make_owner(path, executor, instance_id="second-owner")
    with pytest.raises(AcceptanceOwnerError) as replay_failure:
        restarted.mutate(blocked_request)

    assert replay_failure.value.code is AcceptanceErrorCode.RUN_ALREADY_ACTIVE
    snapshot = restarted.snapshot(active.acceptance_run_id)
    assert snapshot is not None
    assert snapshot.acceptance_run_id == active.acceptance_run_id
    assert snapshot.status is AcceptanceStatus.QUEUED
    restarted_store.close()
