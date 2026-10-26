from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import make_package
from modular_results_registry.errors import (
    IntegrityError,
    RegistryError,
    ResourceLimitError,
)
from modular_results_registry.storage import SQLiteRegistry


def _set_version(path: Path, value: object) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (value,))


def test_bootstrap_and_full_legacy_migration_chain(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SQLiteRegistry(path) as registry:
        assert registry.schema_version() == 4
        assert registry.migration_history()[0]["migration_id"] == "bootstrap-v4"
    _set_version(path, "1")
    with SQLiteRegistry(path) as registry:
        assert registry.schema_version() == 4
        assert [item["migration_id"] for item in registry.migration_history()] == [
            "bootstrap-v4",
            "v1-to-v2-annotations",
            "v2-to-v3-metric-definitions",
            "v3-to-v4-actor-scoped-annotations",
        ]
        package = make_package()
        registry.register_package(package)
        registry.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="auditor-a",
            reason="LIMITATION",
            payload={"text": "a"},
        )
        registry.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="auditor-b",
            reason="LIMITATION",
            payload={"text": "a"},
        )
        assert len(registry.annotations(entity_id=package.experiments[0].experiment_id)) == 2


def test_v2_with_metrics_is_refused_without_inference(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SQLiteRegistry(path) as registry:
        registry.register_package(make_package())
    _set_version(path, "2")
    with pytest.raises(RegistryError, match="automatic inference is forbidden"):
        SQLiteRegistry(path)
    assert path.exists()


@pytest.mark.parametrize("value", ["99", "invalid"])
def test_preflight_rejects_unsupported_or_invalid_schema_version(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / f"registry-{value}.sqlite"
    with SQLiteRegistry(path):
        pass
    _set_version(path, value)
    with pytest.raises(RegistryError, match="schema version"):
        SQLiteRegistry(path)


def test_preflight_rejects_missing_schema_metadata(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SQLiteRegistry(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_meta WHERE key='schema_version'")
    with pytest.raises(RegistryError, match="metadata is incomplete"):
        SQLiteRegistry(path)


def test_lifecycle_validation_transaction_and_deadline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        SQLiteRegistry(tmp_path / "bad.sqlite", timeout_seconds=0)
    with SQLiteRegistry(tmp_path / "registry.sqlite") as registry:
        with pytest.raises(ValueError, match="query timeout"):
            with registry.read_deadline(0):
                pass
        with registry.read_deadline(1):
            with pytest.raises(RegistryError, match="nested query"):
                with registry.read_deadline(1):
                    pass
        with pytest.raises(RuntimeError):
            with registry.transaction() as connection:
                connection.execute("CREATE TABLE rollback_probe(value INTEGER)")
                raise RuntimeError("rollback")
        assert (
            registry._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='rollback_probe'"
            ).fetchone()
            is None
        )
        with registry.transaction():
            with pytest.raises(RegistryError, match="nested registry transactions"):
                with registry.transaction():
                    pass


def test_read_deadline_interrupts_expensive_query(tmp_path: Path) -> None:
    with SQLiteRegistry(tmp_path / "registry.sqlite") as registry:
        with pytest.raises(ResourceLimitError, match="execution timeout"):
            with registry.read_deadline(1e-9):
                registry._connection.execute(
                    "WITH RECURSIVE cnt(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM cnt WHERE x<10000000) "
                    "SELECT sum(x) FROM cnt"
                ).fetchone()
        assert registry._read_deadline_active is False
        assert registry._connection.execute("SELECT 1").fetchone()[0] == 1


def test_schema_script_and_migration_collision_helpers(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    SQLiteRegistry._execute_schema_script(
        connection,
        "CREATE TABLE one(value INTEGER);\nINSERT INTO one VALUES(1);\n",
    )
    assert connection.execute("SELECT value FROM one").fetchone()[0] == 1
    with pytest.raises(RegistryError, match="incomplete"):
        SQLiteRegistry._execute_schema_script(connection, "CREATE TABLE broken(")
    connection.close()

    path = tmp_path / "registry.sqlite"
    with SQLiteRegistry(path) as registry:
        with registry.transaction() as db:
            with pytest.raises(RegistryError, match="history collision"):
                SQLiteRegistry._record_migration(db, 0, 4, "bootstrap-v4")


def test_schema_version_and_revision_missing_are_integrity_errors(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SQLiteRegistry(path) as registry:
        registry._connection.execute("DELETE FROM schema_meta WHERE key='schema_version'")
        with pytest.raises(IntegrityError, match="schema version"):
            registry.schema_version()
        registry._connection.execute("DELETE FROM registry_state WHERE singleton=1")
        with pytest.raises(IntegrityError, match="registry_state"):
            registry.revision()
        with registry.transaction() as connection:
            with pytest.raises(IntegrityError, match="registry_state"):
                registry._next_revision(connection)
