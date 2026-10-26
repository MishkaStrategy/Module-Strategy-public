from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import build_auditor

from strategy_audit_validation import EvidenceTamperedError
from strategy_audit_validation.canonical import canonical_sha256


def test_recomputed_unkeyed_chain_cannot_hide_historical_signature_tamper(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    older_decision = auditor.audit(
        evidence_package_reference="artifact:historical-signed-decision",
        evidence=valid_evidence,
    )
    requested_decision = auditor.audit(
        evidence_package_reference="artifact:requested-signed-decision",
        evidence=valid_evidence,
    )
    store.close()

    forged_signature = f"hmac-sha256:{'0' * 64}"
    forged_decision_hash = canonical_sha256(
        {**older_decision.signature_payload(), "signature": forged_signature}
    )

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT request_hash, decision_hash FROM audit_decisions ORDER BY sequence"
    ).fetchall()
    request_hash = str(rows[0][0])
    forged_record_hash = canonical_sha256(
        {
            "request_hash": request_hash,
            "decision_hash": forged_decision_hash,
            "previous_record_hash": None,
        }
    )
    connection.execute(
        """
        UPDATE audit_decisions
        SET signature = ?, decision_hash = ?, record_hash = ?
        WHERE request_hash = ?
        """,
        (forged_signature, forged_decision_hash, forged_record_hash, request_hash),
    )
    later_request_hash = str(rows[1][0])
    later_record_hash = canonical_sha256(
        {
            "request_hash": later_request_hash,
            "decision_hash": str(rows[1][1]),
            "previous_record_hash": forged_record_hash,
        }
    )
    connection.execute(
        """
        UPDATE audit_decisions
        SET previous_record_hash = ?, record_hash = ?
        WHERE request_hash = ?
        """,
        (forged_record_hash, later_record_hash, later_request_hash),
    )
    connection.commit()
    connection.close()

    restarted, restarted_store = build_auditor(path)
    # The attacker recomputed the public SHA-256 chain, so structural integrity
    # alone passes. The process-held HMAC sweep must catch the older record even
    # when the caller asks to verify a later, still-authentic decision.
    restarted_store.verify_integrity()
    with pytest.raises(EvidenceTamperedError, match="signature mismatch"):
        restarted.verify_stored_decision(expected=requested_decision)
    with pytest.raises(EvidenceTamperedError, match="signature mismatch"):
        restarted.audit(
            evidence_package_reference="artifact:different-request",
            evidence=valid_evidence,
        )
    restarted_store.close()
