"""Versioned independent audit contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .canonical import canonical_sha256
from .errors import EvidenceMalformedError, EvidenceTamperedError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
CONTRACT_VERSION = "strategy-audit-port-v1"
CONTRACT_VERSION_V2 = "strategy-audit-port-v2"
SUPPORTED_CONTRACT_VERSIONS = frozenset({CONTRACT_VERSION, CONTRACT_VERSION_V2})


class AuditOutcome(StrEnum):
    APPROVED_FOR_PAPER = "APPROVED_FOR_PAPER"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


class FactoryAdmissionStatus(StrEnum):
    APPROVED_FOR_PAPER = "APPROVED_FOR_PAPER"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class AuditRequestBinding:
    """Keyed binding between a stored decision and its original audit request."""

    audit_id: str
    request_hash: str
    policy_fingerprint: str
    decision_hash: str
    signature: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.audit_id, str) or _ID.fullmatch(self.audit_id) is None:
            raise EvidenceMalformedError("binding audit_id must be a portable identifier")
        for value, name in (
            (self.request_hash, "request_hash"),
            (self.policy_fingerprint, "policy_fingerprint"),
            (self.decision_hash, "decision_hash"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise EvidenceMalformedError(f"binding {name} must be a lowercase SHA-256")
        if not isinstance(self.signature, str) or not self.signature.startswith("hmac-sha256:"):
            raise EvidenceMalformedError("binding signature must be HMAC-SHA256 encoded")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise EvidenceMalformedError("unsupported audit request-binding schema_version")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verified: bool
    reason_codes: tuple[str, ...]
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool):
            raise EvidenceMalformedError("verified must be bool")
        reasons = tuple(sorted(set(self.reason_codes)))
        if not reasons:
            raise EvidenceMalformedError("verification result requires reason codes")
        if any(not isinstance(item, str) or not item.strip() for item in reasons):
            raise EvidenceMalformedError("verification reason codes must be non-empty strings")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True, slots=True)
class AuditDecision:
    audit_id: str
    candidate_id: str
    outcome: AuditOutcome
    evidence_package_reference: str
    evidence_hash: str
    reason_codes: tuple[str, ...]
    issued_at: datetime
    auditor_id: str
    contract_version: str
    signature: str
    decision_hash: str
    live_trading_authorized: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.audit_id, "audit_id"),
            (self.candidate_id, "candidate_id"),
            (self.auditor_id, "auditor_id"),
        ):
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise EvidenceMalformedError(f"{name} must be a portable identifier")
        if (
            not isinstance(self.evidence_package_reference, str)
            or not self.evidence_package_reference
        ):
            raise EvidenceMalformedError("evidence_package_reference must be non-empty")
        for value, name in (
            (self.evidence_hash, "evidence_hash"),
            (self.decision_hash, "decision_hash"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise EvidenceMalformedError(f"{name} must be a lowercase SHA-256")
        if self.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            raise EvidenceMalformedError("unsupported audit contract version")
        if not isinstance(self.signature, str) or not self.signature.startswith("hmac-sha256:"):
            raise EvidenceMalformedError("signature must be HMAC-SHA256 encoded")
        reasons = tuple(sorted(set(self.reason_codes)))
        if not reasons:
            raise EvidenceMalformedError("audit decision requires reason codes")
        object.__setattr__(self, "reason_codes", reasons)
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise EvidenceMalformedError("issued_at must be timezone-aware")
        object.__setattr__(self, "issued_at", self.issued_at.astimezone(UTC))
        if self.live_trading_authorized:
            raise EvidenceMalformedError("independent auditor cannot authorize live trading")
        if self.schema_version != 1:
            raise EvidenceMalformedError("unsupported AuditDecision schema_version")
        if canonical_sha256(self.hash_payload()) != self.decision_hash:
            raise EvidenceTamperedError("audit decision hash mismatch")

    @property
    def factory_status(self) -> FactoryAdmissionStatus:
        if self.outcome is AuditOutcome.UNAVAILABLE:
            return FactoryAdmissionStatus.INSUFFICIENT_EVIDENCE
        return FactoryAdmissionStatus(self.outcome.value)

    def signature_payload(self) -> Mapping[str, Any]:
        return {
            "audit_id": self.audit_id,
            "candidate_id": self.candidate_id,
            "status": self.factory_status.value,
            "evidence_package_reference": self.evidence_package_reference,
            "evidence_hash": self.evidence_hash,
            "reason_codes": self.reason_codes,
            "issued_at": self.issued_at,
            "auditor_id": self.auditor_id,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
        }

    def hash_payload(self) -> Mapping[str, Any]:
        return {**self.signature_payload(), "signature": self.signature}

    def as_factory_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "audit_id": self.audit_id,
                "candidate_id": self.candidate_id,
                "status": self.factory_status.value,
                "evidence_package_reference": self.evidence_package_reference,
                "evidence_hash": self.evidence_hash,
                "reason_codes": self.reason_codes,
                "issued_at": self.issued_at,
                "auditor_id": self.auditor_id,
                "contract_version": self.contract_version,
                "signature": self.signature,
                "decision_hash": self.decision_hash,
                "schema_version": 1,
            }
        )
