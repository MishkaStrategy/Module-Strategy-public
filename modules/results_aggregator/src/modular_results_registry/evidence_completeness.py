from __future__ import annotations

from dataclasses import dataclass

from .canonical import deterministic_id
from .errors import IntegrityError, ValidationError
from .models import AuditEvidenceRecord, CompletenessStatus, EvidenceStatus


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    evidence_type: str
    blocking: bool = True
    minimum_status: EvidenceStatus = EvidenceStatus.PASS

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_type, str) or not self.evidence_type.strip():
            raise ValidationError("evidence_type must be non-blank")
        if not isinstance(self.blocking, bool):
            raise ValidationError("blocking must be boolean")
        if not isinstance(self.minimum_status, EvidenceStatus):
            raise ValidationError("minimum_status must be an EvidenceStatus")
        if self.minimum_status not in {EvidenceStatus.PASS, EvidenceStatus.PARTIAL}:
            raise ValidationError("minimum_status must be PASS or PARTIAL")


@dataclass(frozen=True, slots=True)
class EvidenceCompletenessResult:
    status: CompletenessStatus
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    partial: tuple[str, ...]
    contaminated: tuple[str, ...]
    incompatible: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    result_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, CompletenessStatus):
            raise ValidationError("status must be a CompletenessStatus")
        categories = (
            "satisfied",
            "missing",
            "failed",
            "partial",
            "contaminated",
            "incompatible",
        )
        seen: set[str] = set()
        for name in categories:
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) and item.strip() for item in values
            ):
                raise ValidationError(f"{name} must be a tuple of non-blank strings")
            if len(set(values)) != len(values):
                raise ValidationError(f"{name} must be unique")
            overlap = seen & set(values)
            if overlap:
                raise ValidationError("evidence completeness categories must be disjoint")
            seen.update(values)
        if not isinstance(self.blocking_reasons, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.blocking_reasons
        ):
            raise ValidationError("blocking_reasons must be a tuple of non-blank strings")
        if len(set(self.blocking_reasons)) != len(self.blocking_reasons):
            raise ValidationError("blocking_reasons must be unique")
        payload = {
            "status": self.status,
            "satisfied": self.satisfied,
            "missing": self.missing,
            "failed": self.failed,
            "partial": self.partial,
            "contaminated": self.contaminated,
            "incompatible": self.incompatible,
            "blocking": self.blocking_reasons,
        }
        expected = deterministic_id("completeness", payload, digest_chars=48)
        if self.result_id != expected:
            raise IntegrityError("evidence completeness result ID mismatch")


def evaluate_evidence_completeness(
    evidence: tuple[AuditEvidenceRecord, ...],
    requirements: tuple[EvidenceRequirement, ...],
) -> EvidenceCompletenessResult:
    if not isinstance(evidence, tuple) or not all(
        isinstance(item, AuditEvidenceRecord) for item in evidence
    ):
        raise ValidationError("evidence must be a tuple of AuditEvidenceRecord")
    if not isinstance(requirements, tuple) or not all(
        isinstance(item, EvidenceRequirement) for item in requirements
    ):
        raise ValidationError("requirements must be a tuple of EvidenceRequirement")
    if not requirements:
        raise ValidationError("at least one evidence requirement is required")
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise ValidationError("evidence IDs must be unique")
    if len({item.evidence_type for item in requirements}) != len(requirements):
        raise ValidationError("evidence requirement types must be unique")
    by_type: dict[str, list[AuditEvidenceRecord]] = {}
    for item in evidence:
        by_type.setdefault(item.evidence_type, []).append(item)
    satisfied: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    partial: list[str] = []
    contaminated: list[str] = []
    incompatible: list[str] = []
    blocking: list[str] = []

    for requirement in sorted(requirements, key=lambda item: item.evidence_type):
        items = by_type.get(requirement.evidence_type, [])
        statuses = {item.status for item in items}
        if not items:
            missing.append(requirement.evidence_type)
            if requirement.blocking:
                blocking.append(f"MISSING:{requirement.evidence_type}")
            continue
        if EvidenceStatus.CONTAMINATED in statuses:
            contaminated.append(requirement.evidence_type)
            if requirement.blocking:
                blocking.append(f"CONTAMINATED:{requirement.evidence_type}")
        elif EvidenceStatus.INCOMPATIBLE in statuses:
            incompatible.append(requirement.evidence_type)
            if requirement.blocking:
                blocking.append(f"INCOMPATIBLE:{requirement.evidence_type}")
        elif EvidenceStatus.FAIL in statuses:
            failed.append(requirement.evidence_type)
            if requirement.blocking:
                blocking.append(f"FAILED:{requirement.evidence_type}")
        elif EvidenceStatus.PASS in statuses:
            satisfied.append(requirement.evidence_type)
        elif EvidenceStatus.PARTIAL in statuses:
            if requirement.minimum_status is EvidenceStatus.PARTIAL:
                satisfied.append(requirement.evidence_type)
            else:
                partial.append(requirement.evidence_type)
                if requirement.blocking:
                    blocking.append(f"PARTIAL:{requirement.evidence_type}")
        else:
            missing.append(requirement.evidence_type)
            if requirement.blocking:
                blocking.append(f"MISSING:{requirement.evidence_type}")

    if contaminated:
        status = CompletenessStatus.CONTAMINATED
    elif incompatible:
        status = CompletenessStatus.INCOMPATIBLE
    elif blocking:
        status = CompletenessStatus.BLOCKED
    elif missing or failed:
        status = CompletenessStatus.INSUFFICIENT
    elif partial:
        status = CompletenessStatus.PARTIAL
    else:
        status = CompletenessStatus.COMPLETE
    payload = {
        "status": status,
        "satisfied": tuple(satisfied),
        "missing": tuple(missing),
        "failed": tuple(failed),
        "partial": tuple(partial),
        "contaminated": tuple(contaminated),
        "incompatible": tuple(incompatible),
        "blocking": tuple(blocking),
    }
    return EvidenceCompletenessResult(
        status=status,
        satisfied=tuple(satisfied),
        missing=tuple(missing),
        failed=tuple(failed),
        partial=tuple(partial),
        contaminated=tuple(contaminated),
        incompatible=tuple(incompatible),
        blocking_reasons=tuple(blocking),
        result_id=deterministic_id("completeness", payload, digest_chars=48),
    )
