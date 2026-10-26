from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_research_runner.engine import BacktestJob, BacktestRunReference
from modular_research_runner.models import ExperimentStatus, SpecificationError
from modular_research_runner.repository import (
    SqliteExperimentRepository,
    StateRepositoryError,
    _datetime,
    _failure,
    _iso,
    _job,
    _json,
    _require_row,
    _result,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _repository(tmp_path: Path) -> SqliteExperimentRepository:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    repository.register_experiment(
        experiment_id="exp-corruption",
        specification_sha256="a" * 64,
        manifest={"schema_version": 1},
        created_at=NOW,
    )
    return repository


def _job_value(tmp_path: Path) -> BacktestJob:
    return BacktestJob(
        run_id="run-corruption",
        experiment_id="exp-corruption",
        stage_id="stage-train",
        split_id="split-train",
        dataset_id="dataset-1",
        scenario_id="base",
        seed=1,
        parameters={},
        output_directory=tmp_path / "output",
        stage_role="train",
        metadata={"configuration_sha256": "b" * 64},
    )


def test_repository_codec_guards_reject_malformed_values() -> None:
    with pytest.raises(SpecificationError, match="timestamps must be timezone-aware"):
        _iso(datetime(2026, 7, 23))
    with pytest.raises(StateRepositoryError, match="stored timestamp is invalid"):
        _datetime("not-a-timestamp")

    with pytest.raises(StateRepositoryError, match="stored job payload is invalid"):
        _job(_json([]))
    with pytest.raises(StateRepositoryError, match="stored job payload is invalid"):
        _job(_json({"period": "not-a-mapping"}))
    with pytest.raises(StateRepositoryError, match="stored job payload is invalid"):
        _job(_json({"run_id": "missing-required-fields"}))

    with pytest.raises(StateRepositoryError, match="stored failure payload is invalid"):
        _failure(_json([]))
    with pytest.raises(StateRepositoryError, match="stored failure payload is invalid"):
        _failure(_json({"category": "UNKNOWN"}))

    with pytest.raises(StateRepositoryError, match="stored result payload is invalid"):
        _result(_json([]))
    with pytest.raises(StateRepositoryError, match="stored result payload is invalid"):
        _result(_json({"requested_run_id": "missing-required-fields"}))

    with pytest.raises(StateRepositoryError, match="row disappeared after test operation"):
        _require_row(None, operation="test operation")


def test_repository_rejects_symlink_and_directory_database_paths(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"")
    symlink = tmp_path / "linked.sqlite3"
    symlink.symlink_to(target)
    with pytest.raises(StateRepositoryError, match="must not be a symlink"):
        SqliteExperimentRepository(symlink)

    directory = tmp_path / "directory.sqlite3"
    directory.mkdir()
    with pytest.raises(StateRepositoryError, match="must be a regular file"):
        SqliteExperimentRepository(directory)


@pytest.mark.parametrize("stored_version", ["999", "not-an-integer"])
def test_repository_rejects_invalid_or_corrupt_schema_version(
    tmp_path: Path,
    stored_version: str,
) -> None:
    path = tmp_path / "schema.sqlite3"
    SqliteExperimentRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE repository_meta SET value = ? WHERE key = 'schema_version'",
            (stored_version,),
        )
        connection.commit()
    with pytest.raises(StateRepositoryError, match="unsupported state repository schema version"):
        SqliteExperimentRepository(path)


def test_repository_experiment_state_guards_fail_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(StateRepositoryError, match="experiment is not registered"):
        repository.experiment_status("exp-missing")
    with pytest.raises(StateRepositoryError, match="experiment is not registered"):
        repository.set_experiment_status(
            "exp-missing",
            ExperimentStatus.RUNNING,
            at=NOW,
        )

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE experiments SET status = 'NOT_A_STATUS' WHERE experiment_id = ?",
            ("exp-corruption",),
        )
        connection.commit()
    with pytest.raises(StateRepositoryError, match="stored experiment status is invalid"):
        repository.experiment_status("exp-corruption")


def test_repository_job_registration_and_transition_guards(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job = _job_value(tmp_path)
    with pytest.raises(SpecificationError, match="max_attempts must be positive"):
        repository.register_jobs([job], max_attempts=0, created_at=NOW)

    result = BacktestRunReference(
        requested_run_id="run-missing",
        engine_run_id="engine-run-missing",
        output_directory=tmp_path / "missing-output",
        bars_processed=0,
        restart_verified=True,
    )
    with pytest.raises(StateRepositoryError, match="run is not registered"):
        repository.succeed(
            "run-missing",
            worker_id="worker-missing",
            session_id="session-missing",
            lease_token="token-missing",
            expected_revision=1,
            result=result,
            at=NOW,
        )
