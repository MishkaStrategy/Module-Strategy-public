from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_research_runner import BacktestJob, SqliteExperimentRepository, StateRepositoryError

_NOW = datetime(2026, 8, 10, 6, 55, tzinfo=UTC)


class _BoundedRepository(SqliteExperimentRepository):
    def __init__(self, path: Path) -> None:
        self.max_page_count: int | None = None
        super().__init__(path)

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        if self.max_page_count is not None:
            connection.execute(f"PRAGMA max_page_count = {self.max_page_count}")
        return connection


def _page_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA page_count").fetchone()
        assert row is not None
        return int(row[0])
    finally:
        connection.close()


def _job(root: Path, index: int, experiment_id: str) -> BacktestJob:
    return BacktestJob(
        run_id=f"storage-boundary-run-{index:03d}",
        experiment_id=experiment_id,
        stage_id="train",
        split_id="fixture",
        dataset_id="fixture-btcusdt-5m-governance-v1",
        scenario_id="paper-only",
        seed=index,
        parameters={"index": index, "padding": "x" * 2048},
        output_directory=root / "outputs",
        stage_role="train",
    )


def _integrity_and_counts(path: Path, run_id: str) -> tuple[str, int, int]:
    connection = sqlite3.connect(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        run_count = connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?", (run_id,)
        ).fetchone()
        assert integrity is not None and run_count is not None and event_count is not None
        return str(integrity[0]), int(run_count[0]), int(event_count[0])
    finally:
        connection.close()


def test_repository_sqlite_full_is_sanitized_atomic_and_retryable(tmp_path: Path) -> None:
    path = tmp_path / "research-state.sqlite3"
    repository = _BoundedRepository(path)
    experiment_id = "research-storage-boundary"
    repository.register_experiment(
        experiment_id=experiment_id,
        specification_sha256="a" * 64,
        manifest={"dataset_id": "fixture-btcusdt-5m-governance-v1", "schema_version": 1},
        created_at=_NOW,
    )
    repository.max_page_count = _page_count(path)

    failed_job: BacktestJob | None = None
    for index in range(1, 65):
        job = _job(tmp_path, index, experiment_id)
        try:
            inserted = repository.register_jobs([job], max_attempts=2, created_at=_NOW)
        except StateRepositoryError as exc:
            assert "database or disk is full" not in str(exc).lower()
            failed_job = job
            break
        except sqlite3.Error as exc:
            pytest.fail(f"raw SQLite error escaped Research repository: {type(exc).__name__}")
        else:
            assert inserted == 1

    assert failed_job is not None, "SQLite boundary was not reached within 64 public registrations"
    integrity, run_count, event_count = _integrity_and_counts(path, failed_job.run_id)
    assert integrity == "ok"
    assert run_count == 0
    assert event_count == 0

    repository.max_page_count = None
    assert repository.register_jobs([failed_job], max_attempts=2, created_at=_NOW) == 1
    assert repository.register_jobs([failed_job], max_attempts=2, created_at=_NOW) == 0

    reopened = SqliteExperimentRepository(path)
    integrity, run_count, event_count = _integrity_and_counts(path, failed_job.run_id)
    assert integrity == "ok"
    assert run_count == 1
    assert event_count == 1
    assert len(reopened.events(failed_job.run_id)) == 1
