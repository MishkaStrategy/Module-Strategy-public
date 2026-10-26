from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_research_runner import (
    BacktestJob,
    BacktestRunReference,
    ExperimentStatus,
    ReadOnlyExperimentRepository,
    ReadOnlyRepositoryError,
    ReadOnlyRepositoryNotFound,
    RunStatus,
    SqliteExperimentRepository,
)

NOW = datetime(2026, 8, 7, 10, 30, tzinfo=UTC)


def _seed_repository(path: Path, tmp_path: Path) -> None:
    repository = SqliteExperimentRepository(path)
    repository.register_experiment(
        experiment_id="experiment-1",
        specification_sha256="a" * 64,
        manifest={"private_note": "must not be projected"},
        created_at=NOW,
    )
    job = BacktestJob(
        run_id="run-1",
        experiment_id="experiment-1",
        stage_id="stage-1",
        split_id="split-1",
        dataset_id="dataset-1",
        scenario_id="scenario-1",
        seed=7,
        parameters={"private_parameter": "must not be projected"},
        output_directory=tmp_path / "private-job-output",
        stage_role="train",
        repetition_index=2,
        metadata={"private_metadata": "must not be projected"},
    )
    assert repository.register_jobs((job,), max_attempts=2, created_at=NOW) == 1
    assert repository.queue_planned("experiment-1", at=NOW) == 1
    claimed = repository.claim_next(
        experiment_id="experiment-1",
        worker_id="worker-secret",
        session_id="session-secret",
        lease_token="lease-secret",
        at=NOW,
        lease_seconds=60,
    )
    assert claimed is not None
    claimed_job, claimed_state, lease = claimed
    assert claimed_job.run_id == "run-1"
    assert claimed_state.status is RunStatus.RUNNING
    assert lease.run_id == "run-1"
    repository.succeed(
        "run-1",
        worker_id="worker-secret",
        session_id="session-secret",
        lease_token="lease-secret",
        expected_revision=lease.revision,
        result=BacktestRunReference(
            requested_run_id="run-1",
            engine_run_id="engine-run-1",
            output_directory=tmp_path / "private-result-output",
            bars_processed=120,
            restart_verified=True,
        ),
        at=NOW,
    )
    repository.set_experiment_status("experiment-1", ExperimentStatus.COMPLETED, at=NOW)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_only_repository_projects_safe_native_state_without_mutating_db(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.sqlite3"
    _seed_repository(path, tmp_path)
    before_hash = _sha256(path)
    before_mtime = path.stat().st_mtime_ns
    before_entries = tuple(sorted(item.name for item in tmp_path.iterdir()))

    reader = ReadOnlyExperimentRepository(path)
    experiments = reader.list_experiments()
    experiment = reader.get_experiment("experiment-1")
    runs = reader.list_runs("experiment-1")
    run = reader.get_run("run-1")

    assert len(experiments) == 1
    assert experiment.status is ExperimentStatus.COMPLETED
    assert experiment.run_counts == {RunStatus.SUCCEEDED: 1}
    assert runs == (run,)
    assert run.status is RunStatus.SUCCEEDED
    assert run.stage_id == "stage-1"
    assert run.split_id == "split-1"
    assert run.dataset_id == "dataset-1"
    assert run.scenario_id == "scenario-1"
    assert run.stage_role == "train"
    assert run.repetition_index == 2
    assert run.attempt_count == 1
    assert run.max_attempts == 2
    assert run.engine_run_id == "engine-run-1"
    assert run.bars_processed == 120
    assert run.restart_verified is True
    assert run.lease_expires_at is None
    rendered = repr((experiment, run))
    assert "private-job-output" not in rendered
    assert "private-result-output" not in rendered
    assert "private_parameter" not in rendered
    assert "private_metadata" not in rendered
    assert "worker-secret" not in rendered
    assert "session-secret" not in rendered
    assert "lease-secret" not in rendered

    assert _sha256(path) == before_hash
    assert path.stat().st_mtime_ns == before_mtime
    assert tuple(sorted(item.name for item in tmp_path.iterdir())) == before_entries


def test_read_only_repository_missing_path_is_side_effect_free(tmp_path: Path) -> None:
    path = tmp_path / "missing-parent" / "research.sqlite3"

    with pytest.raises(ReadOnlyRepositoryError, match="does not exist"):
        ReadOnlyExperimentRepository(path)

    assert not path.exists()
    assert not path.parent.exists()


def test_read_only_repository_rejects_symlink_database(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _seed_repository(target, tmp_path)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)

    with pytest.raises(ReadOnlyRepositoryError, match="symlink"):
        ReadOnlyExperimentRepository(linked)


def test_read_only_repository_rejects_incompatible_schema_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _seed_repository(path, tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE repository_meta SET value = '999' WHERE key = 'schema_version'")
    before_hash = _sha256(path)

    with pytest.raises(ReadOnlyRepositoryError, match="unsupported Research state schema version"):
        ReadOnlyExperimentRepository(path)

    assert _sha256(path) == before_hash


def test_read_only_repository_fails_closed_on_corrupt_job_identity(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _seed_repository(path, tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runs SET job_json = ? WHERE run_id = 'run-1'",
            ('{"run_id":"other-run","experiment_id":"experiment-1"}',),
        )
    reader = ReadOnlyExperimentRepository(path)

    with pytest.raises(ReadOnlyRepositoryError, match="run identity mismatch"):
        reader.get_run("run-1")


def test_read_only_repository_returns_typed_not_found(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _seed_repository(path, tmp_path)
    reader = ReadOnlyExperimentRepository(path)

    with pytest.raises(ReadOnlyRepositoryNotFound):
        reader.get_experiment("missing-experiment")
    with pytest.raises(ReadOnlyRepositoryNotFound):
        reader.get_run("missing-run")
    with pytest.raises(ReadOnlyRepositoryNotFound):
        reader.list_runs("missing-experiment")
