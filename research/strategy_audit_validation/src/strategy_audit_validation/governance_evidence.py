"""Immutable evidence, detached approval and audit checkpoint contracts."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .errors import EvidenceMalformedError, EvidenceTamperedError
from .governance_common import (
    CompositionCandidate,
    CompositionState,
    _datetime,
    _exact_keys,
    _freeze,
    _git_sha,
    _identifier,
    _mapping,
    _sha,
)


class ApprovalOutcome(StrEnum):
    APPROVED_FOR_PAPER = "APPROVED_FOR_PAPER"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ImmutableEvidence:
    payload: Mapping[str, Any]
    encoded: bytes
    evidence_digest: str
    candidate_id: str
    composition_hash: str
    composition_version: str
    generated_at: datetime
    source_commit_sha: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ImmutableEvidence:
        required = {
            "schema_version",
            "candidate_id",
            "composition_hash",
            "composition_version",
            "generated_at",
            "source_commit_sha",
            "provenance",
            "results",
        }
        _exact_keys(payload, required, "governance evidence")
        if payload["schema_version"] != 1:
            raise EvidenceMalformedError("unsupported governance evidence schema_version")
        candidate_id = _identifier(payload["candidate_id"], "evidence candidate_id")
        composition_hash = _sha(payload["composition_hash"], "evidence composition_hash")
        composition_version = _identifier(
            payload["composition_version"], "evidence composition_version"
        )
        generated_at = _datetime(payload["generated_at"], "evidence generated_at")
        source_commit_sha = _git_sha(payload["source_commit_sha"], "evidence source_commit_sha")
        provenance = _mapping(payload["provenance"], "evidence provenance")
        if provenance.get("utc_only") is not True:
            raise EvidenceMalformedError("evidence provenance must declare utc_only=true")
        if provenance.get("unfinished_htf_allowed") is not False:
            raise EvidenceMalformedError("evidence provenance must forbid unfinished HTF candles")
        if provenance.get("lookahead_allowed") is not False:
            raise EvidenceMalformedError("evidence provenance must forbid look-ahead")
        _identifier(provenance.get("workload_id"), "evidence workload_id")
        _mapping(payload["results"], "evidence results")
        encoded = canonical_json_bytes(payload)
        decoded = strict_json_loads(encoded)
        if not isinstance(decoded, Mapping):
            raise EvidenceMalformedError("governance evidence root must be a mapping")
        return cls(
            payload=_freeze(decoded),
            encoded=encoded,
            evidence_digest=hashlib.sha256(encoded).hexdigest(),
            candidate_id=candidate_id,
            composition_hash=composition_hash,
            composition_version=composition_version,
            generated_at=generated_at,
            source_commit_sha=source_commit_sha,
        )

    def verify_for(
        self,
        candidate: CompositionCandidate,
        *,
        now: datetime,
        maximum_age: timedelta,
    ) -> None:
        if hashlib.sha256(self.encoded).hexdigest() != self.evidence_digest:
            raise EvidenceTamperedError("evidence digest mismatch")
        if canonical_json_bytes(self.payload) != self.encoded:
            raise EvidenceTamperedError("evidence bytes are not canonical or were mutated")
        if self.candidate_id != candidate.candidate_id:
            raise EvidenceMalformedError("evidence belongs to another candidate")
        if self.composition_hash != candidate.composition_hash:
            raise EvidenceMalformedError("evidence composition hash mismatch")
        if self.composition_version != candidate.composition_version:
            raise EvidenceMalformedError("evidence composition version mismatch")
        now_utc = _datetime(now, "evidence verification time")
        age = now_utc - self.generated_at
        if age.total_seconds() < 0:
            raise EvidenceMalformedError("evidence is future-dated")
        if age > maximum_age:
            raise EvidenceMalformedError("evidence is stale")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    candidate_id: str
    composition_hash: str
    evidence_digest: str
    outcome: ApprovalOutcome
    actor_id: str
    actor_role: str
    issued_at: datetime
    signature: str
    approval_digest: str

    def signature_payload(self) -> Mapping[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "composition_hash": self.composition_hash,
            "evidence_digest": self.evidence_digest,
            "outcome": self.outcome.value,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "issued_at": self.issued_at,
            "schema_version": 1,
        }

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "approval candidate_id")
        _sha(self.composition_hash, "approval composition_hash")
        _sha(self.evidence_digest, "approval evidence_digest")
        _identifier(self.actor_id, "approval actor_id")
        if self.actor_role != "independent_auditor":
            raise EvidenceMalformedError("approval actor_role must be independent_auditor")
        object.__setattr__(self, "issued_at", _datetime(self.issued_at, "approval issued_at"))
        if not self.signature.startswith("hmac-sha256:"):
            raise EvidenceMalformedError("approval signature must be HMAC-SHA256")
        _sha(self.approval_digest, "approval_digest")
        expected = canonical_sha256({**self.signature_payload(), "signature": self.signature})
        if self.approval_digest != expected:
            raise EvidenceTamperedError("approval digest mismatch")


class ApprovalAuthority:
    """Detached approval authority; candidates cannot call into their own state."""

    def __init__(self, *, authority_id: str, signing_key: bytes) -> None:
        self.authority_id = _identifier(authority_id, "authority_id")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("approval signing key must contain at least 32 bytes")
        self._signing_key = signing_key

    def issue(
        self,
        *,
        candidate: CompositionCandidate,
        evidence: ImmutableEvidence,
        outcome: ApprovalOutcome,
        actor_id: str,
        actor_role: str,
        now: datetime,
        maximum_evidence_age: timedelta = timedelta(days=7),
    ) -> ApprovalRecord:
        normalized_actor = _identifier(actor_id, "approval actor_id")
        if actor_role != "independent_auditor":
            raise EvidenceMalformedError("only independent_auditor may issue approval")
        if normalized_actor != self.authority_id:
            raise EvidenceMalformedError("approval actor does not match configured authority")
        if normalized_actor == candidate.created_by:
            raise EvidenceMalformedError("candidate creator cannot approve its own candidate")
        if not candidate.complete or candidate.state is not CompositionState.CANDIDATE:
            raise EvidenceMalformedError("incomplete composition cannot be approved")
        evidence.verify_for(candidate, now=now, maximum_age=maximum_evidence_age)
        issued_at = _datetime(now, "approval time")
        payload = {
            "candidate_id": candidate.candidate_id,
            "composition_hash": candidate.composition_hash,
            "evidence_digest": evidence.evidence_digest,
            "outcome": outcome.value,
            "actor_id": normalized_actor,
            "actor_role": actor_role,
            "issued_at": issued_at,
            "schema_version": 1,
        }
        signature = self._sign(payload)
        return ApprovalRecord(
            candidate_id=candidate.candidate_id,
            composition_hash=candidate.composition_hash,
            evidence_digest=evidence.evidence_digest,
            outcome=outcome,
            actor_id=normalized_actor,
            actor_role=actor_role,
            issued_at=issued_at,
            signature=signature,
            approval_digest=canonical_sha256({**payload, "signature": signature}),
        )

    def verify(self, record: ApprovalRecord) -> None:
        expected = self._sign(record.signature_payload())
        if not hmac.compare_digest(expected, record.signature):
            raise EvidenceTamperedError("approval signature mismatch")

    def _sign(self, payload: Mapping[str, Any]) -> str:
        digest = hmac.new(
            self._signing_key,
            canonical_json_bytes(payload),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"


@dataclass(frozen=True, slots=True)
class AuditCheckpoint:
    session_id: str
    candidate_id: str
    evidence_digest: str
    completed_steps: tuple[str, ...]
    checkpoint_digest: str

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        candidate_id: str,
        evidence_digest: str,
        completed_steps: Sequence[str],
    ) -> AuditCheckpoint:
        normalized_steps = tuple(dict.fromkeys(completed_steps))
        for step in normalized_steps:
            _identifier(step, "checkpoint step")
        payload = {
            "session_id": _identifier(session_id, "checkpoint session_id"),
            "candidate_id": _identifier(candidate_id, "checkpoint candidate_id"),
            "evidence_digest": _sha(evidence_digest, "checkpoint evidence_digest"),
            "completed_steps": normalized_steps,
            "schema_version": 1,
        }
        return cls(
            session_id=str(payload["session_id"]),
            candidate_id=str(payload["candidate_id"]),
            evidence_digest=str(payload["evidence_digest"]),
            completed_steps=normalized_steps,
            checkpoint_digest=canonical_sha256(payload),
        )

    def verify(self) -> None:
        expected = canonical_sha256(
            {
                "session_id": self.session_id,
                "candidate_id": self.candidate_id,
                "evidence_digest": self.evidence_digest,
                "completed_steps": self.completed_steps,
                "schema_version": 1,
            }
        )
        if expected != self.checkpoint_digest:
            raise EvidenceTamperedError("checkpoint digest mismatch")


__all__ = [
    "ApprovalAuthority",
    "ApprovalOutcome",
    "ApprovalRecord",
    "AuditCheckpoint",
    "ImmutableEvidence",
]
