"""Durable write and restart operations for Strategy Governance."""

from __future__ import annotations

import hashlib

from .canonical import canonical_json_bytes, strict_json_loads
from .errors import (
    EvidenceIdentityConflictError,
    EvidenceMalformedError,
    EvidenceTamperedError,
)
from .governance_common import CompositionCandidate, _identifier, _iso
from .governance_evidence import (
    ApprovalAuthority,
    ApprovalRecord,
    AuditCheckpoint,
    ImmutableEvidence,
)
from .governance_store_base import GovernanceLedgerBase


class GovernanceLedgerWriteMixin(GovernanceLedgerBase):
    def register_candidate(self, candidate: CompositionCandidate) -> bool:
        encoded = canonical_json_bytes(candidate.payload)
        payload_digest = hashlib.sha256(encoded).hexdigest()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT * FROM composition_candidates WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["composition_hash"]) != candidate.composition_hash
                    or str(existing["creator_id"]) != candidate.created_by
                    or bool(existing["complete"]) != candidate.complete
                    or str(existing["payload_digest"]) != payload_digest
                    or bytes(existing["payload_json"]) != encoded
                ):
                    raise EvidenceIdentityConflictError(
                        "candidate identity was reused with different content"
                    )
                self._connection.execute("COMMIT")
                return False
            self._connection.execute(
                """
                INSERT INTO composition_candidates(
                    candidate_id, composition_hash, creator_id, complete,
                    payload_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.composition_hash,
                    candidate.created_by,
                    int(candidate.complete),
                    payload_digest,
                    encoded,
                ),
            )
            self._append_chain("candidate", candidate.candidate_id, payload_digest)
            self._connection.execute("COMMIT")
            return True
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def register_evidence(self, evidence: ImmutableEvidence) -> bool:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            candidate = self._connection.execute(
                "SELECT candidate_id FROM composition_candidates WHERE candidate_id = ?",
                (evidence.candidate_id,),
            ).fetchone()
            if candidate is None:
                raise EvidenceMalformedError("evidence candidate is not registered")
            existing = self._connection.execute(
                "SELECT * FROM governance_evidence WHERE evidence_digest = ?",
                (evidence.evidence_digest,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["candidate_id"]) != evidence.candidate_id
                    or str(existing["source_commit_sha"]) != evidence.source_commit_sha
                    or bytes(existing["payload_json"]) != evidence.encoded
                ):
                    raise EvidenceIdentityConflictError(
                        "evidence digest was reused with different content"
                    )
                self._connection.execute("COMMIT")
                return False
            self._connection.execute(
                """
                INSERT INTO governance_evidence(
                    evidence_digest, candidate_id, generated_at,
                    source_commit_sha, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_digest,
                    evidence.candidate_id,
                    _iso(evidence.generated_at),
                    evidence.source_commit_sha,
                    evidence.encoded,
                ),
            )
            self._append_chain("evidence", evidence.evidence_digest, evidence.evidence_digest)
            self._connection.execute("COMMIT")
            return True
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def save_checkpoint(self, checkpoint: AuditCheckpoint) -> None:
        checkpoint.verify()
        encoded_steps = canonical_json_bytes(checkpoint.completed_steps)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT * FROM audit_checkpoints WHERE session_id = ?",
                (checkpoint.session_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["candidate_id"]) != checkpoint.candidate_id
                    or str(existing["evidence_digest"]) != checkpoint.evidence_digest
                ):
                    raise EvidenceIdentityConflictError(
                        "checkpoint session was rebound to another audit"
                    )
                previous_steps = strict_json_loads(bytes(existing["completed_steps_json"]))
                if not isinstance(previous_steps, list):
                    raise EvidenceTamperedError("stored checkpoint steps are malformed")
                if not set(previous_steps).issubset(checkpoint.completed_steps):
                    raise EvidenceMalformedError("checkpoint progress cannot move backwards")
                if (
                    str(existing["checkpoint_digest"]) == checkpoint.checkpoint_digest
                    and bytes(existing["completed_steps_json"]) == encoded_steps
                ):
                    self._connection.execute("COMMIT")
                    return
                self._connection.execute(
                    """
                    UPDATE audit_checkpoints
                    SET completed_steps_json = ?, checkpoint_digest = ?
                    WHERE session_id = ?
                    """,
                    (encoded_steps, checkpoint.checkpoint_digest, checkpoint.session_id),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO audit_checkpoints(
                        session_id, candidate_id, evidence_digest,
                        completed_steps_json, checkpoint_digest
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.session_id,
                        checkpoint.candidate_id,
                        checkpoint.evidence_digest,
                        encoded_steps,
                        checkpoint.checkpoint_digest,
                    ),
                )
            self._append_chain(
                "checkpoint",
                f"{checkpoint.session_id}:{checkpoint.checkpoint_digest}",
                checkpoint.checkpoint_digest,
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def resume_checkpoint(
        self,
        *,
        session_id: str,
        candidate_id: str,
        evidence_digest: str,
    ) -> AuditCheckpoint:
        row = self._connection.execute(
            "SELECT * FROM audit_checkpoints WHERE session_id = ?",
            (_identifier(session_id, "resume session_id"),),
        ).fetchone()
        if row is None:
            raise EvidenceMalformedError("checkpoint session does not exist")
        if str(row["candidate_id"]) != candidate_id:
            raise EvidenceMalformedError("checkpoint candidate digest changed")
        if str(row["evidence_digest"]) != evidence_digest:
            raise EvidenceMalformedError("checkpoint evidence digest changed")
        steps = strict_json_loads(bytes(row["completed_steps_json"]))
        if not isinstance(steps, list) or any(not isinstance(step, str) for step in steps):
            raise EvidenceTamperedError("stored checkpoint steps are malformed")
        checkpoint = AuditCheckpoint(
            session_id=session_id,
            candidate_id=candidate_id,
            evidence_digest=evidence_digest,
            completed_steps=tuple(steps),
            checkpoint_digest=str(row["checkpoint_digest"]),
        )
        checkpoint.verify()
        return checkpoint

    def record_approval(
        self,
        record: ApprovalRecord,
        *,
        authority: ApprovalAuthority,
    ) -> bool:
        authority.verify(record)
        encoded = canonical_json_bytes(
            {**record.signature_payload(), "signature": record.signature}
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT payload_json FROM governance_approvals WHERE approval_digest = ?",
                (record.approval_digest,),
            ).fetchone()
            if existing is not None:
                if bytes(existing["payload_json"]) != encoded:
                    raise EvidenceIdentityConflictError(
                        "approval digest was reused with different content"
                    )
                self._connection.execute("COMMIT")
                return False
            self._connection.execute(
                """
                INSERT INTO governance_approvals(
                    approval_digest, candidate_id, evidence_digest,
                    actor_id, outcome, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.approval_digest,
                    record.candidate_id,
                    record.evidence_digest,
                    record.actor_id,
                    record.outcome.value,
                    encoded,
                ),
            )
            self._append_chain("approval", record.approval_digest, record.approval_digest)
            self._connection.execute("COMMIT")
            return True
        except Exception:
            self._connection.execute("ROLLBACK")
            raise


__all__ = ["GovernanceLedgerWriteMixin"]
