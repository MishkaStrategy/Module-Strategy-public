from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modular_research_runner import _repository_impl
from modular_research_runner.repository import SqliteExperimentRepository


class _FailingSchemaRepository(SqliteExperimentRepository):
    @staticmethod
    def _install_claim_schema(connection: sqlite3.Connection) -> None:
        SqliteExperimentRepository._install_claim_schema(connection)
        raise RuntimeError("injected schema failure")


def _schema_objects(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return set(
            connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )


def test_new_repository_schema_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite3"
    with pytest.raises(RuntimeError, match="injected schema failure"):
        _FailingSchemaRepository(path)
    assert _schema_objects(path) == set()
    recovered = SqliteExperimentRepository(path)
    assert recovered.SCHEMA_VERSION == 2
    assert ("table", "run_claims") in _schema_objects(path)
    assert ("trigger", "trg_run_claim_cleanup") in _schema_objects(path)


def test_v1_to_v2_migration_rolls_back_schema_and_version_together(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    _repository_impl.SqliteExperimentRepository(path)
    before = _schema_objects(path)
    with pytest.raises(RuntimeError, match="injected schema failure"):
        _FailingSchemaRepository(path)
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM repository_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == "1"
    assert _schema_objects(path) == before
    SqliteExperimentRepository(path)
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM repository_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == "2"
    assert ("table", "run_claims") in _schema_objects(path)
    assert ("trigger", "trg_run_claim_cleanup") in _schema_objects(path)
