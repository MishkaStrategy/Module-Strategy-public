from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_system_acceptance import (
    AcceptanceCheckpoint,
    AcceptanceErrorCode,
    AcceptanceExecutorRegistry,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceRunSpec,
    AcceptanceStatus,
    EvidenceReference,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    ResourceSample,
    SQLiteAcceptanceStore,
    SystemAcceptanceOwner,
    TrustedOperator,
)

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def spec(*, soak24: bool = False, soak72: bool = False) -> AcceptanceRunSpec:
    return AcceptanceRunSpec(
        full_rc_sha="a" * 40,
        dataset_id="btc-usdtm-5m-v1",
        dataset_digest="b" * 64,
        strategy_id="strategy-candidate-v2",
        strategy_digest="c" * 64,
        config_digest="d" * 64,
        enable_soak_24h=soak24,
        enable_soak_72h=soak72,
    )


def mutation(
    request_id: str,
    operation: AcceptanceMutation,
    *,
    run_id: str | None = None,
    run_spec: AcceptanceRunSpec | None = None,
) -> AcceptanceMutationRequest:
    return AcceptanceMutationRequest(
        request_id=request_id,
        operator=TrustedOperator("control-plane:local-operator"),
        mutation=operation,
        run_id=run_id,
        spec=run_spec,
    )


class Executor:
    def __init__(self, outcomes: list[PhaseOutcome] | None = None) -> None:
        self.outcomes = outcomes or [PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)]
        self.execute_attempts: list[str] = []
        self.recover_attempts: list[str] = []

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.execute_attempts.append(context.attempt_id)
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.recover_attempts.append(context.attempt_id)
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


def registry(executor: object) -> AcceptanceExecutorRegistry:
    return AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase})


def owner(
    path: Path,
    executor: object,
    clock: Clock,
    *,
    instance: str = "acceptance-owner-1",
) -> tuple[SystemAcceptanceOwner, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    return (
        SystemAcceptanceOwner(
            store,
            registry(executor),
            instance_id=instance,
            clock=clock.now,
            lease_seconds=5,
        ),
        store,
    )


def test_start_exact_duplicate_is_durable_replay(tmp_path: Path) -> None:
    clock = Clock()
    executor = Executor()
    first_owner, first_store = owner(tmp_path / "acceptance.db", executor, clock)
    request = mutation("start-001", AcceptanceMutation.START, run_spec=spec())

    first = first_owner.mutate(request)
    replay = first_owner.mutate(request)

    assert first.replayed is False
    assert replay.replayed is True
    assert first.acceptance_run_id == replay.acceptance_run_id
    assert first.status is AcceptanceStatus.QUEUED
    first_store.close()

    restarted, restarted_store = owner(
        tmp_path / "acceptance.db",
        executor,
        clock,
        instance="acceptance-owner-2",
    )
    after_restart = restarted.mutate(request)
    assert after_restart.replayed is True
    assert after_restart.acceptance_run_id == first.acceptance_run_id
    restarted_store.close()


def test_same_request_different_payload_conflicts(tmp_path: Path) -> None:
    clock = Clock()
    executor = Executor()
    current, store = owner(tmp_path / "acceptance.db", executor, clock)
    current.mutate(mutation("same-id", AcceptanceMutation.START, run_spec=spec()))

    changed = AcceptanceRunSpec(
        full_rc_sha="a" * 40,
        dataset_id="btc-usdtm-5m-v2",
        dataset_digest="e" * 64,
        strategy_id="strategy-candidate-v2",
        strategy_digest="c" * 64,
        config_digest="d" * 64,
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        current.mutate(mutation("same-id", AcceptanceMutation.START, run_spec=changed))

    assert raised.value.code is AcceptanceErrorCode.IDEMPOTENCY_CONFLICT
    store.close()


def test_two_owner_instances_concurrent_duplicate_create_one_run(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    clock = Clock()
    executor = Executor()
    first, first_store = owner(path, executor, clock, instance="owner-a")
    second, second_store = owner(path, executor, clock, instance="owner-b")
    request = mutation("concurrent-start", AcceptanceMutation.START, run_spec=spec())
    barrier = threading.Barrier(2)
    results: list[object] = []

    def call(current: SystemAcceptanceOwner) -> None:
        barrier.wait(timeout=5)
        try:
            results.append(current.mutate(request))
        except BaseException as exc:
            results.append(exc)

    threads = [
        threading.Thread(target=call, args=(first,)),
        threading.Thread(target=call, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert not any(isinstance(result, BaseException) for result in results)
    run_ids = {
        result.acceptance_run_id for result in results if not isinstance(result, BaseException)
    }
    assert len(run_ids) == 1
    assert sum(result.replayed for result in results if not isinstance(result, BaseException)) == 1
    first_store.close()
    second_store.close()


def test_browser_independent_progress_survives_owner_reopen(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    clock = Clock()
    executor = Executor()
    first, first_store = owner(path, executor, clock)
    started = first.mutate(mutation("browser-close", AcceptanceMutation.START, run_spec=spec()))
    first_store.close()

    reopened, reopened_store = owner(path, executor, clock, instance="owner-after-browser")
    progressed = reopened.step()

    assert progressed is not None
    assert progressed.acceptance_run_id == started.acceptance_run_id
    assert progressed.phase is AcceptancePhase.DATASET
    assert progressed.status is AcceptanceStatus.RUNNING
    reopened_store.close()


def test_active_test_time_is_not_wall_clock_time(tmp_path: Path) -> None:
    clock = Clock()
    checkpoint = AcceptanceCheckpoint(
        phase=AcceptancePhase.ENVIRONMENT,
        updated_at=NOW,
        token="environment-slice-1",
        digest="e" * 64,
    )
    executor = Executor(
        [
            PhaseOutcome(
                status=PhaseOutcomeStatus.RUNNING,
                active_seconds=5.0,
                checkpoint=checkpoint,
                resources=(
                    ResourceSample(
                        timestamp=NOW,
                        cpu_percent=25.0,
                        ram_bytes=1_000_000,
                        disk_free_bytes=2_000_000,
                    ),
                ),
            ),
            PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED, active_seconds=2.0),
        ]
    )
    current, store = owner(tmp_path / "acceptance.db", executor, clock)
    current.mutate(mutation("active-time", AcceptanceMutation.START, run_spec=spec()))

    clock.advance(100)
    first = current.step()
    assert first is not None
    assert first.active_test_time == 5.0
    assert first.wall_clock_elapsed == 100.0
    assert first.phase is AcceptancePhase.ENVIRONMENT

    clock.advance(200)
    second = current.step()
    assert second is not None
    assert second.active_test_time == 7.0
    assert second.wall_clock_elapsed == 300.0
    assert second.phase is AcceptancePhase.DATASET
    store.close()


class CrashExecutor:
    def __init__(self) -> None:
        self.execute_attempt: str | None = None
        self.recover_attempt: str | None = None

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.execute_attempt = context.attempt_id
        raise SystemExit("simulated process crash")

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.recover_attempt = context.attempt_id
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED, active_seconds=1.0)


def test_process_restart_recovers_same_attempt_without_duplicate_execute(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    clock = Clock()
    executor = CrashExecutor()
    first, first_store = owner(path, executor, clock, instance="owner-before-crash")
    first.mutate(mutation("restart-run", AcceptanceMutation.START, run_spec=spec()))

    with pytest.raises(SystemExit):
        first.step()
    assert executor.execute_attempt is not None
    first_store.close()

    clock.advance(6)
    recovered, recovered_store = owner(path, executor, clock, instance="owner-after-crash")
    recovery_snapshot = recovered.recover_after_restart()
    assert recovery_snapshot is not None
    assert recovery_snapshot.status is AcceptanceStatus.RECOVERING
    assert recovery_snapshot.restart_count == 1

    progressed = recovered.step()
    assert progressed is not None
    assert executor.recover_attempt == executor.execute_attempt
    assert progressed.phase is AcceptancePhase.DATASET
    assert progressed.active_test_time == 1.0
    recovered_store.close()


def test_stop_during_active_slice_cancels_at_safe_boundary(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.db"
    clock = Clock()
    entered = threading.Event()
    release = threading.Event()

    class BlockingExecutor:
        def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
            entered.set()
            assert release.wait(timeout=5)
            return PhaseOutcome(
                status=PhaseOutcomeStatus.RUNNING,
                active_seconds=1.0,
                checkpoint=AcceptanceCheckpoint(
                    phase=context.phase,
                    updated_at=clock.now(),
                    token="safe-stop-boundary",
                    digest="f" * 64,
                ),
            )

        def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
            return self.execute(context)

    executor = BlockingExecutor()
    worker, worker_store = owner(path, executor, clock, instance="worker")
    controller, controller_store = owner(path, executor, clock, instance="controller")
    started = worker.mutate(mutation("start-stop", AcceptanceMutation.START, run_spec=spec()))
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(worker.step()))
    thread.start()
    assert entered.wait(timeout=5)

    stopped = controller.mutate(
        mutation("stop-001", AcceptanceMutation.STOP, run_id=started.acceptance_run_id)
    )
    assert stopped.status is AcceptanceStatus.STOPPING
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    final = controller.snapshot(started.acceptance_run_id)
    assert final is not None
    assert final.status is AcceptanceStatus.CANCELLED
    assert final.completed_at is not None
    worker_store.close()
    controller_store.close()


def test_failure_is_retryable_with_new_request_and_same_run_id(tmp_path: Path) -> None:
    clock = Clock()
    executor = Executor(
        [
            PhaseOutcome(
                status=PhaseOutcomeStatus.FAILED,
                error_code="DETERMINISTIC_PHASE_FAILURE",
                next_action="RETRY_PHASE",
            ),
            PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED),
        ]
    )
    current, store = owner(tmp_path / "acceptance.db", executor, clock)
    started = current.mutate(mutation("start-fail", AcceptanceMutation.START, run_spec=spec()))
    failed = current.step()
    assert failed is not None
    assert failed.status is AcceptanceStatus.FAILED
    assert failed.error == "DETERMINISTIC_PHASE_FAILURE"

    retried = current.mutate(
        mutation("retry-001", AcceptanceMutation.RETRY, run_id=started.acceptance_run_id)
    )
    assert retried.acceptance_run_id == started.acceptance_run_id
    assert retried.status is AcceptanceStatus.QUEUED
    succeeded = current.step()
    assert succeeded is not None
    assert succeeded.phase is AcceptancePhase.DATASET
    store.close()


def test_evidence_warning_and_resource_surfaces_reject_private_payloads() -> None:
    with pytest.raises(AcceptanceOwnerError):
        EvidenceReference(kind="report", digest="a" * 64, reference="private/path")
    with pytest.raises(AcceptanceOwnerError):
        PhaseOutcome(status=PhaseOutcomeStatus.RUNNING, warnings=("token=value",))
    with pytest.raises(AcceptanceOwnerError):
        ResourceSample(timestamp=NOW, cpu_percent=101.0, ram_bytes=1, disk_free_bytes=1)


def test_optional_72h_model_requires_24h_and_orders_final_verification() -> None:
    with pytest.raises(AcceptanceOwnerError) as raised:
        spec(soak72=True)
    assert raised.value.code is AcceptanceErrorCode.INVALID_REQUEST

    enabled = spec(soak24=True, soak72=True)
    assert enabled.phases[-3:] == (
        AcceptancePhase.SOAK_24H,
        AcceptancePhase.SOAK_72H,
        AcceptancePhase.FINAL_VERIFICATION,
    )
