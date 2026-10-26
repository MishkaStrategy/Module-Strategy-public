from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from strategy_audit_validation import (
    AuditDecision,
    AuditOutcome,
    HmacDecisionSigner,
    SQLiteAuditStore,
)
from strategy_audit_validation.canonical import canonical_sha256
from strategy_audit_validation.query import (
    ReadOnlyAuditNotFound,
    ReadOnlyAuditStore,
    ReadOnlyAuditStoreError,
)

NOW = datetime(2026, 8, 7, 12, 50, tzinfo=UTC)


def _seed(path: Path) -> None:
    signer = HmacDecisionSigner(b"audit-readonly-test-key-32-bytes!!")
    with SQLiteAuditStore(path) as store:
        evidence = {
            "candidate": {"candidate_id": "candidate-1"},
            "private_host_path": "/Users/private/evidence",
            "private_note": "do-not-project",
        }
        evidence_hash = store.register_evidence(
            reference="evidence://candidate-1/package-1",
            payload=evidence,
            candidate_id="candidate-1",
            observed_at=NOW,
        )
        request_hash = canonical_sha256(
            {
                "reference": "evidence://candidate-1/package-1",
                "evidence_hash": evidence_hash,
                "auditor": "independent-auditor",
            }
        )
        signature_payload = {
            "audit_id": "audit-readonly-1",
            "candidate_id": "candidate-1",
            "status": "APPROVED_FOR_PAPER",
            "evidence_package_reference": "evidence://candidate-1/package-1",
            "evidence_hash": evidence_hash,
            "reason_codes": ("PAPER_ADMISSION_APPROVED",),
            "issued_at": NOW,
            "auditor_id": "independent-auditor",
            "contract_version": "strategy-audit-port-v1",
            "schema_version": 1,
        }
        signature = signer.sign(signature_payload)
        decision = AuditDecision(
            audit_id="audit-readonly-1",
            candidate_id="candidate-1",
            outcome=AuditOutcome.APPROVED_FOR_PAPER,
            evidence_package_reference="evidence://candidate-1/package-1",
            evidence_hash=evidence_hash,
            reason_codes=("PAPER_ADMISSION_APPROVED",),
            issued_at=NOW,
            auditor_id="independent-auditor",
            contract_version="strategy-audit-port-v1",
            signature=signature,
            decision_hash=canonical_sha256({**signature_payload, "signature": signature}),
        )
        store.save_legacy_unbound_decision(request_hash=request_hash, decision=decision)
        store.verify_integrity()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fault_inject(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(statement, parameters)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode is None or str(mode[0]).lower() != "delete":
            raise AssertionError("fault injection failed to restore quiescent SQLite journal mode")


def test_read_only_audit_store_projects_metadata_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    _seed(path)
    before_hash = _sha(path)
    before_mtime = path.stat().st_mtime_ns
    before_entries = tuple(sorted(item.name for item in tmp_path.iterdir()))

    reader = ReadOnlyAuditStore(path)
    integrity = reader.verify_integrity()
    decisions = reader.list_decisions()
    evidence = reader.list_evidence()

    assert integrity.evidence_count == 1
    assert integrity.decision_count == 1
    assert integrity.chain_head is not None
    assert integrity.hash_chain_verified is True
    assert integrity.hmac_signatures_reverified is False
    assert len(decisions) == 1
    assert decisions[0].outcome is AuditOutcome.APPROVED_FOR_PAPER
    assert decisions[0].candidate_id == "candidate-1"
    assert decisions[0].reason_codes == ("PAPER_ADMISSION_APPROVED",)
    assert decisions[0].hmac_signature_present is True
    assert decisions[0].hmac_signature_reverified is False
    assert len(evidence) == 1
    assert evidence[0].candidate_id == "candidate-1"
    assert evidence[0].decision_count == 1

    rendered = repr((integrity, decisions, evidence))
    assert "private_host_path" not in rendered
    assert "/Users/private/evidence" not in rendered
    assert "do-not-project" not in rendered
    assert "hmac-sha256:" not in rendered

    assert _sha(path) == before_hash
    assert path.stat().st_mtime_ns == before_mtime
    assert tuple(sorted(item.name for item in tmp_path.iterdir())) == before_entries


def test_read_only_audit_store_missing_path_is_side_effect_free(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "audit.sqlite3"

    with pytest.raises(ReadOnlyAuditStoreError, match="does not exist"):
        ReadOnlyAuditStore(path)

    assert not path.exists()
    assert not path.parent.exists()


def test_read_only_audit_store_rejects_symlink(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    _seed(path)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(path)

    with pytest.raises(ReadOnlyAuditStoreError, match="symlink"):
        ReadOnlyAuditStore(linked)


def test_read_only_audit_store_rejects_active_sqlite_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    _seed(path)
    sidecar = Path(str(path) + "-wal")
    sidecar.write_bytes(b"active-wal-canary")

    with pytest.raises(ReadOnlyAuditStoreError, match="not quiescent"):
        ReadOnlyAuditStore(path)

    assert sidecar.read_bytes() == b"active-wal-canary"


def test_read_only_audit_store_fails_closed_on_chain_tamper(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    _seed(path)
    _fault_inject(
        path,
        "UPDATE audit_decisions SET record_hash = ? WHERE audit_id = ?",
        ("f" * 64, "audit-readonly-1"),
    )

    with pytest.raises(ReadOnlyAuditStoreError, match="record hash mismatch"):
        ReadOnlyAuditStore(path)


def test_read_only_audit_store_returns_typed_not_found(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    _seed(path)
    reader = ReadOnlyAuditStore(path)

    with pytest.raises(ReadOnlyAuditNotFound):
        reader.get_decision("audit-missing")
    with pytest.raises(ReadOnlyAuditNotFound):
        reader.get_evidence("evidence://missing")


def test_read_only_v2_schema_requires_binding_table_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    _seed(path)
    _fault_inject(path, "DROP TABLE audit_request_bindings")
    before_hash = _sha(path)
    before_entries = tuple(sorted(item.name for item in tmp_path.iterdir()))

    with pytest.raises(ReadOnlyAuditStoreError, match="binding table is missing"):
        ReadOnlyAuditStore(path)

    assert _sha(path) == before_hash
    assert tuple(sorted(item.name for item in tmp_path.iterdir())) == before_entries
