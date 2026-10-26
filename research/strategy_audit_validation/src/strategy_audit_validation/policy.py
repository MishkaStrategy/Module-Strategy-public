"""Deterministic independent paper-admission policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .canonical import canonical_sha256
from .errors import EvidenceMalformedError


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    maximum_evidence_age_seconds: int = 7 * 24 * 60 * 60
    maximum_risk_fraction: Decimal = Decimal("0.05")
    maximum_drawdown_fraction: Decimal = Decimal("0.50")
    minimum_results_packages: int = 1
    require_funding_lineage_for_futures: bool = True
    require_external_dataset_verifier: bool = True
    require_external_results_verifier: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_evidence_age_seconds, bool)
            or not isinstance(self.maximum_evidence_age_seconds, int)
            or self.maximum_evidence_age_seconds < 1
        ):
            raise EvidenceMalformedError("maximum_evidence_age_seconds must be positive")
        for value, name in (
            (self.maximum_risk_fraction, "maximum_risk_fraction"),
            (self.maximum_drawdown_fraction, "maximum_drawdown_fraction"),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not Decimal("0") < value <= 1
            ):
                raise EvidenceMalformedError(f"{name} must be finite and in (0, 1]")
        if (
            isinstance(self.minimum_results_packages, bool)
            or not isinstance(self.minimum_results_packages, int)
            or self.minimum_results_packages < 1
        ):
            raise EvidenceMalformedError("minimum_results_packages must be positive")
        for name in (
            "require_funding_lineage_for_futures",
            "require_external_dataset_verifier",
            "require_external_results_verifier",
        ):
            if not isinstance(getattr(self, name), bool):
                raise EvidenceMalformedError(f"{name} must be bool")
        if self.schema_version != 1:
            raise EvidenceMalformedError("unsupported AuditPolicy schema_version")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)
