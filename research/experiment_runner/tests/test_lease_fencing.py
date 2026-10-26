from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_research_runner import _repository_impl
from modular_research_runner.engine import BacktestJob, BacktestRunReference
from modular_research_runner.repository import (
    RunLease,
    SqliteExperimentRepository,
    StateRepositoryError,
)
from modular_research_runner.state import RunStatus, StateTransitionError

NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _job(tmp_path: Path) -> BacktestJob:
    return BacktestJob(
        run_id="run-fenced",
        experiment_id="exp-fenced",
        stage_id="stage-train",
        split_id="split-train",
        dataset_id="dataset",
        scenario_id="base",
        seed=17,
        parameters={},
        output_directory=tmp_path / "output",
    )


def _register(repository, tmp_path: Path, *, max_attempts: int = 2) -> BacktestJob:
    repository.register_experiment(
        experiment_id="exp-fenced",
        specification_sha256="a" * 64,
        manifest={"schema_version": 1},
        created_at=NOW,
    )
    job = _job(tmp_path)
    repository.register_jobs([job], max_attempts=max_attempts, created_at=NOW)
    repository.queue_planned("exp-fenced", at=NOW)
    return job


def _claim(
    repository: SqliteExperimentRepository,
    *,
    session_id: str,
    lease_token: str,
    at: datetime,
    worker_id: str = "reused-worker",
) -> RunLease:
    claimed = repository.claim_next(
        experiment_id="exp-fenced",
        worker_id=worker_id,
        session_id=session_id,
        lease_token=lease_token,
        at=at,
        lease_seconds=10,
    )
    assert claimed is not None
    return claimed[2]


def test_stale_worker_cannot_publish_after_expired_lease_is_reissued(
    tmp_path: Path,
) -> None:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    job = _register(repository, tmp_path)
    stale = _claim(
        repository,
        session_id="scheduler-old",
        lease_token="claim-old",
        at=NOW,
    )

    recovered_at = NOW + timedelta(seconds=11)
    assert (
        repository.recover_expired_leases(
            experiment_id="exp-fenced",
            at=recovered_at,
        )
        == 1
    )
    current = _claim(
        repository,
        session_id="scheduler-new",
        lease_token="claim-new",
        at=recovered_at,
    )
    result = BacktestRunReference(
        requested_run_id=job.run_id,
        engine_run_id="engine-current",
        output_directory=tmp_path / "output-current",
        bars_processed=10,
        restart_verified=True,
    )

    stale_arguments = {
        "worker_id": stale.worker_id,
        "session_id": stale.session_id,
        "lease_token": stale.lease_token,
        "expected_revision": stale.revision,
    }
    with pytest.raises(StateTransitionError, match="no longer active"):
        repository.heartbeat(
            job.run_id,
            **stale_arguments,
            at=recovered_at + timedelta(seconds=1),
            lease_seconds=10,
        )
    with pytest.raises(StateTransitionError, match="no longer active"):
        repository.succeed(
            job.run_id,
            **stale_arguments,
            result=result,
            at=recovered_at + timedelta(seconds=1),
        )
    with pytest.raises(StateTransitionError, match="no longer active"):
        repository.cancel_claim(
            job.run_id,
            **stale_arguments,
            at=recovered_at + timedelta(seconds=1),
        )

    completed = repository.succeed(
        job.run_id,
        worker_id=current.worker_id,
        session_id=current.session_id,
        lease_token=current.lease_token,
        expected_revision=current.revision,
        result=result,
        at=recovered_at + timedelta(seconds=2),
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert completed.attempt_count == 2
    with sqlite3.connect(repository.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM run_claims WHERE run_id = ?",
                (job.run_id,),
            ).fetchone()[0]
            == 0
        )


def test_claim_requires_matching_session_token_and_revision(tmp_path: Path) -> None:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    job = _register(repository, tmp_path, max_attempts=1)
    lease = _claim(
        repository,
        session_id="scheduler-a",
        lease_token="claim-a",
        at=NOW,
        worker_id="worker-a",
    )

    invalid_claims = (
        {
            "worker_id": lease.worker_id,
            "session_id": "scheduler-b",
            "lease_token": lease.lease_token,
            "expected_revision": lease.revision,
        },
        {
            "worker_id": lease.worker_id,
            "session_id": lease.session_id,
            "lease_token": "claim-b",
            "expected_revision": lease.revision,
        },
        {
            "worker_id": lease.worker_id,
            "session_id": lease.session_id,
            "lease_token": lease.lease_token,
            "expected_revision": lease.revision + 1,
        },
    )
    for claim in invalid_claims:
        with pytest.raises(StateTransitionError, match="no longer active"):
            repository.heartbeat(
                job.run_id,
                **claim,
                at=NOW + timedelta(seconds=1),
                lease_seconds=10,
            )

    _, renewed = repository.heartbeat(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        at=NOW + timedelta(seconds=1),
        lease_seconds=10,
    )
    assert renewed.revision == lease.revision + 1
    with pytest.raises(StateTransitionError, match="no longer active"):
        repository.cancel_claim(
            job.run_id,
            worker_id=lease.worker_id,
            session_id=lease.session_id,
            lease_token=lease.lease_token,
            expected_revision=lease.revision,
            at=NOW + timedelta(seconds=2),
        )
    canceled = repository.cancel_claim(
        job.run_id,
        worker_id=renewed.worker_id,
        session_id=renewed.session_id,
        lease_token=renewed.lease_token,
        expected_revision=renewed.revision,
        at=NOW + timedelta(seconds=2),
    )
    assert canceled.status is RunStatus.CANCELED


def test_schema_v1_migrates_to_fenced_v2_when_no_claim_is_active(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy = _repository_impl.SqliteExperimentRepository(path)
    _register(legacy, tmp_path, max_attempts=1)

    migrated = SqliteExperimentRepository(path)
    assert migrated.get_run("run-fenced").status is RunStatus.QUEUED
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM repository_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        claim_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'run_claims'"
        ).fetchone()
    assert version == "2"
    assert claim_table == ("run_claims",)


def test_schema_v1_migration_rejects_active_unfenced_claim(tmp_path: Path) -> None:
    path = tmp_path / "legacy-active.sqlite3"
    legacy = _repository_impl.SqliteExperimentRepository(path)
    _register(legacy, tmp_path, max_attempts=1)
    claimed = legacy.claim_next(
        experiment_id="exp-fenced",
        worker_id="legacy-worker",
        at=NOW,
        lease_seconds=30,
    )
    assert claimed is not None

    with pytest.raises(
        StateRepositoryError,
        match="cannot migrate state repository with active run leases",
    ):
        SqliteExperimentRepository(path)
