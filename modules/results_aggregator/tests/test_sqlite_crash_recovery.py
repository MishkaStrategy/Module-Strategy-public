from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

from modular_results_registry.canonical import canonical_json_bytes
from modular_results_registry.serialization import registration_package_from_mapping
from modular_results_registry.storage import SQLiteRegistry


_STRICT_FLAGS = (
    "audit_sequence_contiguous",
    "audit_revision_monotonic",
    "audit_revision_not_ahead",
    "audit_revision_coverage",
    "audit_event_ids_valid",
    "audit_bindings_valid",
    "audit_timestamps_valid",
    "audit_timestamp_monotonic",
    "operation_revision_coverage",
    "quarantine_records_valid",
    "entity_records_valid",
    "relation_records_valid",
    "annotation_records_valid",
    "annotation_actor_scope_constraint",
    "migration_records_valid",
    "append_only_triggers_present",
    "registered_revisions_valid",
)


def _commit_then_crash(database: str, package_path: str) -> None:
    with open(package_path, encoding="utf-8") as handle:
        package = registration_package_from_mapping(json.load(handle))
    registry = SQLiteRegistry(database)
    outcome = registry.register_package(package)
    assert outcome.state == "REGISTERED"
    os._exit(73)


def _begin_then_crash(database: str) -> None:
    registry = SQLiteRegistry(database)
    registry._connection.execute("BEGIN IMMEDIATE")
    registry._connection.execute("UPDATE registry_state SET revision=99 WHERE singleton=1")
    assert registry._connection.in_transaction
    os._exit(74)


def _run_crashing_process(target, *args: str, expected_exitcode: int) -> None:
    process = multiprocessing.get_context("spawn").Process(target=target, args=args)
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        raise AssertionError("crash-recovery child exceeded its deadline")
    assert process.exitcode == expected_exitcode


def test_committed_registration_survives_process_crash(tmp_path: Path, package) -> None:
    database = tmp_path / "committed-crash.db"
    package_path = tmp_path / "package.json"
    package_path.write_bytes(canonical_json_bytes(package))

    _run_crashing_process(
        _commit_then_crash,
        str(database),
        str(package_path),
        expected_exitcode=73,
    )

    with SQLiteRegistry(database) as registry:
        assert registry.revision() == 1
        assert registry.get_payload("package", package.package_id) is not None
        report = registry.verify_integrity()
        assert report["sqlite_integrity"] == "ok"
        assert report["foreign_key_violations"] == 0
        assert all(report[name] is True for name in _STRICT_FLAGS)


def test_uncommitted_wal_transaction_rolls_back_after_process_crash(tmp_path: Path) -> None:
    database = tmp_path / "uncommitted-crash.db"
    with SQLiteRegistry(database) as registry:
        before_hash = registry.verified_state_hash()
        assert registry.revision() == 0

    _run_crashing_process(
        _begin_then_crash,
        str(database),
        expected_exitcode=74,
    )

    with SQLiteRegistry(database) as registry:
        assert registry.revision() == 0
        assert registry.verified_state_hash() == before_hash
        report = registry.verify_integrity()
        assert report["sqlite_integrity"] == "ok"
        assert report["foreign_key_violations"] == 0
        assert all(report[name] is True for name in _STRICT_FLAGS)
