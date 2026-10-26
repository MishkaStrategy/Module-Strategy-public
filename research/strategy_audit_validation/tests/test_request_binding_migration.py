from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import build_auditor

from strategy_audit_validation import (
    AuditUnavailableError,
    EvidenceMalformedError,
    EvidenceTamperedError,
)
from strategy_audit_validation.contracts import AuditRequestBinding
from strategy_audit_validation.query import ReadOnlyAuditStore


def _downgrade_to_v1(path: Path) -> None:
    """Create a genuine v1-shaped fixture from a closed synthetic v2 store."""

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TABLE audit_request_bindings")
        connection.execute("UPDATE audit_metadata SET value = '1' WHERE key = 'schema_version'")


def _schema_version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(
            connection.execute(
                "SELECT value FROM audit_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audit_id", "bad id"),
        ("request_hash", "not-a-hash"),
        ("policy_fingerprint", "not-a-hash"),
        ("decision_hash", "not-a-hash"),
        ("signature", "unsigned"),
        ("schema_version", True),
    ],
)
def test_request_binding_contract_rejects_malformed_identity(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "audit_id": "audit-valid",
        "request_hash": "a" * 64,
        "policy_fingerprint": "b" * 64,
        "decision_hash": "c" * 64,
        "signature": "hmac-sha256:" + "d" * 64,
        "schema_version": 1,
    }
    payload[field] = value
    with pytest.raises(EvidenceMalformedError):
        AuditRequestBinding(**payload)  # type: ignore[arg-type]


def test_v1_snapshot_remains_readable_then_migrates_without_backfill(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    decision = auditor.audit(
        evidence_package_reference="artifact:legacy-migration", evidence=valid_evidence
    )
    request_hash = str(
        store._connection.execute(
            "SELECT request_hash FROM audit_decisions WHERE audit_id = ?",
            (decision.audit_id,),
        ).fetchone()[0]
    )
    store.close()
    _downgrade_to_v1(path)

    readonly = ReadOnlyAuditStore(path)
    assert readonly.get_decision(decision.audit_id).decision_hash == decision.decision_hash

    migrated_auditor, migrated_store = build_auditor(path)
    assert _schema_version(path) == "2"
    assert migrated_store.decision_for_request(request_hash) == decision
    assert (
        migrated_store._connection.execute(
            "SELECT COUNT(*) FROM audit_request_bindings"
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(AuditUnavailableError, match="legacy/unverifiable"):
        migrated_auditor.verify_stored_decision(expected=decision)
    migrated_store.close()


def test_migration_failure_rolls_back_binding_table_and_version(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    decision = auditor.audit(
        evidence_package_reference="artifact:migration-rollback", evidence=valid_evidence
    )
    store.close()
    _downgrade_to_v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER reject_schema_upgrade BEFORE UPDATE ON audit_metadata
            BEGIN SELECT RAISE(ABORT, 'synthetic migration failure'); END"""
        )
    before = path.read_bytes()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic migration failure"):
        from strategy_audit_validation import SQLiteAuditStore

        SQLiteAuditStore(path)

    assert _schema_version(path) == "1"
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'audit_request_bindings'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_decisions WHERE audit_id = ?", (decision.audit_id,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "future-version",
        "malformed-version",
        "noncanonical-version",
        "extra-metadata",
        "tables-without-metadata",
        "v1-with-bindings",
        "missing-v2-table",
    ],
)
def test_unsupported_or_incomplete_v2_schema_fails_without_repair(
    tmp_path: Path, corruption: str
) -> None:
    from strategy_audit_validation import SQLiteAuditStore

    path = tmp_path / "audit.sqlite"
    SQLiteAuditStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
        if corruption == "future-version":
            connection.execute("UPDATE audit_metadata SET value = '99'")
        elif corruption == "malformed-version":
            connection.execute("UPDATE audit_metadata SET value = 'not-a-version'")
        elif corruption == "noncanonical-version":
            connection.execute("UPDATE audit_metadata SET value = '02'")
        elif corruption == "extra-metadata":
            connection.execute("INSERT INTO audit_metadata(key, value) VALUES ('extra', 'x')")
        elif corruption == "tables-without-metadata":
            connection.execute("DROP TABLE audit_metadata")
        elif corruption == "v1-with-bindings":
            connection.execute("UPDATE audit_metadata SET value = '1'")
        else:
            connection.execute("DROP TABLE audit_request_bindings")
    before = path.read_bytes()

    with pytest.raises(EvidenceTamperedError):
        SQLiteAuditStore(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'audit_request_bindings'"
        ).fetchone()[0] == (0 if corruption == "missing-v2-table" else 1)
