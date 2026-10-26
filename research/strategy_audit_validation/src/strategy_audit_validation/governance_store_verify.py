"""Full durable integrity verification and count reporting."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType

from .canonical import canonical_sha256, strict_json_loads
from .errors import EvidenceTamperedError
from .governance_common import (
    _canonical_stored_mapping,
    _composition_hash_payload,
    _sha,
)
from .governance_evidence import AuditCheckpoint
from .governance_store_base import GovernanceLedgerBase


class GovernanceLedgerVerifyMixin(GovernanceLedgerBase):
    def verify_integrity(self) -> None:
        candidates: dict[str, str] = {}
        rows = self._connection.execute(
            "SELECT * FROM composition_candidates ORDER BY candidate_id"
        ).fetchall()
        for row in rows:
            decoded = _canonical_stored_mapping(bytes(row["payload_json"]), "candidate")
            payload_digest = hashlib.sha256(bytes(row["payload_json"])).hexdigest()
            if payload_digest != str(row["payload_digest"]):
                raise EvidenceTamperedError("stored candidate payload digest mismatch")
            digest = canonical_sha256(_composition_hash_payload(decoded))
            if digest != str(row["composition_hash"]):
                raise EvidenceTamperedError("stored candidate composition hash mismatch")
            candidate_id = f"strategy-candidate-{digest[:24]}"
            if candidate_id != str(row["candidate_id"]):
                raise EvidenceTamperedError("stored candidate identity mismatch")
            candidates[candidate_id] = digest

        evidence: set[str] = set()
        rows = self._connection.execute(
            "SELECT * FROM governance_evidence ORDER BY evidence_digest"
        ).fetchall()
        for row in rows:
            payload_bytes = bytes(row["payload_json"])
            decoded = _canonical_stored_mapping(payload_bytes, "evidence")
            digest = hashlib.sha256(payload_bytes).hexdigest()
            if digest != str(row["evidence_digest"]):
                raise EvidenceTamperedError("stored evidence digest mismatch")
            if str(row["candidate_id"]) not in candidates:
                raise EvidenceTamperedError("stored evidence candidate is missing")
            if decoded.get("candidate_id") != str(row["candidate_id"]):
                raise EvidenceTamperedError("stored evidence candidate mismatch")
            evidence.add(digest)

        rows = self._connection.execute(
            "SELECT * FROM audit_checkpoints ORDER BY session_id"
        ).fetchall()
        for row in rows:
            steps = strict_json_loads(bytes(row["completed_steps_json"]))
            if not isinstance(steps, list) or any(not isinstance(step, str) for step in steps):
                raise EvidenceTamperedError("stored checkpoint steps are malformed")
            checkpoint = AuditCheckpoint(
                session_id=str(row["session_id"]),
                candidate_id=str(row["candidate_id"]),
                evidence_digest=str(row["evidence_digest"]),
                completed_steps=tuple(steps),
                checkpoint_digest=str(row["checkpoint_digest"]),
            )
            checkpoint.verify()
            if checkpoint.candidate_id not in candidates:
                raise EvidenceTamperedError("checkpoint candidate is missing")
            if checkpoint.evidence_digest not in evidence:
                raise EvidenceTamperedError("checkpoint evidence is missing")

        approval_rows = self._connection.execute(
            "SELECT * FROM governance_approvals ORDER BY approval_digest"
        ).fetchall()
        for row in approval_rows:
            payload_bytes = bytes(row["payload_json"])
            decoded = _canonical_stored_mapping(payload_bytes, "approval")
            digest = canonical_sha256(decoded)
            if digest != str(row["approval_digest"]):
                raise EvidenceTamperedError("stored approval digest mismatch")
            if str(row["candidate_id"]) not in candidates:
                raise EvidenceTamperedError("approval candidate is missing")
            if str(row["evidence_digest"]) not in evidence:
                raise EvidenceTamperedError("approval evidence is missing")

        previous: str | None = None
        chain = self._connection.execute(
            "SELECT * FROM governance_chain ORDER BY sequence"
        ).fetchall()
        expected_sequence = 1
        for row in chain:
            if int(row["sequence"]) != expected_sequence:
                raise EvidenceTamperedError("governance chain sequence is not contiguous")
            expected_sequence += 1
            if row["previous_record_hash"] != previous:
                raise EvidenceTamperedError("governance chain linkage mismatch")
            payload_digest = _sha(str(row["payload_digest"]), "stored payload_digest")
            expected = canonical_sha256(
                {
                    "record_kind": str(row["record_kind"]),
                    "record_identity": str(row["record_identity"]),
                    "payload_digest": payload_digest,
                    "previous_record_hash": previous,
                }
            )
            if str(row["record_hash"]) != expected:
                raise EvidenceTamperedError("governance chain record hash mismatch")
            previous = expected

    def counts(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "candidates": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM composition_candidates"
                    ).fetchone()[0]
                ),
                "evidence": int(
                    self._connection.execute("SELECT COUNT(*) FROM governance_evidence").fetchone()[
                        0
                    ]
                ),
                "approvals": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM governance_approvals"
                    ).fetchone()[0]
                ),
                "checkpoints": int(
                    self._connection.execute("SELECT COUNT(*) FROM audit_checkpoints").fetchone()[0]
                ),
            }
        )


__all__ = ["GovernanceLedgerVerifyMixin"]
