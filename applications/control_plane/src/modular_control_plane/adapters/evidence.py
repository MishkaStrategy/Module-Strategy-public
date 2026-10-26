from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from ..evidence import (
    AuditDecisionSnapshot,
    AuditEvidenceSnapshot,
    AuditIntegritySnapshot,
    EvidenceStateInvalid,
    ResultsEvidenceSnapshot,
    ResultsIntegritySnapshot,
)


class _Value(Protocol):
    @property
    def value(self) -> str: ...


class _AuditIntegrity(Protocol):
    evidence_count: int
    decision_count: int
    chain_head: str | None


class _AuditDecision(Protocol):
    sequence: int
    audit_id: str
    candidate_id: str
    outcome: _Value
    evidence_reference: str
    evidence_hash: str
    reason_codes: tuple[str, ...]
    issued_at: datetime
    auditor_id: str
    contract_version: str
    decision_hash: str


class _AuditEvidence(Protocol):
    reference: str
    evidence_hash: str
    candidate_id: str
    first_seen_at: datetime
    decision_count: int


class _AuditRepository(Protocol):
    def verify_integrity(self) -> _AuditIntegrity: ...

    def list_decisions(self, *, limit: int, offset: int) -> tuple[_AuditDecision, ...]: ...

    def list_evidence(self, *, limit: int, offset: int) -> tuple[_AuditEvidence, ...]: ...


class _ResultsIntegrity(Protocol):
    registry_revision: int
    evidence_count: int


class _ResultsEvidence(Protocol):
    evidence_id: str
    experiment_id: str
    evidence_type: str
    producer: str
    producer_version: str
    status: _Value
    severity: _Value
    created_at: datetime
    evidence_hash: str
    content_hash: str
    registered_revision: int
    run_count: int
    artifact_count: int
    limitations_count: int


class _ResultsRepository(Protocol):
    def verify_integrity(self) -> _ResultsIntegrity: ...

    def list_evidence(self, *, limit: int, offset: int) -> tuple[_ResultsEvidence, ...]: ...


def _existing_file(value: str | Path, label: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    if not raw.exists() or not raw.is_file():
        raise ValueError(f"{label} must be an existing regular file")
    return raw.resolve(strict=True)


class StrategyAuditEvidenceReader:
    def __init__(self, state_path: str | Path) -> None:
        self._path = _existing_file(state_path, "Strategy Audit database")
        self._repository: _AuditRepository | None = None

    def integrity(self) -> AuditIntegritySnapshot:
        try:
            value = self._reader().verify_integrity()
        except Exception as exc:
            raise EvidenceStateInvalid("Strategy Audit ledger failed read-only validation") from exc
        return AuditIntegritySnapshot(
            evidence_count=value.evidence_count,
            decision_count=value.decision_count,
            chain_head=value.chain_head,
        )

    def list_decisions(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[AuditDecisionSnapshot, ...]:
        try:
            values = self._reader().list_decisions(limit=limit, offset=offset)
        except Exception as exc:
            raise EvidenceStateInvalid("Strategy Audit decisions failed validation") from exc
        return tuple(
            AuditDecisionSnapshot(
                sequence=item.sequence,
                audit_id=item.audit_id,
                candidate_id=item.candidate_id,
                outcome=item.outcome.value,
                evidence_reference=item.evidence_reference,
                evidence_hash=item.evidence_hash,
                reason_codes=item.reason_codes,
                issued_at=item.issued_at,
                auditor_id=item.auditor_id,
                contract_version=item.contract_version,
                decision_hash=item.decision_hash,
            )
            for item in values
        )

    def list_evidence(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[AuditEvidenceSnapshot, ...]:
        try:
            values = self._reader().list_evidence(limit=limit, offset=offset)
        except Exception as exc:
            raise EvidenceStateInvalid("Strategy Audit evidence failed validation") from exc
        return tuple(
            AuditEvidenceSnapshot(
                reference=item.reference,
                evidence_hash=item.evidence_hash,
                candidate_id=item.candidate_id,
                first_seen_at=item.first_seen_at,
                decision_count=item.decision_count,
            )
            for item in values
        )

    def _reader(self) -> _AuditRepository:
        if self._repository is None:
            try:
                from strategy_audit_validation.query import ReadOnlyAuditStore
            except ImportError as exc:
                raise EvidenceStateInvalid(
                    "Strategy Audit read-only dependency is unavailable"
                ) from exc
            try:
                native = ReadOnlyAuditStore(self._path)
            except Exception as exc:
                raise EvidenceStateInvalid("Strategy Audit ledger failed validation") from exc
            self._repository = cast(_AuditRepository, native)
        return self._repository


class ResultsRegistryEvidenceReader:
    def __init__(self, state_path: str | Path) -> None:
        self._path = _existing_file(state_path, "Results Aggregator registry")
        self._repository: _ResultsRepository | None = None

    def integrity(self) -> ResultsIntegritySnapshot:
        try:
            value = self._reader().verify_integrity()
        except Exception as exc:
            raise EvidenceStateInvalid("Results evidence registry failed validation") from exc
        return ResultsIntegritySnapshot(
            registry_revision=value.registry_revision,
            evidence_count=value.evidence_count,
        )

    def list_evidence(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ResultsEvidenceSnapshot, ...]:
        try:
            values = self._reader().list_evidence(limit=limit, offset=offset)
        except Exception as exc:
            raise EvidenceStateInvalid("Results evidence rows failed validation") from exc
        return tuple(
            ResultsEvidenceSnapshot(
                evidence_id=item.evidence_id,
                experiment_id=item.experiment_id,
                evidence_type=item.evidence_type,
                producer=item.producer,
                producer_version=item.producer_version,
                status=item.status.value,
                severity=item.severity.value,
                created_at=item.created_at,
                evidence_hash=item.evidence_hash,
                content_hash=item.content_hash,
                registered_revision=item.registered_revision,
                run_count=item.run_count,
                artifact_count=item.artifact_count,
                limitations_count=item.limitations_count,
            )
            for item in values
        )

    def _reader(self) -> _ResultsRepository:
        if self._repository is None:
            try:
                from modular_results_registry.evidence_query import ReadOnlyEvidenceRegistry
            except ImportError as exc:
                raise EvidenceStateInvalid("Results evidence dependency is unavailable") from exc
            try:
                native = ReadOnlyEvidenceRegistry(self._path)
            except Exception as exc:
                raise EvidenceStateInvalid("Results evidence registry failed validation") from exc
            self._repository = cast(_ResultsRepository, native)
        return self._repository
