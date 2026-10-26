"""Factory-compatible local port without coupling ownership to Strategy Factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .auditor import IndependentStrategyAuditor
from .contracts import CONTRACT_VERSION
from .errors import AuditUnavailableError, EvidenceMalformedError, StrategyAuditError

EvidenceResolver = Callable[[str], Mapping[str, Any]]


class LocalStrategyAuditPort:
    contract_version = CONTRACT_VERSION

    def __init__(
        self,
        *,
        auditor: IndependentStrategyAuditor,
        evidence_resolver: EvidenceResolver,
    ) -> None:
        self._auditor = auditor
        self._resolver = evidence_resolver
        self.contract_version = auditor.contract_version

    def audit(self, evidence_package_reference: str) -> Mapping[str, Any]:
        if not isinstance(evidence_package_reference, str) or not evidence_package_reference:
            raise EvidenceMalformedError("evidence_package_reference must be non-empty")
        try:
            payload = self._resolver(evidence_package_reference)
        except StrategyAuditError:
            raise
        except Exception as exc:
            raise AuditUnavailableError("audit evidence resolver is unavailable") from exc
        if not isinstance(payload, Mapping):
            raise EvidenceMalformedError("resolved audit evidence must be a mapping")
        return self._auditor.audit(
            evidence_package_reference=evidence_package_reference,
            evidence=payload,
        ).as_factory_mapping()
