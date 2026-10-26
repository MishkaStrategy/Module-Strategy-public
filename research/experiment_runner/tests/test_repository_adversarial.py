from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from effective_config_fixture import prepared_effective_config

from modular_research_runner.effective_config import (
    EffectiveConfigArtifactReference,
    write_sidecar_for_job,
)
from modular_research_runner.engine import BacktestJob, BacktestRunReference
from modular_research_runner.models import ExperimentStatus, SpecificationError, TimeRange
from modular_research_runner.output_manifest import write_output_manifest
from modular_research_runner.repository import (
    RunLease,
    SqliteExperimentRepository,
    StateRepositoryError,
)
from modular_research_runner.scheduler import verify_output_directory
from modular_research_runner.state import (
    FailureCategory,
    FailureRecord,
    RunStatus,
    StateTransitionError,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _job(root: Path, run_id: str = "run-a") -> BacktestJob:
    return BacktestJob(
        run_id,
        "exp-a",
        "stage-a",
        "split-a",
        "data-a",
        "base",
        7,
        {"x": 1},
        root.resolve(),
    )


def _repo(tmp_path: Path) -> SqliteExperimentRepository:
    repo = SqliteExperimentRepository(tmp_path / "db.sqlite3")
    repo.register_experiment(
        experiment_id="exp-a",
        specification_sha256="a" * 64,
        manifest={"v": 1},
        created_at=NOW,
    )
    return repo


def _claim(
    repo: SqliteExperimentRepository,
    *,
    worker_id: str = "w",
    session_id: str = "session-w",
    lease_token: str = "token-w",
    lease_seconds: int = 10,
) -> RunLease:
    claimed = repo.claim_next(
        experiment_id="exp-a",
        worker_id=worker_id,
        session_id=session_id,
        lease_token=lease_token,
        at=NOW,
        lease_seconds=lease_seconds,
    )
    assert claimed is not None
    return claimed[2]


def test_repository_path_and_schema_defenses(tmp_path: Path) -> None:
    target = tmp_path / "real.sqlite3"
    target.touch()
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(StateRepositoryError, match="symlink"):
        SqliteExperimentRepository(link)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(StateRepositoryError, match="regular file"):
        SqliteExperimentRepository(directory)

    broken = tmp_path / "broken.sqlite3"
    with sqlite3.connect(broken) as connection:
        connection.execute("CREATE TABLE repository_meta(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO repository_meta(key, value) VALUES('schema_version', '999')"
        )
    with pytest.raises(StateRepositoryError, match="schema version"):
        SqliteExperimentRepository(broken)


def test_missing_entities_and_status_updates_are_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert repo.experiment_status("exp-a") is ExperimentStatus.CREATED
    repo.set_experiment_status("exp-a", ExperimentStatus.PLANNED, at=NOW)
    assert repo.experiment_status("exp-a") is ExperimentStatus.PLANNED
    with pytest.raises(StateRepositoryError, match="not registered"):
        repo.experiment_status("missing")
    with pytest.raises(StateRepositoryError, match="not registered"):
        repo.set_experiment_status("missing", ExperimentStatus.FAILED, at=NOW)
    with pytest.raises(StateRepositoryError, match="not registered"):
        repo.get_run("missing")
    with pytest.raises(StateRepositoryError, match="not registered"):
        repo.get_job("missing")


def test_claim_validation_cancel_counts_and_filtering(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jobs = [_job(tmp_path, "run-b"), _job(tmp_path, "run-a")]
    repo.register_jobs(jobs, max_attempts=1, created_at=NOW)
    with pytest.raises(SpecificationError, match="worker_id"):
        repo.claim_next(
            experiment_id="exp-a",
            worker_id=" ",
            session_id="session",
            lease_token="token",
            at=NOW,
            lease_seconds=1,
        )
    with pytest.raises(SpecificationError, match="session_id"):
        repo.claim_next(
            experiment_id="exp-a",
            worker_id="w",
            session_id=" ",
            lease_token="token",
            at=NOW,
            lease_seconds=1,
        )
    with pytest.raises(SpecificationError, match="lease_token"):
        repo.claim_next(
            experiment_id="exp-a",
            worker_id="w",
            session_id="session",
            lease_token=" ",
            at=NOW,
            lease_seconds=1,
        )
    with pytest.raises(SpecificationError, match="lease_seconds"):
        repo.claim_next(
            experiment_id="exp-a",
            worker_id="w",
            session_id="session",
            lease_token="token",
            at=NOW,
            lease_seconds=0,
        )
    repo.queue_planned("exp-a", at=NOW)
    assert [item.run_id for item in repo.list_runs("exp-a")] == ["run-a", "run-b"]
    assert repo.counts("exp-a") == {RunStatus.QUEUED: 2}
    assert repo.list_runs("exp-a", statuses=[]) == ()
    assert [item.run_id for item in repo.list_runs("exp-a", statuses=[RunStatus.QUEUED])] == [
        "run-a",
        "run-b",
    ]
    assert repo.cancel_pending("exp-a", at=NOW) == 2
    assert repo.cancel_pending("exp-a", at=NOW) == 0
    assert repo.counts("exp-a") == {RunStatus.CANCELED: 2}


def test_failure_exhaustion_and_result_identity_checks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    job = _job(tmp_path)
    repo.register_jobs([job], max_attempts=1, created_at=NOW)
    repo.queue_planned("exp-a", at=NOW)
    lease = _claim(repo)
    with pytest.raises(StateTransitionError, match="does not belong"):
        repo.succeed(
            job.run_id,
            worker_id=lease.worker_id,
            session_id=lease.session_id,
            lease_token=lease.lease_token,
            expected_revision=lease.revision,
            result=BacktestRunReference("other", "engine", tmp_path, 1, False),
            at=NOW,
        )
    failed = repo.fail(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        failure=FailureRecord(
            FailureCategory.TRANSIENT,
            "still fails",
            NOW,
            True,
            "TimeoutError",
        ),
        retry_at=NOW + timedelta(seconds=1),
        at=NOW,
    )
    assert failed.status is RunStatus.FAILED
    assert failed.failure is not None
    with pytest.raises(StateTransitionError, match="illegal"):
        repo.mark_corrupted(job.run_id, detail="cannot transition", at=NOW)


def test_success_can_be_marked_corrupted_after_artifact_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    job = _job(tmp_path)
    repo.register_jobs([job], max_attempts=1, created_at=NOW)
    repo.queue_planned("exp-a", at=NOW)
    lease = _claim(repo)
    repo.succeed(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        result=BacktestRunReference(job.run_id, "engine", tmp_path, 1, True),
        at=NOW,
    )
    corrupted = repo.mark_corrupted(job.run_id, detail="checksum mismatch", at=NOW)
    assert corrupted.status is RunStatus.CORRUPTED
    assert corrupted.result is None
    assert repo.events(job.run_id)[-1].detail == "checksum mismatch"


def test_effective_config_reference_survives_repository_cold_restore(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = tmp_path / "db.sqlite3"
    job = _job(tmp_path)
    repo.register_jobs([job], max_attempts=1, created_at=NOW)
    repo.queue_planned("exp-a", at=NOW)
    lease = _claim(repo)
    artifact = EffectiveConfigArtifactReference(
        "effective-backtest-config.json",
        "a" * 64,
        128,
    )
    repo.succeed(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        result=BacktestRunReference(
            job.run_id,
            "engine-run",
            tmp_path,
            3,
            True,
            effective_config_artifact=artifact,
        ),
        at=NOW,
    )

    restored = SqliteExperimentRepository(path).get_run(job.run_id)
    assert restored.result is not None
    assert restored.result.effective_config_artifact == artifact

    legacy = BacktestRunReference(job.run_id, "legacy-engine", tmp_path, 1, False)
    from modular_research_runner._repository_impl import _result_payload

    assert set(_result_payload(legacy)) == {
        "requested_run_id",
        "engine_run_id",
        "output_directory",
        "bars_processed",
        "restart_verified",
    }


def test_scheduler_sidecar_root_survives_sqlite_reopen(tmp_path: Path) -> None:
    job_root = tmp_path / "job-root"
    job_root.mkdir()
    job = _job(job_root)
    output = job_root / "engine-results" / "engine-run"
    output.mkdir(parents=True)
    (output / "synthetic-result.json").write_text("{}", encoding="utf-8")
    prepared = prepared_effective_config(tmp_path, run_id=job.run_id, context_overrides={
        "experiment_id": job.experiment_id, "stage_id": job.stage_id,
        "stage_role": job.stage_role, "split_id": job.split_id,
        "dataset_id": job.dataset_id, "scenario_id": job.scenario_id,
    })
    job = replace(job, period=TimeRange(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        metadata={"configuration_sha256": prepared.context["configuration_sha256"]})
    artifact = write_sidecar_for_job(
        job_root,
        prepared,
        engine_run_id="engine-run",
        result_output_directory=output,
    )
    result = BacktestRunReference(
        job.run_id,
        "engine-run",
        output,
        1,
        False,
        effective_config_artifact=artifact,
    )
    write_output_manifest(result)
    repo = _repo(tmp_path)
    repo.register_jobs([job], max_attempts=1, created_at=NOW)
    repo.queue_planned("exp-a", at=NOW)
    lease = _claim(repo)
    repo.succeed(
        job.run_id,
        worker_id=lease.worker_id,
        session_id=lease.session_id,
        lease_token=lease.lease_token,
        expected_revision=lease.revision,
        result=result,
        at=NOW,
    )

    restored = SqliteExperimentRepository(tmp_path / "db.sqlite3").get_run(job.run_id)

    assert restored.result == result
    assert verify_output_directory(restored.result, job=SqliteExperimentRepository(
        tmp_path / "db.sqlite3").get_job(job.run_id))


def test_expired_final_attempt_becomes_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    job = _job(tmp_path)
    repo.register_jobs([job], max_attempts=1, created_at=NOW)
    repo.queue_planned("exp-a", at=NOW)
    _claim(
        repo,
        worker_id="lost",
        session_id="lost-session",
        lease_token="lost-token",
        lease_seconds=1,
    )
    assert repo.recover_expired_leases(experiment_id="exp-a", at=NOW + timedelta(seconds=2)) == 1
    state = repo.get_run(job.run_id)
    assert state.status is RunStatus.FAILED
    assert state.failure is not None
    assert state.failure.exception_type == "LeaseExpired"


def test_stage_activation_is_frozen_and_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    candidate = "c" * 64
    job = BacktestJob(
        "run-validation",
        "exp-a",
        "stage-validation",
        "split",
        "data",
        "base",
        1,
        {},
        tmp_path,
        stage_role="validation",
        metadata={"configuration_sha256": candidate},
    )
    assert repo.stage_activation_candidates("exp-a", "validation") is None
    assert (
        repo.activate_stage(
            experiment_id="exp-a",
            target_role="validation",
            candidate_configuration_hashes=[candidate],
            jobs=[job],
            max_attempts=1,
            activated_at=NOW,
        )
        == 1
    )
    assert (
        repo.activate_stage(
            experiment_id="exp-a",
            target_role="validation",
            candidate_configuration_hashes=[candidate],
            jobs=[job],
            max_attempts=1,
            activated_at=NOW,
        )
        == 0
    )
    assert repo.stage_activation_candidates("exp-a", "validation") == (candidate,)
    with pytest.raises(StateRepositoryError, match="different frozen"):
        repo.activate_stage(
            experiment_id="exp-a",
            target_role="validation",
            candidate_configuration_hashes=["d" * 64],
            jobs=[replace(job, metadata={"configuration_sha256": "d" * 64})],
            max_attempts=1,
            activated_at=NOW,
        )
    with pytest.raises(StateRepositoryError, match="not an activatable"):
        repo.activate_stage(
            experiment_id="exp-a",
            target_role="train",
            candidate_configuration_hashes=[candidate],
            jobs=[job],
            max_attempts=1,
            activated_at=NOW,
        )
