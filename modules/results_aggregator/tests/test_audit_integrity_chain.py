from __future__ import annotations

import sqlite3
from pathlib import Path

from modular_results_registry.storage import SQLiteRegistry


def test_audit_event_identity_timestamp_and_chain_are_verified(tmp_path: Path, package) -> None:
    database = tmp_path / "registry.db"
    with SQLiteRegistry(database) as registry:
        registry.register_package(package)
        first = registry.verify_integrity()
        second = registry.verify_integrity()
    assert first == second
    assert first["audit_event_ids_valid"] is True
    assert first["audit_timestamps_valid"] is True
    assert first["audit_timestamp_monotonic"] is True
    assert len(first["audit_chain_hash"]) == 64


def test_offline_audit_tampering_is_detected(tmp_path: Path, package) -> None:
    database = tmp_path / "registry.db"
    with SQLiteRegistry(database) as registry:
        registry.register_package(package)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TRIGGER prevent_audit_update")
        connection.execute("UPDATE audit_log SET reason_code='TAMPERED' WHERE sequence=1")
        connection.commit()
    finally:
        connection.close()
    with SQLiteRegistry(database) as registry:
        report = registry.verify_integrity()
    assert report["sqlite_integrity"] == "ok"
    assert report["audit_event_ids_valid"] is False
