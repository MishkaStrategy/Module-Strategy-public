"""Runtime bridge from Strategy Factory artifacts to the independent auditor."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from strategy_audit_validation import (
    CONTRACT_VERSION,
    AuditUnavailableError,
    IndependentStrategyAuditor,
    LocalStrategyAuditPort,
    StrategyAuditError,
)

from .audit_envelope import FactoryArtifactEvidenceResolver
from .canonical import canonical_json_bytes, strict_json_loads
from .errors import CompatibilityError, ContractError, ExternalComponentUnavailableError
from .run import StrategyFactoryRun


class IndependentStrategyAuditPort:
    """Factory-facing adapter over the separately owned local audit port."""

    contract_version = CONTRACT_VERSION

    def __init__(
        self,
        *,
        auditor: IndependentStrategyAuditor,
        run: StrategyFactoryRun,
    ) -> None:
        if not isinstance(auditor, IndependentStrategyAuditor):
            raise TypeError("auditor must be IndependentStrategyAuditor")
        if not isinstance(run, StrategyFactoryRun):
            raise TypeError("run must be StrategyFactoryRun")
        self._port = LocalStrategyAuditPort(
            auditor=auditor,
            evidence_resolver=FactoryArtifactEvidenceResolver(run),
        )
        self.contract_version = self._port.contract_version

    def audit(self, evidence_package_reference: str) -> Mapping[str, Any]:
        try:
            decision = self._port.audit(evidence_package_reference)
        except AuditUnavailableError as exc:
            raise ExternalComponentUnavailableError(
                "independent Strategy Auditor is unavailable"
            ) from exc
        except StrategyAuditError as exc:
            raise CompatibilityError("independent Strategy Auditor rejected evidence") from exc
        if not isinstance(decision, Mapping):
            raise CompatibilityError("independent Strategy Auditor returned a malformed decision")
        try:
            normalized = strict_json_loads(canonical_json_bytes(decision))
        except ContractError as exc:
            raise CompatibilityError(
                "independent Strategy Auditor returned a non-canonical decision"
            ) from exc
        if not isinstance(normalized, dict) or any(not isinstance(key, str) for key in normalized):
            raise CompatibilityError("independent Strategy Auditor returned a malformed decision")
        return MappingProxyType(normalized)


__all__ = ["IndependentStrategyAuditPort"]
