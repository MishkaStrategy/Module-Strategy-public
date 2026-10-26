"""Independent deterministic Strategy Audit implementation."""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from .canonical import canonical_json_bytes, canonical_sha256
from .contracts import (
    CONTRACT_VERSION,
    SUPPORTED_CONTRACT_VERSIONS,
    AuditDecision,
    AuditOutcome,
    AuditRequestBinding,
    FactoryAdmissionStatus,
)
from .errors import (
    AuditUnavailableError,
    EvidenceIdentityConflictError,
    EvidenceMalformedError,
    EvidenceTamperedError,
)
from .policy import AuditPolicy
from .store import SQLiteAuditStore
from .verification import EvidenceValidator

Clock = Callable[[], datetime]
_AUDIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HmacDecisionSigner:
    """Integrity-bind decisions with a process-supplied key that is never persisted."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("audit HMAC key must contain at least 32 bytes")
        self._key = key

    def sign(self, payload: Mapping[str, Any]) -> str:
        digest = hmac.new(self._key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def verify(self, decision: AuditDecision) -> None:
        expected = self.sign(decision.signature_payload())
        if not hmac.compare_digest(expected, decision.signature):
            raise EvidenceTamperedError("audit decision signature mismatch")

    @staticmethod
    def _request_binding_payload(
        *,
        audit_id: str,
        request_hash: str,
        policy_fingerprint: str,
        decision_hash: str,
    ) -> Mapping[str, Any]:
        return {
            "domain": "strategy-audit-request-binding-v1",
            "schema_version": 1,
            "audit_id": audit_id,
            "request_hash": request_hash,
            "policy_fingerprint": policy_fingerprint,
            "decision_hash": decision_hash,
        }

    @staticmethod
    def request_hash_for_decision(decision: AuditDecision, *, policy_fingerprint: str) -> str:
        return canonical_sha256(
            {
                "evidence_package_reference": decision.evidence_package_reference,
                "evidence_hash": decision.evidence_hash,
                "auditor_id": decision.auditor_id,
                "contract_version": decision.contract_version,
                "policy_fingerprint": policy_fingerprint,
            }
        )

    def create_request_binding(
        self, decision: AuditDecision, *, policy_fingerprint: str
    ) -> AuditRequestBinding:
        """Create an authenticated binding for a newly signed decision."""

        request_hash = self.request_hash_for_decision(
            decision, policy_fingerprint=policy_fingerprint
        )
        if decision.audit_id != f"audit-{request_hash[:24]}":
            raise EvidenceIdentityConflictError("decision audit_id does not commit to its request")
        payload = self._request_binding_payload(
            audit_id=decision.audit_id,
            request_hash=request_hash,
            policy_fingerprint=policy_fingerprint,
            decision_hash=decision.decision_hash,
        )
        return AuditRequestBinding(
            audit_id=decision.audit_id,
            request_hash=request_hash,
            policy_fingerprint=policy_fingerprint,
            decision_hash=decision.decision_hash,
            signature=self.sign(payload),
        )

    def verify_request_binding(self, decision: AuditDecision, binding: AuditRequestBinding) -> None:
        """Verify the original request, policy and decision are jointly bound."""

        if binding.audit_id != decision.audit_id or binding.decision_hash != decision.decision_hash:
            raise EvidenceTamperedError("audit request binding decision identity mismatch")
        expected_request_hash = self.request_hash_for_decision(
            decision, policy_fingerprint=binding.policy_fingerprint
        )
        if (
            binding.request_hash != expected_request_hash
            or decision.audit_id != f"audit-{expected_request_hash[:24]}"
        ):
            raise EvidenceTamperedError("audit request binding request identity mismatch")
        payload = self._request_binding_payload(
            audit_id=binding.audit_id,
            request_hash=binding.request_hash,
            policy_fingerprint=binding.policy_fingerprint,
            decision_hash=binding.decision_hash,
        )
        expected_signature = self.sign(payload)
        if not hmac.compare_digest(expected_signature, binding.signature):
            raise EvidenceTamperedError("audit request binding signature mismatch")


class IndependentStrategyAuditor:
    contract_version = CONTRACT_VERSION

    def __init__(
        self,
        *,
        auditor_id: str,
        policy: AuditPolicy,
        validator: EvidenceValidator,
        signer: HmacDecisionSigner,
        store: SQLiteAuditStore,
        clock: Clock | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> None:
        if not auditor_id or auditor_id.strip() != auditor_id:
            raise ValueError("auditor_id must be a non-blank trimmed string")
        validator_policy = getattr(validator, "_policy", None)
        if validator_policy is not None and validator_policy.fingerprint != policy.fingerprint:
            raise ValueError("validator and auditor policy fingerprints must match")
        if contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            raise ValueError("unsupported audit contract version")
        self.auditor_id = auditor_id
        self.contract_version = contract_version
        self.policy = policy
        self._validator = validator
        self._signer = signer
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def _verify_durable_signatures(self) -> None:
        """Verify every historical HMAC, not only the decision requested for replay.

        The SQLite record chain is intentionally unkeyed so operators can inspect it,
        therefore an attacker with database write access could otherwise rewrite a
        historical decision and recompute the SHA-256 chain. The process-held HMAC
        key is the independent integrity authority and must cover the entire history
        before any new evidence is accepted.
        """

        rows = self._store._connection.execute(
            "SELECT request_hash FROM audit_decisions ORDER BY sequence"
        ).fetchall()
        for row in rows:
            decision = self._store.decision_for_request(str(row["request_hash"]))
            if decision is None:
                raise EvidenceTamperedError("durable audit decision disappeared")
            self._signer.verify(decision)

    def verify_stored_decision(self, *, expected: AuditDecision) -> AuditDecision:
        """Verify an existing signed decision without creating or changing state.

        This establishes stored-chain integrity, HMAC authenticity, and equality
        with the caller's expected immutable identity. It does not interpret the
        outcome as approval: authentic REJECTED and UNAVAILABLE decisions verify
        just as authentic APPROVED_FOR_PAPER decisions do.
        """

        if not isinstance(expected, AuditDecision):
            raise EvidenceMalformedError("expected must be an AuditDecision")
        return self._verify_stored_decision_identity(
            audit_id=expected.audit_id,
            expected=expected,
        )

    def load_verified_stored_decision(self, *, audit_id: str, decision_hash: str) -> AuditDecision:
        """Load a stored decision by its trusted package-bound identity and verify it.

        The returned decision is authentic, regardless of outcome. Callers that
        require PAPER approval must check ``outcome`` separately.
        """

        if not isinstance(audit_id, str) or _AUDIT_ID.fullmatch(audit_id) is None:
            raise EvidenceMalformedError("audit_id must be a portable identifier")
        if not isinstance(decision_hash, str) or _SHA256.fullmatch(decision_hash) is None:
            raise EvidenceMalformedError("decision_hash must be a lowercase SHA-256")
        return self._verify_stored_decision_identity(
            audit_id=audit_id,
            expected_decision_hash=decision_hash,
        )

    def _verify_stored_decision_identity(
        self,
        *,
        audit_id: str,
        expected: AuditDecision | None = None,
        expected_decision_hash: str | None = None,
    ) -> AuditDecision:
        signer = getattr(self, "_signer", None)
        store = getattr(self, "_store", None)
        if not isinstance(signer, HmacDecisionSigner) or not isinstance(store, SQLiteAuditStore):
            raise AuditUnavailableError("stored decision verification authority is unavailable")
        if store.path.is_symlink() or not store.path.is_file():
            raise AuditUnavailableError("audit store is unavailable")

        try:
            store.verify_integrity()
            self._verify_durable_signatures()
            stored = store._decision_by_audit_id(audit_id)
            if stored is None:
                raise EvidenceIdentityConflictError("expected audit decision is not stored")
            signer.verify(stored)
            binding = store._request_binding_by_audit_id(stored.audit_id)
            if binding is None:
                raise AuditUnavailableError(
                    "stored decision has no authenticated request binding (legacy/unverifiable)"
                )
            signer.verify_request_binding(stored, binding)
            if store._stored_request_hash_by_audit_id(stored.audit_id) != binding.request_hash:
                raise EvidenceTamperedError(
                    "stored audit request key differs from authenticated binding"
                )
        except sqlite3.Error as exc:
            raise AuditUnavailableError("audit store is unavailable") from exc
        if expected is not None and stored != expected:
            raise EvidenceIdentityConflictError("stored audit decision identity does not match")
        if expected_decision_hash is not None and stored.decision_hash != expected_decision_hash:
            raise EvidenceIdentityConflictError("stored audit decision hash does not match")
        return stored

    def audit(
        self,
        *,
        evidence_package_reference: str,
        evidence: Mapping[str, Any],
    ) -> AuditDecision:
        now = self._clock().astimezone(UTC)
        candidate = evidence.get("candidate")
        if not isinstance(candidate, Mapping):
            raise EvidenceMalformedError("audit package candidate must be a mapping")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise EvidenceMalformedError("audit package candidate_id is unavailable")

        # Existing durable state and every keyed decision signature are verified
        # before accepting any new evidence.
        self._store.verify_integrity()
        self._verify_durable_signatures()
        evidence_hash = self._store.register_evidence(
            reference=evidence_package_reference,
            payload=evidence,
            candidate_id=candidate_id,
            observed_at=now,
        )
        request_hash = canonical_sha256(
            {
                "evidence_package_reference": evidence_package_reference,
                "evidence_hash": evidence_hash,
                "auditor_id": self.auditor_id,
                "contract_version": self.contract_version,
                "policy_fingerprint": self.policy.fingerprint,
            }
        )
        replay = self._store.decision_for_request(request_hash)
        if replay is not None:
            self._signer.verify(replay)
            if (
                replay.audit_id != f"audit-{request_hash[:24]}"
                or replay.candidate_id != candidate_id
                or replay.evidence_package_reference != evidence_package_reference
                or replay.evidence_hash != evidence_hash
                or replay.auditor_id != self.auditor_id
                or replay.contract_version != self.contract_version
            ):
                raise EvidenceTamperedError("stored audit replay does not match current request")
            binding = self._store._request_binding_by_audit_id(replay.audit_id)
            if binding is not None:
                self._signer.verify_request_binding(replay, binding)
                if (
                    binding.request_hash != request_hash
                    or binding.policy_fingerprint != self.policy.fingerprint
                ):
                    raise EvidenceTamperedError("stored audit replay request binding mismatch")
            self._store.verify_integrity()
            return replay

        try:
            expected_schema = 2 if self.contract_version.endswith("-v2") else 1
            if evidence.get("schema_version") != expected_schema:
                raise EvidenceMalformedError(
                    "audit evidence schema does not match configured contract version"
                )
            validated_candidate, outcome, reasons = self._validator.evaluate(evidence, now=now)
        except AuditUnavailableError as exc:
            validated_candidate = candidate_id
            outcome = AuditOutcome.UNAVAILABLE
            reasons = (
                "AUDITOR_DEPENDENCY_UNAVAILABLE",
                f"DEPENDENCY_{type(exc).__name__.upper()}",
            )
        except EvidenceMalformedError:
            # Malformed or policy-incompatible input receives a durable, explicit
            # rejection. Content-identity conflicts and durable tampering are not
            # converted: they remain hard errors from the store.
            validated_candidate = candidate_id
            outcome = AuditOutcome.REJECTED
            reasons = ("EVIDENCE_MALFORMED", "PAPER_ADMISSION_REJECTED")
        if validated_candidate != candidate_id:
            raise EvidenceMalformedError("validated candidate identity changed")

        audit_id = f"audit-{request_hash[:24]}"
        factory_status = (
            FactoryAdmissionStatus.INSUFFICIENT_EVIDENCE
            if outcome is AuditOutcome.UNAVAILABLE
            else FactoryAdmissionStatus(outcome.value)
        )
        normalized_reasons = tuple(sorted(set(reasons)))
        signature_payload = {
            "audit_id": audit_id,
            "candidate_id": candidate_id,
            "status": factory_status.value,
            "evidence_package_reference": evidence_package_reference,
            "evidence_hash": evidence_hash,
            "reason_codes": normalized_reasons,
            "issued_at": now,
            "auditor_id": self.auditor_id,
            "contract_version": self.contract_version,
            "schema_version": 1,
        }
        signature = self._signer.sign(signature_payload)
        decision = AuditDecision(
            audit_id=audit_id,
            candidate_id=candidate_id,
            outcome=outcome,
            evidence_package_reference=evidence_package_reference,
            evidence_hash=evidence_hash,
            reason_codes=normalized_reasons,
            issued_at=now,
            auditor_id=self.auditor_id,
            contract_version=self.contract_version,
            signature=signature,
            decision_hash=canonical_sha256({**signature_payload, "signature": signature}),
        )
        self._signer.verify(decision)
        binding = self._signer.create_request_binding(
            decision, policy_fingerprint=self.policy.fingerprint
        )
        if binding.request_hash != request_hash:
            raise EvidenceTamperedError("auditor request binding does not match current request")
        stored = self._store.save_bound_decision(
            request_hash=request_hash, decision=decision, binding=binding
        )
        self._store.verify_integrity()
        self._verify_durable_signatures()
        return stored
