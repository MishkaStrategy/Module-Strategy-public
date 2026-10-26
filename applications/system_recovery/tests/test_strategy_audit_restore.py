from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from strategy_audit_validation.auditor import HmacDecisionSigner
from strategy_audit_validation.canonical import canonical_sha256
from strategy_audit_validation.contracts import CONTRACT_VERSION, AuditDecision, AuditOutcome
from strategy_audit_validation.errors import (
    EvidenceIdentityConflictError,
    EvidenceTamperedError,
)
from strategy_audit_validation.query import ReadOnlyAuditStore
from strategy_audit_validation.store import SQLiteAuditStore

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "9" * 40
_RELEASE_ID = "strategy-audit-owner-restore"
_KEY = b"strategy-audit-clean-host-recovery-key-0001"
_BASE_TIME = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_POLICY_FINGERPRINT = "a" * 64


def _append_decision(
    store: SQLiteAuditStore,
    signer: HmacDecisionSigner,
    *,
    sequence: int,
    legacy_unbound: bool = False,
) -> tuple[str, str, dict[str, object], AuditDecision]:
    candidate_id = f"candidate-recovery-{sequence:03d}"
    reference = f"evidence-recovery-{sequence:03d}"
    evidence: dict[str, object] = {
        "candidate": {"candidate_id": candidate_id},
        "schema_version": 1,
        "proof": f"clean-host-{sequence}",
    }
    issued_at = _BASE_TIME + timedelta(seconds=sequence)
    evidence_hash = store.register_evidence(
        reference=reference,
        payload=evidence,
        candidate_id=candidate_id,
        observed_at=issued_at,
    )
    if legacy_unbound:
        request_payload = {
            "reference": reference,
            "evidence_hash": evidence_hash,
            "auditor_id": "recovery-auditor",
            "sequence": sequence,
        }
    else:
        request_payload = {
            "evidence_package_reference": reference,
            "evidence_hash": evidence_hash,
            "auditor_id": "recovery-auditor",
            "contract_version": CONTRACT_VERSION,
            "policy_fingerprint": _POLICY_FINGERPRINT,
        }
    request_hash = canonical_sha256(request_payload)
    audit_id = f"audit-{request_hash[:24]}"
    signature_payload = {
        "audit_id": audit_id,
        "candidate_id": candidate_id,
        "status": AuditOutcome.APPROVED_FOR_PAPER.value,
        "evidence_package_reference": reference,
        "evidence_hash": evidence_hash,
        "reason_codes": ("RECOVERY_PROOF",),
        "issued_at": issued_at,
        "auditor_id": "recovery-auditor",
        "contract_version": CONTRACT_VERSION,
        "schema_version": 1,
    }
    signature = signer.sign(signature_payload)
    decision = AuditDecision(
        audit_id=audit_id,
        candidate_id=candidate_id,
        outcome=AuditOutcome.APPROVED_FOR_PAPER,
        evidence_package_reference=reference,
        evidence_hash=evidence_hash,
        reason_codes=("RECOVERY_PROOF",),
        issued_at=issued_at,
        auditor_id="recovery-auditor",
        contract_version=CONTRACT_VERSION,
        signature=signature,
        decision_hash=canonical_sha256({**signature_payload, "signature": signature}),
    )
    if legacy_unbound:
        stored = store.save_legacy_unbound_decision(request_hash=request_hash, decision=decision)
    else:
        binding = signer.create_request_binding(decision, policy_fingerprint=_POLICY_FINGERPRINT)
        stored = store.save_bound_decision(
            request_hash=request_hash, decision=decision, binding=binding
        )
    signer.verify(stored)
    store.verify_integrity()
    return request_hash, reference, evidence, stored


def _clean_host_restore(*, source_db: Path, backup_root: Path, clean_root: Path) -> Path:
    source_root = source_db.parent
    entry = snapshot_entry(
        source_root=source_root,
        backup_root=backup_root,
        spec=SnapshotSpec(
            logical_id="strategy-audit.audit-store",
            owner="strategy-audit",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source=source_db.name,
            restore_target="audit.sqlite3",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(entry,),
    )
    return restore_snapshot_entry(
        backup_root=backup_root,
        restore_root=clean_root,
        manifest=manifest,
        logical_id=entry.logical_id,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )


def test_strategy_audit_clean_host_restore_preserves_chain_and_replay_fences(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    source_db = source_root / "audit.sqlite3"
    signer = HmacDecisionSigner(_KEY)

    with SQLiteAuditStore(source_db) as source:
        request_hash, reference, evidence, decision = _append_decision(
            source,
            signer,
            sequence=1,
        )

    restored_db = _clean_host_restore(
        source_db=source_db,
        backup_root=backup_root,
        clean_root=clean_root,
    )
    with SQLiteAuditStore(restored_db) as restored:
        restored.verify_integrity()
        historical = restored.decision_for_request(request_hash)
        assert historical == decision
        assert historical is not None
        signer.verify(historical)
        binding = restored._request_binding_by_audit_id(decision.audit_id)
        assert binding is not None
        signer.verify_request_binding(historical, binding)

        replay_hash = restored.register_evidence(
            reference=reference,
            payload=evidence,
            candidate_id=decision.candidate_id,
            observed_at=_BASE_TIME + timedelta(minutes=1),
        )
        assert replay_hash == decision.evidence_hash
        binding = signer.create_request_binding(decision, policy_fingerprint=_POLICY_FINGERPRINT)
        assert (
            restored.save_bound_decision(
                request_hash=request_hash, decision=decision, binding=binding
            )
            == decision
        )
        with pytest.raises(EvidenceIdentityConflictError):
            restored.register_evidence(
                reference=reference,
                payload={
                    "candidate": {"candidate_id": decision.candidate_id},
                    "changed": True,
                },
                candidate_id=decision.candidate_id,
                observed_at=_BASE_TIME + timedelta(minutes=2),
            )

        second_hash, _, _, second = _append_decision(restored, signer, sequence=2)
        assert restored.decision_for_request(second_hash) == second
        signer.verify(second)
        restored.verify_integrity()


def test_strategy_audit_v1_restore_remains_readable_but_unbound(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source-v1"
    backup_root = tmp_path / "backup-v1"
    clean_root = tmp_path / "clean-v1"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    source_db = source_root / "audit.sqlite3"
    signer = HmacDecisionSigner(_KEY)

    with SQLiteAuditStore(source_db) as source:
        request_hash, _, _, decision = _append_decision(
            source, signer, sequence=3, legacy_unbound=True
        )
    with sqlite3.connect(source_db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TABLE audit_request_bindings")
        connection.execute("UPDATE audit_metadata SET value = '1' WHERE key = 'schema_version'")

    legacy_reader = ReadOnlyAuditStore(source_db)
    assert legacy_reader.get_decision(decision.audit_id).decision_hash == decision.decision_hash

    restored_db = _clean_host_restore(
        source_db=source_db,
        backup_root=backup_root,
        clean_root=clean_root,
    )
    restored_reader = ReadOnlyAuditStore(restored_db)
    assert restored_reader.get_decision(decision.audit_id).decision_hash == decision.decision_hash
    with SQLiteAuditStore(restored_db) as restored:
        assert restored.decision_for_request(request_hash) == decision
        assert (
            restored._connection.execute("SELECT COUNT(*) FROM audit_request_bindings").fetchone()[
                0
            ]
            == 0
        )
        signer.verify(decision)
        restored.verify_integrity()


def test_strategy_audit_tampered_restored_evidence_is_detected(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    source_db = source_root / "audit.sqlite3"
    signer = HmacDecisionSigner(_KEY)

    with SQLiteAuditStore(source_db) as source:
        _, reference, _, _ = _append_decision(source, signer, sequence=1)

    restored_db = _clean_host_restore(
        source_db=source_db,
        backup_root=backup_root,
        clean_root=clean_root,
    )
    with sqlite3.connect(restored_db) as connection:
        connection.execute(
            "UPDATE audit_evidence SET payload_json = ? WHERE reference = ?",
            (b"{}", reference),
        )

    with (
        SQLiteAuditStore(restored_db) as restored,
        pytest.raises(EvidenceTamperedError),
    ):
        restored.verify_integrity()


def test_strategy_audit_tampered_restored_decision_chain_is_detected(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    source_db = source_root / "audit.sqlite3"
    signer = HmacDecisionSigner(_KEY)

    with SQLiteAuditStore(source_db) as source:
        _append_decision(source, signer, sequence=1)

    restored_db = _clean_host_restore(
        source_db=source_db,
        backup_root=backup_root,
        clean_root=clean_root,
    )
    with sqlite3.connect(restored_db) as connection:
        connection.execute("UPDATE audit_decisions SET record_hash = ?", ("0" * 64,))

    with (
        SQLiteAuditStore(restored_db) as restored,
        pytest.raises(EvidenceTamperedError, match="record hash"),
    ):
        restored.verify_integrity()
