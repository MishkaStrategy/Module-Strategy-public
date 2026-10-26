from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import build_auditor

from strategy_audit_validation import (
    AuditDecision,
    AuditOutcome,
    AuditUnavailableError,
    EvidenceIdentityConflictError,
    EvidenceMalformedError,
    EvidenceTamperedError,
    HmacDecisionSigner,
    IndependentStrategyAuditor,
    SQLiteAuditStore,
    UnavailableDatasetLineageVerifier,
)
from strategy_audit_validation.canonical import canonical_sha256
from strategy_audit_validation.contracts import AuditRequestBinding


def _saved_decision(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    *,
    reject: bool = False,
    unavailable: bool = False,
) -> tuple[IndependentStrategyAuditor, SQLiteAuditStore, AuditDecision]:
    auditor_options = (
        {"dataset_verifier": UnavailableDatasetLineageVerifier()} if unavailable else {}
    )
    auditor, store = build_auditor(tmp_path / "audit.sqlite", **auditor_options)
    if reject:
        valid_evidence["execution_assumptions"]["mode"] = "live"
    decision = auditor.audit(
        evidence_package_reference="artifact:stored-decision",
        evidence=valid_evidence,
    )
    return auditor, store, decision


def test_stored_decision_verification_is_read_only_and_status_neutral(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    cases = (
        ("approved", False, False, AuditOutcome.APPROVED_FOR_PAPER),
        ("rejected", True, False, AuditOutcome.REJECTED),
        ("unavailable", False, True, AuditOutcome.UNAVAILABLE),
    )
    for name, reject, unavailable, outcome in cases:
        run_dir = tmp_path / name
        run_dir.mkdir()
        auditor, store, decision = _saved_decision(
            run_dir,
            valid_evidence.copy(),
            reject=reject,
            unavailable=unavailable,
        )
        before_changes = store._connection.total_changes
        before_entries = sorted(path.name for path in run_dir.iterdir())
        before_counts = (
            store._connection.execute("SELECT COUNT(*) FROM audit_evidence").fetchone()[0],
            store._connection.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[0],
        )

        verified = auditor.verify_stored_decision(expected=decision)
        loaded = auditor.load_verified_stored_decision(
            audit_id=decision.audit_id,
            decision_hash=decision.decision_hash,
        )

        assert verified == decision
        assert loaded == decision
        assert loaded is not verified
        assert verified.outcome is outcome
        assert store._connection.total_changes == before_changes
        assert sorted(path.name for path in run_dir.iterdir()) == before_entries
        assert before_counts == (
            store._connection.execute("SELECT COUNT(*) FROM audit_evidence").fetchone()[0],
            store._connection.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[0],
        )
        store.close()


def test_wrong_process_key_and_tampered_row_fail_closed(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    wrong_key_auditor = IndependentStrategyAuditor(
        auditor_id=auditor.auditor_id,
        policy=auditor.policy,
        validator=auditor._validator,
        signer=HmacDecisionSigner(b"different-key-that-is-at-least-32-bytes"),
        store=store,
    )
    with pytest.raises(EvidenceTamperedError, match="signature"):
        wrong_key_auditor.verify_stored_decision(expected=decision)

    store._connection.execute(
        "UPDATE audit_decisions SET signature = ? WHERE audit_id = ?",
        ("hmac-sha256:" + "0" * 64, decision.audit_id),
    )
    with pytest.raises(EvidenceTamperedError):
        auditor.verify_stored_decision(expected=decision)
    store.close()


def test_request_hash_rewrite_with_recomputed_public_chain_is_detected(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    row = store._connection.execute(
        "SELECT previous_record_hash FROM audit_decisions WHERE audit_id = ?",
        (decision.audit_id,),
    ).fetchone()
    previous = row["previous_record_hash"]
    forged_request_hash = "f" * 64
    forged_record_hash = canonical_sha256(
        {
            "request_hash": forged_request_hash,
            "decision_hash": decision.decision_hash,
            "previous_record_hash": previous,
        }
    )
    # Model a DB editor with foreign_keys disabled who changes the row key and
    # recomputes the public chain, while leaving the signer-authenticated binding.
    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE audit_decisions SET request_hash = ?, record_hash = ? WHERE audit_id = ?",
        (forged_request_hash, forged_record_hash, decision.audit_id),
    )
    connection.commit()
    connection.close()

    store.verify_integrity()
    with pytest.raises(EvidenceTamperedError, match="request key"):
        auditor.verify_stored_decision(expected=decision)
    store.close()


def test_missing_binding_never_downgrades_to_legacy_strong_verification(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    store._connection.execute(
        "DELETE FROM audit_request_bindings WHERE audit_id = ?", (decision.audit_id,)
    )
    with pytest.raises(AuditUnavailableError, match="legacy/unverifiable"):
        auditor.verify_stored_decision(expected=decision)
    with pytest.raises(AuditUnavailableError, match="legacy/unverifiable"):
        auditor.load_verified_stored_decision(
            audit_id=decision.audit_id,
            decision_hash=decision.decision_hash,
        )
    store.close()


def test_legacy_unbound_decision_remains_replayable_but_unverifiable(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    binding = store._request_binding_by_audit_id(decision.audit_id)
    assert binding is not None
    store._connection.execute(
        "DELETE FROM audit_request_bindings WHERE audit_id = ?", (decision.audit_id,)
    )

    replay = auditor.audit(
        evidence_package_reference="artifact:stored-decision", evidence=valid_evidence
    )
    assert replay == decision
    with pytest.raises(AuditUnavailableError, match="legacy/unverifiable"):
        auditor.verify_stored_decision(expected=decision)
    store.close()


def test_malformed_binding_rows_fail_closed(tmp_path: Path, valid_evidence: dict[str, Any]) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    store._connection.execute(
        "UPDATE audit_request_bindings SET schema_version = 'invalid' WHERE audit_id = ?",
        (decision.audit_id,),
    )
    with pytest.raises(EvidenceTamperedError, match="binding is malformed"):
        auditor.verify_stored_decision(expected=decision)
    store.close()


def test_request_binding_authenticates_decision_and_policy_context(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    binding = store._request_binding_by_audit_id(decision.audit_id)
    assert binding is not None

    changed_payload = dict(decision.signature_payload())
    changed_payload["audit_id"] = "audit-different"
    changed_signature = auditor._signer.sign(changed_payload)
    changed_decision = replace(
        decision,
        audit_id="audit-different",
        signature=changed_signature,
        decision_hash=canonical_sha256({**changed_payload, "signature": changed_signature}),
    )
    with pytest.raises(EvidenceIdentityConflictError, match="audit_id"):
        auditor._signer.create_request_binding(
            changed_decision,
            policy_fingerprint=auditor.policy.fingerprint,
        )

    with pytest.raises(EvidenceTamperedError, match="decision identity"):
        auditor._signer.verify_request_binding(decision, replace(binding, decision_hash="e" * 64))
    with pytest.raises(EvidenceTamperedError, match="request identity"):
        auditor._signer.verify_request_binding(decision, replace(binding, request_hash="f" * 64))
    with pytest.raises(EvidenceTamperedError, match="signature"):
        auditor._signer.verify_request_binding(
            decision, replace(binding, signature="hmac-sha256:" + "0" * 64)
        )
    store.close()


def test_existing_bound_decision_cannot_be_replayed_with_different_binding(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    binding = store._request_binding_by_audit_id(decision.audit_id)
    assert binding is not None
    alternate_payload = auditor._signer._request_binding_payload(
        audit_id=binding.audit_id,
        request_hash=binding.request_hash,
        policy_fingerprint="e" * 64,
        decision_hash=binding.decision_hash,
    )
    alternate = AuditRequestBinding(
        audit_id=binding.audit_id,
        request_hash=binding.request_hash,
        policy_fingerprint="e" * 64,
        decision_hash=binding.decision_hash,
        signature=auditor._signer.sign(alternate_payload),
    )

    with pytest.raises(EvidenceTamperedError, match="stored request binding"):
        store.save_bound_decision(
            request_hash=binding.request_hash, decision=decision, binding=alternate
        )
    assert store.decision_for_request(binding.request_hash) == decision
    assert store._request_binding_by_audit_id(decision.audit_id) == binding
    store.close()


def test_store_rejects_binding_that_does_not_match_decision_identity(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    _auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    binding = store._request_binding_by_audit_id(decision.audit_id)
    assert binding is not None
    with pytest.raises(EvidenceIdentityConflictError, match="request binding"):
        store.save_bound_decision(
            request_hash=binding.request_hash,
            decision=decision,
            binding=replace(binding, decision_hash="e" * 64),
        )
    store.close()


def test_historical_signature_sweep_fails_if_a_decision_disappears(
    tmp_path: Path, valid_evidence: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor, store, _decision = _saved_decision(tmp_path, valid_evidence)
    monkeypatch.setattr(store, "decision_for_request", lambda _request_hash: None)
    with pytest.raises(EvidenceTamperedError, match="disappeared"):
        auditor._verify_durable_signatures()
    store.close()


def test_authenticated_replay_must_match_current_candidate_and_binding(
    tmp_path: Path, valid_evidence: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    payload = dict(decision.signature_payload())
    payload["candidate_id"] = "candidate-other"
    signature = auditor._signer.sign(payload)
    forged_context = replace(
        decision,
        candidate_id="candidate-other",
        signature=signature,
        decision_hash=canonical_sha256({**payload, "signature": signature}),
    )
    monkeypatch.setattr(store, "decision_for_request", lambda _request_hash: forged_context)
    with pytest.raises(EvidenceTamperedError, match="replay does not match current request"):
        auditor.audit(
            evidence_package_reference="artifact:stored-decision", evidence=valid_evidence
        )
    store.close()


def test_audit_rejects_signer_binding_that_does_not_commit_to_request(
    tmp_path: Path, valid_evidence: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    create_binding = auditor._signer.create_request_binding

    def mismatched_binding(*args: Any, **kwargs: Any) -> AuditRequestBinding:
        return replace(create_binding(*args, **kwargs), request_hash="f" * 64)

    monkeypatch.setattr(auditor._signer, "create_request_binding", mismatched_binding)
    with pytest.raises(EvidenceTamperedError, match="does not match current request"):
        auditor.audit(
            evidence_package_reference="artifact:binding-mismatch", evidence=valid_evidence
        )
    assert store._connection.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[0] == 0
    store.close()


def test_decision_and_binding_commit_atomically(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    store._connection.execute(
        """CREATE TRIGGER reject_request_binding BEFORE INSERT ON audit_request_bindings
        BEGIN SELECT RAISE(ABORT, 'synthetic binding insert failure'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic binding insert failure"):
        auditor.audit(evidence_package_reference="artifact:atomic-binding", evidence=valid_evidence)
    assert store._connection.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[0] == 0
    assert (
        store._connection.execute("SELECT COUNT(*) FROM audit_request_bindings").fetchone()[0] == 0
    )
    store.verify_integrity()
    store.close()


def test_wrong_expected_identity_and_missing_signer_fail_closed(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    payload = dict(decision.signature_payload())
    payload["candidate_id"] = "different-candidate"
    signature = auditor._signer.sign(payload)
    expected = replace(
        decision,
        candidate_id="different-candidate",
        signature=signature,
        decision_hash=canonical_sha256({**payload, "signature": signature}),
    )
    with pytest.raises(EvidenceIdentityConflictError, match="identity"):
        auditor.verify_stored_decision(expected=expected)

    auditor._signer = None  # type: ignore[assignment]
    with pytest.raises(AuditUnavailableError, match="authority"):
        auditor.verify_stored_decision(expected=decision)
    store.close()


def test_missing_expected_record_and_malformed_input_fail_closed(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    payload = dict(decision.signature_payload())
    payload["audit_id"] = "audit-not-stored"
    signature = auditor._signer.sign(payload)
    absent = replace(
        decision,
        audit_id="audit-not-stored",
        signature=signature,
        decision_hash=canonical_sha256({**payload, "signature": signature}),
    )
    with pytest.raises(EvidenceIdentityConflictError, match="not stored"):
        auditor.verify_stored_decision(expected=absent)
    with pytest.raises(EvidenceIdentityConflictError, match="not stored"):
        auditor.load_verified_stored_decision(
            audit_id=absent.audit_id,
            decision_hash=decision.decision_hash,
        )
    with pytest.raises(EvidenceMalformedError, match="AuditDecision"):
        auditor.verify_stored_decision(expected=object())  # type: ignore[arg-type]
    store.close()


def test_direct_verified_lookup_checks_hash_and_identity_inputs(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)

    with pytest.raises(EvidenceIdentityConflictError, match="hash"):
        auditor.load_verified_stored_decision(
            audit_id=decision.audit_id,
            decision_hash="f" * 64,
        )
    for audit_id, decision_hash in (
        (" ", decision.decision_hash),
        (decision.audit_id, decision.decision_hash.upper()),
        (decision.audit_id, "not-a-hash"),
    ):
        with pytest.raises(EvidenceMalformedError):
            auditor.load_verified_stored_decision(
                audit_id=audit_id,
                decision_hash=decision_hash,
            )
    store.close()


def test_direct_verified_lookup_sweeps_historical_signatures(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, historical = _saved_decision(tmp_path, valid_evidence.copy())
    target = auditor.audit(
        evidence_package_reference="artifact:second-stored-decision",
        evidence=valid_evidence.copy(),
    )
    assert target.audit_id != historical.audit_id
    rows = store._connection.execute(
        """SELECT audit_id, request_hash, previous_record_hash
        FROM audit_decisions ORDER BY sequence"""
    ).fetchall()
    previous_record_hash: str | None = None
    for row in rows:
        signature = "hmac-sha256:" + "0" * 64 if row["audit_id"] == historical.audit_id else None
        if signature is None:
            decision_hash = store._decision_by_audit_id(row["audit_id"]).decision_hash
        else:
            decision_hash = canonical_sha256(
                {**historical.signature_payload(), "signature": signature}
            )
        record_hash = canonical_sha256(
            {
                "request_hash": row["request_hash"],
                "decision_hash": decision_hash,
                "previous_record_hash": previous_record_hash,
            }
        )
        if signature is not None:
            store._connection.execute(
                """UPDATE audit_decisions
                SET signature = ?, decision_hash = ?, previous_record_hash = ?, record_hash = ?
                WHERE audit_id = ?""",
                (signature, decision_hash, previous_record_hash, record_hash, row["audit_id"]),
            )
        elif row["previous_record_hash"] != previous_record_hash:
            store._connection.execute(
                """UPDATE audit_decisions
                SET previous_record_hash = ?, record_hash = ? WHERE audit_id = ?""",
                (previous_record_hash, record_hash, row["audit_id"]),
            )
        previous_record_hash = record_hash
    store.verify_integrity()

    with pytest.raises(EvidenceTamperedError, match="signature"):
        auditor.load_verified_stored_decision(
            audit_id=target.audit_id,
            decision_hash=target.decision_hash,
        )
    store.close()


def test_closed_store_fails_as_unavailable_without_recreating_it(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    path = store.path
    store.close()

    with pytest.raises(AuditUnavailableError, match="store"):
        auditor.verify_stored_decision(expected=decision)
    with pytest.raises(AuditUnavailableError, match="store"):
        auditor.load_verified_stored_decision(
            audit_id=decision.audit_id,
            decision_hash=decision.decision_hash,
        )
    assert path.is_file()


def test_missing_store_does_not_create_database_or_directory(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    missing_dir = tmp_path / "absent" / "nested"
    path = missing_dir / "audit.sqlite"
    auditor, store, decision = _saved_decision(tmp_path / "existing", valid_evidence)
    store.close()

    # Point the existing auditor at a path that has never existed. The verifier
    # must check availability before SQLite can create a file or parent directory.
    missing_store = type(store).__new__(type(store))
    missing_store.path = path
    missing_store._connection = sqlite3.connect(":memory:")
    auditor._store = missing_store
    with pytest.raises(AuditUnavailableError, match="store"):
        auditor.verify_stored_decision(expected=decision)
    assert not missing_dir.exists()
    assert not path.exists()
    missing_store.close()


def test_direct_lookup_rejects_blank_id_and_symlink_store(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    _auditor, store, decision = _saved_decision(tmp_path, valid_evidence)
    with pytest.raises(ValueError, match="audit_id"):
        store._decision_by_audit_id(" ")

    symlink = tmp_path / "audit-alias.sqlite"
    symlink.symlink_to(store.path)
    store.path = symlink
    with pytest.raises(sqlite3.OperationalError, match="unavailable"):
        store._decision_by_audit_id(decision.audit_id)
    store.close()
