from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ComponentHealth, ComponentId, ContractModel, HealthStatus, SafeScalar
from .service import Clock, utc_now


class AuditOutcome(StrEnum):
    APPROVED_FOR_PAPER = "APPROVED_FOR_PAPER"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


class ResultsEvidenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONTAMINATED = "CONTAMINATED"
    INCOMPATIBLE = "INCOMPATIBLE"


class EvidenceSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AuditLedgerIntegrity(ContractModel):
    evidence_count: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    chain_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    hash_chain_verified: Literal[True] = True
    hmac_signatures_reverified: Literal[False] = False

    @model_validator(mode="after")
    def validate_chain_head(self) -> AuditLedgerIntegrity:
        if self.decision_count == 0 and self.chain_head is not None:
            raise ValueError("empty audit ledger cannot publish a chain head")
        if self.decision_count > 0 and self.chain_head is None:
            raise ValueError("non-empty audit ledger requires a chain head")
        return self


class AuditDecisionSummary(ContractModel):
    sequence: int = Field(ge=1)
    audit_id: str = Field(min_length=1, max_length=128)
    candidate_id: str = Field(min_length=1, max_length=128)
    outcome: AuditOutcome
    evidence_reference: str = Field(min_length=1, max_length=512)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[str, ...]
    issued_at: datetime
    auditor_id: str = Field(min_length=1, max_length=128)
    contract_version: Literal["strategy-audit-port-v1"] = "strategy-audit-port-v1"
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash_chain_verified: Literal[True] = True
    hmac_signature_present: Literal[True] = True
    hmac_signature_reverified: Literal[False] = False

    @field_validator("issued_at")
    @classmethod
    def require_aware_issued_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issued_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(value) <= 64:
            raise ValueError("reason_codes must contain 1..64 values")
        normalized = tuple(sorted(set(value)))
        if normalized != value:
            raise ValueError("reason_codes must be unique and sorted")
        if any(not item or len(item) > 128 for item in value):
            raise ValueError("reason_codes are invalid")
        return value


class AuditDecisionList(ContractModel):
    decisions: tuple[AuditDecisionSummary, ...]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool


class AuditEvidenceSummary(ContractModel):
    reference: str = Field(min_length=1, max_length=512)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(min_length=1, max_length=128)
    first_seen_at: datetime
    decision_count: int = Field(ge=0)
    content_integrity_verified: Literal[True] = True

    @field_validator("first_seen_at")
    @classmethod
    def require_aware_first_seen_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("first_seen_at must be timezone-aware")
        return value.astimezone(UTC)


class AuditEvidenceList(ContractModel):
    evidence: tuple[AuditEvidenceSummary, ...]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool


class ResultsEvidenceSummary(ContractModel):
    evidence_id: str = Field(min_length=1, max_length=192)
    experiment_id: str = Field(min_length=1, max_length=192)
    evidence_type: str = Field(min_length=1, max_length=256)
    producer: str = Field(min_length=1, max_length=256)
    producer_version: str = Field(min_length=1, max_length=128)
    status: ResultsEvidenceStatus
    severity: EvidenceSeverity
    created_at: datetime
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_revision: int = Field(ge=0)
    run_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    limitations_count: int = Field(ge=0)
    content_integrity_verified: Literal[True] = True

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)


class ResultsEvidenceList(ContractModel):
    evidence: tuple[ResultsEvidenceSummary, ...]
    registry_revision: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool


class EvidenceOverview(ContractModel):
    audit_configured: bool
    results_registry_configured: bool
    audit_integrity: AuditLedgerIntegrity | None
    results_registry_revision: int | None = Field(default=None, ge=0)
    results_evidence_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_sources(self) -> EvidenceOverview:
        if self.audit_configured != (self.audit_integrity is not None):
            raise ValueError("audit source shape is inconsistent")
        results_present = self.results_registry_revision is not None
        if results_present != (self.results_evidence_count is not None):
            raise ValueError("results evidence source shape is inconsistent")
        if self.results_registry_configured != results_present:
            raise ValueError("results registry source shape is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class AuditIntegritySnapshot:
    evidence_count: int
    decision_count: int
    chain_head: str | None


@dataclass(frozen=True, slots=True)
class AuditDecisionSnapshot:
    sequence: int
    audit_id: str
    candidate_id: str
    outcome: str
    evidence_reference: str
    evidence_hash: str
    reason_codes: tuple[str, ...]
    issued_at: datetime
    auditor_id: str
    contract_version: str
    decision_hash: str


@dataclass(frozen=True, slots=True)
class AuditEvidenceSnapshot:
    reference: str
    evidence_hash: str
    candidate_id: str
    first_seen_at: datetime
    decision_count: int


@dataclass(frozen=True, slots=True)
class ResultsIntegritySnapshot:
    registry_revision: int
    evidence_count: int


@dataclass(frozen=True, slots=True)
class ResultsEvidenceSnapshot:
    evidence_id: str
    experiment_id: str
    evidence_type: str
    producer: str
    producer_version: str
    status: str
    severity: str
    created_at: datetime
    evidence_hash: str
    content_hash: str
    registered_revision: int
    run_count: int
    artifact_count: int
    limitations_count: int


class AuditEvidenceReader(Protocol):
    def integrity(self) -> AuditIntegritySnapshot: ...

    def list_decisions(self, *, limit: int, offset: int) -> tuple[AuditDecisionSnapshot, ...]: ...

    def list_evidence(self, *, limit: int, offset: int) -> tuple[AuditEvidenceSnapshot, ...]: ...


class ResultsEvidenceReader(Protocol):
    def integrity(self) -> ResultsIntegritySnapshot: ...

    def list_evidence(self, *, limit: int, offset: int) -> tuple[ResultsEvidenceSnapshot, ...]: ...


class EvidenceSourceNotConfigured(RuntimeError):
    pass


class EvidenceStateInvalid(RuntimeError):
    pass


class EvidenceControlService:
    component = ComponentId.EVIDENCE

    def __init__(
        self,
        *,
        audit_reader: AuditEvidenceReader | None = None,
        results_reader: ResultsEvidenceReader | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if audit_reader is None and results_reader is None:
            raise ValueError("at least one evidence source is required")
        self._audit = audit_reader
        self._results = results_reader
        self._clock = clock

    def health(self) -> ComponentHealth:
        failures: list[str] = []
        details: dict[str, SafeScalar] = {
            "audit_configured": self._audit is not None,
            "results_registry_configured": self._results is not None,
        }
        if self._audit is not None:
            try:
                audit = self._audit.integrity()
                details.update(
                    {
                        "audit_evidence_count": audit.evidence_count,
                        "audit_decision_count": audit.decision_count,
                        "audit_hash_chain_verified": True,
                        "audit_hmac_reverified": False,
                    }
                )
            except Exception:
                failures.append("strategy_audit")
        if self._results is not None:
            try:
                results = self._results.integrity()
                details.update(
                    {
                        "results_evidence_count": results.evidence_count,
                        "results_registry_revision": results.registry_revision,
                    }
                )
            except Exception:
                failures.append("results_registry")
        configured = int(self._audit is not None) + int(self._results is not None)
        if len(failures) == configured:
            status = HealthStatus.UNAVAILABLE
            summary = "Evidence sources are unavailable"
        elif failures:
            status = HealthStatus.DEGRADED
            summary = "Evidence is partially available"
        else:
            status = HealthStatus.HEALTHY
            summary = "Evidence sources are readable"
        if failures:
            details["failed_sources"] = ",".join(sorted(failures))
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=self._now(),
            summary=summary,
            details=details,
        )

    def overview(self) -> EvidenceOverview:
        audit = self._audit.integrity() if self._audit is not None else None
        results = self._results.integrity() if self._results is not None else None
        return EvidenceOverview(
            audit_configured=self._audit is not None,
            results_registry_configured=self._results is not None,
            audit_integrity=(
                AuditLedgerIntegrity(
                    evidence_count=audit.evidence_count,
                    decision_count=audit.decision_count,
                    chain_head=audit.chain_head,
                )
                if audit is not None
                else None
            ),
            results_registry_revision=(results.registry_revision if results is not None else None),
            results_evidence_count=(results.evidence_count if results is not None else None),
        )

    def list_audit_decisions(self, *, limit: int = 50, offset: int = 0) -> AuditDecisionList:
        reader = self._require_audit()
        _validate_page(limit=limit, offset=offset)
        snapshots = reader.list_decisions(limit=limit, offset=offset)
        has_more = len(snapshots) == limit and bool(
            reader.list_decisions(limit=1, offset=offset + limit)
        )
        return AuditDecisionList(
            decisions=tuple(self._public_decision(item) for item in snapshots),
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    def list_audit_evidence(self, *, limit: int = 50, offset: int = 0) -> AuditEvidenceList:
        reader = self._require_audit()
        _validate_page(limit=limit, offset=offset)
        snapshots = reader.list_evidence(limit=limit, offset=offset)
        has_more = len(snapshots) == limit and bool(
            reader.list_evidence(limit=1, offset=offset + limit)
        )
        return AuditEvidenceList(
            evidence=tuple(self._public_audit_evidence(item) for item in snapshots),
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    def list_results_evidence(self, *, limit: int = 50, offset: int = 0) -> ResultsEvidenceList:
        reader = self._require_results()
        _validate_page(limit=limit, offset=offset)
        integrity = reader.integrity()
        snapshots = reader.list_evidence(limit=limit, offset=offset)
        has_more = len(snapshots) == limit and bool(
            reader.list_evidence(limit=1, offset=offset + limit)
        )
        return ResultsEvidenceList(
            evidence=tuple(self._public_results_evidence(item) for item in snapshots),
            registry_revision=integrity.registry_revision,
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    @staticmethod
    def _public_decision(snapshot: AuditDecisionSnapshot) -> AuditDecisionSummary:
        if snapshot.contract_version != "strategy-audit-port-v1":
            raise EvidenceStateInvalid("Strategy Audit contract version is unsupported")
        return AuditDecisionSummary(
            sequence=snapshot.sequence,
            audit_id=snapshot.audit_id,
            candidate_id=snapshot.candidate_id,
            outcome=AuditOutcome(snapshot.outcome),
            evidence_reference=snapshot.evidence_reference,
            evidence_hash=snapshot.evidence_hash,
            reason_codes=snapshot.reason_codes,
            issued_at=snapshot.issued_at,
            auditor_id=snapshot.auditor_id,
            contract_version="strategy-audit-port-v1",
            decision_hash=snapshot.decision_hash,
        )

    @staticmethod
    def _public_audit_evidence(snapshot: AuditEvidenceSnapshot) -> AuditEvidenceSummary:
        return AuditEvidenceSummary(
            reference=snapshot.reference,
            evidence_hash=snapshot.evidence_hash,
            candidate_id=snapshot.candidate_id,
            first_seen_at=snapshot.first_seen_at,
            decision_count=snapshot.decision_count,
        )

    @staticmethod
    def _public_results_evidence(snapshot: ResultsEvidenceSnapshot) -> ResultsEvidenceSummary:
        return ResultsEvidenceSummary(
            evidence_id=snapshot.evidence_id,
            experiment_id=snapshot.experiment_id,
            evidence_type=snapshot.evidence_type,
            producer=snapshot.producer,
            producer_version=snapshot.producer_version,
            status=ResultsEvidenceStatus(snapshot.status),
            severity=EvidenceSeverity(snapshot.severity),
            created_at=snapshot.created_at,
            evidence_hash=snapshot.evidence_hash,
            content_hash=snapshot.content_hash,
            registered_revision=snapshot.registered_revision,
            run_count=snapshot.run_count,
            artifact_count=snapshot.artifact_count,
            limitations_count=snapshot.limitations_count,
        )

    def _require_audit(self) -> AuditEvidenceReader:
        if self._audit is None:
            raise EvidenceSourceNotConfigured("Strategy Audit evidence source is not configured")
        return self._audit

    def _require_results(self) -> ResultsEvidenceReader:
        if self._results is None:
            raise EvidenceSourceNotConfigured("Results evidence source is not configured")
        return self._results

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _validate_page(*, limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be non-negative")
