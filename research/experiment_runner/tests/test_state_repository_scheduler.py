from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_research_runner import BacktestJob, ModularBacktestEngineAdapter
from modular_research_runner.models import ExperimentStatus, SpecificationError
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.repository import (
    RunLease,
    SqliteExperimentRepository,
    StateRepositoryError,
)
from modular_research_runner.scheduler import CancellationToken, LocalResearchScheduler
from modular_research_runner.state import (
    FailureCategory,
    FailureRecord,
    RunStatus,
    StateTransitionError,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _job(tmp_path: Path, suffix: str = "1") -> BacktestJob:
    return BacktestJob(
        run_id=f"run-{suffix}",
        experiment_id="exp-test",
        stage_id="train",
        split_id="split-1",
        dataset_id="dataset-1",
        scenario_id="base",
        seed=int(suffix) if suffix.isdigit() else 1,
        parameters={"threshold": 0.45, "label": suffix},
        output_directory=(tmp_path / "outputs").resolve(),
    )


def _repository(tmp_path: Path) -> SqliteExperimentRepository:
    repository = SqliteExperimentRepository(tmp_path / "state" / "research.sqlite3")
    repository.register_experiment(
        experiment_id="exp-test",
        specification_sha256="a" * 64,
        manifest={"schema_version": 1},
        created_at=BASE_TIME,
    )
    return repository


def _claim(
    repository: SqliteExperimentRepository,
    *,
    worker_id: str,
    session_id: str,
    lease_token: str,
    at: datetime = BASE_TIME,
    lease_seconds: int = 30,
) -> tuple[BacktestJob, object, RunLease]:
    claimed = repository.claim_next(
        experiment_id="exp-test",
        worker_id=worker_id,
        session_id=session_id,
        lease_token=lease_token,
        at=at,
        lease_seconds=lease_seconds,
    )
    assert claimed is not None
    return claimed


def _engine_outcome(job: BacktestJob, *, bars_processed: int = 900) -> SimpleNamespace:
    output_directory = job.output_directory / job.run_id
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "result.json").write_text("{}\n", encoding="utf-8")
    return SimpleNamespace(
        run_id=f"engine-{job.run_id}",
        output_directory=output_directory,
        bars_processed=bars_processed,
        restart_verified=True,
    )


def test_repository_registration_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.register_experiment(
        experiment_id="exp-test",
        specification_sha256="a" * 64,
        manifest={"schema_version": 1},
        created_at=BASE_TIME + timedelta(days=1),
    )
    with pytest.raises(StateRepositoryError, match="different provenance"):
        repository.register_experiment(
            experiment_id="exp-test",
            specification_sha256="b" * 64,
            manifest={"schema_version": 1},
            created_at=BASE_TIME,
        )

    assert repository.register_jobs([_job(tmp_path)], max_attempts=2, created_at=BASE_TIME) == 1
    assert repository.register_jobs([_job(tmp_path)], max_attempts=2, created_at=BASE_TIME) == 0
    with pytest.raises(StateRepositoryError, match="different inputs"):
        repository.register_jobs([_job(tmp_path)], max_attempts=3, created_at=BASE_TIME)


def test_repository_claim_heartbeat_and_success_round_trip(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job = _job(tmp_path)
    repository.register_jobs([job], max_attempts=2, created_at=BASE_TIME)
    assert repository.queue_planned("exp-test", at=BASE_TIME) == 1

    restored_job, state, lease = _claim(
        repository,
        worker_id="worker-1",
        session_id="session-1",
        lease_token="token-1",
    )
    assert restored_job == job
    assert state.status is RunStatus.RUNNING
    assert state.attempt_count == 1
    assert state.lease_owner == "worker-1"

    heartbeat, lease = repository.heartbeat(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        at=BASE_TIME + timedelta(seconds=10),
        lease_seconds=30,
    )
    assert heartbeat.lease_expires_at == BASE_TIME + timedelta(seconds=40)
    with pytest.raises(StateTransitionError, match="no longer active"):
        repository.heartbeat(
            job.run_id,
            worker_id="worker-2",
            session_id=lease.session_id,
            lease_token=lease.lease_token,
            expected_revision=lease.revision,
            at=BASE_TIME + timedelta(seconds=11),
            lease_seconds=30,
        )

    reference = ModularBacktestEngineAdapter(
        lambda value: value,
        lambda _: _engine_outcome(job),
    ).execute(job)
    completed = repository.succeed(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        result=reference,
        at=BASE_TIME + timedelta(seconds=20),
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert completed.result == reference
    assert [event.to_status for event in repository.events(job.run_id)] == [
        RunStatus.PLANNED,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.SUCCEEDED,
    ]


def test_heartbeat_rejects_non_positive_lease_duration(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job = _job(tmp_path)
    repository.register_jobs([job], max_attempts=1, created_at=BASE_TIME)
    repository.queue_planned("exp-test", at=BASE_TIME)
    _, _, lease = _claim(
        repository,
        worker_id="worker-1",
        session_id="session-1",
        lease_token="token-1",
    )

    for value in (0, -1, True):
        with pytest.raises(SpecificationError, match="lease_seconds must be positive"):
            repository.heartbeat(
                job.run_id,
                worker_id=lease.worker_id,
                session_id=lease.session_id,
                lease_token=lease.lease_token,
                expected_revision=lease.revision,
                at=BASE_TIME + timedelta(seconds=1),
                lease_seconds=value,
            )


def test_retry_wait_is_not_released_before_available_time(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job = _job(tmp_path)
    repository.register_jobs([job], max_attempts=2, created_at=BASE_TIME)
    repository.queue_planned("exp-test", at=BASE_TIME)
    _, _, lease = _claim(
        repository,
        worker_id="w",
        session_id="session-w",
        lease_token="token-w",
    )
    retry_at = BASE_TIME + timedelta(seconds=60)
    state = repository.fail(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        failure=FailureRecord(
            category=FailureCategory.TRANSIENT,
            message="temporary",
            occurred_at=BASE_TIME,
            retryable=True,
        ),
        retry_at=retry_at,
        at=BASE_TIME,
    )
    assert state.status is RunStatus.RETRY_WAIT
    assert repository.queue_planned("exp-test", at=BASE_TIME + timedelta(seconds=59)) == 0
    assert repository.queue_planned("exp-test", at=retry_at) == 1
    assert repository.get_run(job.run_id).status is RunStatus.QUEUED


def test_expired_lease_is_recovered_and_retried(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job = _job(tmp_path)
    repository.register_jobs([job], max_attempts=2, created_at=BASE_TIME)
    repository.queue_planned("exp-test", at=BASE_TIME)
    _claim(
        repository,
        worker_id="dead",
        session_id="dead-session",
        lease_token="dead-token",
        lease_seconds=10,
    )
    assert (
        repository.recover_expired_leases(
            experiment_id="exp-test", at=BASE_TIME + timedelta(seconds=11)
        )
        == 1
    )
    state = repository.get_run(job.run_id)
    assert state.status is RunStatus.QUEUED
    assert state.attempt_count == 1
    claimed = repository.claim_next(
        experiment_id="exp-test",
        worker_id="replacement",
        session_id="replacement-session",
        lease_token="replacement-token",
        at=BASE_TIME + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert claimed is not None
    assert claimed[1].attempt_count == 2


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = BASE_TIME
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.value += timedelta(seconds=seconds)
        time.sleep(0.001)


def test_scheduler_retries_and_returns_run_id_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    jobs = [_job(tmp_path, suffix) for suffix in ("3", "1", "2")]
    repository.register_jobs(jobs, max_attempts=2, created_at=BASE_TIME)
    attempts: dict[str, int] = {}
    lock = threading.Lock()

    def config_factory(job: BacktestJob) -> BacktestJob:
        return job

    def run_function(job: BacktestJob) -> SimpleNamespace:
        with lock:
            attempts[job.run_id] = attempts.get(job.run_id, 0) + 1
            attempt = attempts[job.run_id]
        if job.run_id == "run-2" and attempt == 1:
            raise TimeoutError("temporary engine timeout")
        return _engine_outcome(job, bars_processed=100 + int(job.run_id[-1]))

    clock = _AdvancingClock()
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(config_factory, run_function),
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=1.0,
            maximum_backoff_seconds=1.0,
        ),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=2,
        clock=clock,
        sleep=clock.sleep,
        poll_interval_seconds=0.25,
    )
    summary = scheduler.run("exp-test")
    assert summary.status is ExperimentStatus.COMPLETED
    assert [state.run_id for state in summary.runs] == ["run-1", "run-2", "run-3"]
    assert all(state.status is RunStatus.SUCCEEDED for state in summary.runs)
    assert attempts == {"run-1": 1, "run-2": 2, "run-3": 1}
    assert repository.get_run("run-2").attempt_count == 2


def test_scheduler_records_terminal_failure_without_retry(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.register_jobs([_job(tmp_path)], max_attempts=3, created_at=BASE_TIME)
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(
            lambda job: job,
            lambda job: (_ for _ in ()).throw(ValueError("bad input")),
        ),
        retry_policy=RetryPolicy(max_attempts=3),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
    )
    summary = scheduler.run("exp-test")
    assert summary.status is ExperimentStatus.FAILED
    run = summary.runs[0]
    assert run.status is RunStatus.FAILED
    assert run.attempt_count == 1
    assert run.failure is not None
    assert run.failure.category is FailureCategory.INPUT


def test_scheduler_honors_preexisting_cancellation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.register_jobs([_job(tmp_path)], max_attempts=1, created_at=BASE_TIME)
    token = CancellationToken()
    token.cancel()
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(
            lambda job: job,
            lambda job: pytest.fail("canceled job must not execute"),
        ),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
    )
    summary = scheduler.run("exp-test", cancellation=token)
    assert summary.status is ExperimentStatus.CANCELED
    assert summary.runs[0].status is RunStatus.CANCELED


def _process_config(job: BacktestJob) -> BacktestJob:
    return job


def _process_run(job: BacktestJob) -> SimpleNamespace:
    job.output_directory.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        run_id=f"engine-{job.run_id}",
        output_directory=job.output_directory,
        bars_processed=5,
        restart_verified=True,
    )


def test_scheduler_supports_spawned_process_workers(tmp_path: Path) -> None:
    from modular_research_runner.scheduler import ExecutorMode

    repository = _repository(tmp_path)
    repository.register_jobs([_job(tmp_path)], max_attempts=1, created_at=BASE_TIME)
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(_process_config, _process_run),
        retry_policy=RetryPolicy(max_attempts=1),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=1,
        executor_mode=ExecutorMode.PROCESS,
    )
    summary = scheduler.run("exp-test")
    assert summary.status is ExperimentStatus.COMPLETED
    assert summary.runs[0].result is not None
    assert summary.runs[0].result.bars_processed == 5
