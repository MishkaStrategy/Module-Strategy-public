from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_sha256, deterministic_id
from .errors import ValidationError
from .model_common import (
    FindingCode,
    Severity,
    _freeze_mapping,
    _require_enum,
    _unique,
    nonblank,
    validate_id,
)
from .model_metrics import AuditEvidenceRecord, MetricDefinitionRecord, MetricSnapshot, RoleDecision
from .model_records import (
    ArtifactRecord,
    ConfigurationRecord,
    DatasetRecord,
    ExperimentRecord,
    RunRecord,
)


@dataclass(frozen=True, slots=True)
class RegistrationPackage:
    package_id: str
    artifacts: tuple[ArtifactRecord, ...]
    datasets: tuple[DatasetRecord, ...]
    configurations: tuple[ConfigurationRecord, ...]
    experiments: tuple[ExperimentRecord, ...]
    runs: tuple[RunRecord, ...]
    metrics: tuple[MetricSnapshot, ...]
    metric_definitions: tuple[MetricDefinitionRecord, ...] = ()
    schema_version: str = "2"
    producer_module_id: str = "modular-results-registry"
    producer_module_version: str = "0.1.0.dev0"
    evidence: tuple[AuditEvidenceRecord, ...] = ()
    roles: tuple[RoleDecision, ...] = ()

    def __post_init__(self) -> None:
        validate_id("package_id", self.package_id)
        ordering = {
            "artifacts": lambda item: item.artifact_id,
            "datasets": lambda item: item.dataset_id,
            "configurations": lambda item: item.configuration_id,
            "experiments": lambda item: item.experiment_id,
            "runs": lambda item: item.run_id,
            "metric_definitions": lambda item: item.definition_id,
            "metrics": lambda item: item.metric_key,
            "evidence": lambda item: item.evidence_id,
            "roles": lambda item: item.decision_id,
        }
        for field_name, key in ordering.items():
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise ValidationError(f"{field_name} must be a tuple")
            object.__setattr__(self, field_name, tuple(sorted(value, key=key)))
        if self.schema_version != "2":
            raise ValidationError("unsupported registration package schema_version")
        nonblank("producer_module_id", self.producer_module_id)
        nonblank("producer_module_version", self.producer_module_version)
        groups = {
            "artifact_ids": tuple(item.artifact_id for item in self.artifacts),
            "dataset_ids": tuple(item.dataset_id for item in self.datasets),
            "configuration_ids": tuple(item.configuration_id for item in self.configurations),
            "experiment_ids": tuple(item.experiment_id for item in self.experiments),
            "run_ids": tuple(item.run_id for item in self.runs),
            "metric_definition_ids": tuple(item.definition_id for item in self.metric_definitions),
            "metric_keys": tuple(item.metric_key for item in self.metrics),
            "evidence_ids": tuple(item.evidence_id for item in self.evidence),
            "role_ids": tuple(item.decision_id for item in self.roles),
        }
        for name, values in groups.items():
            _unique(name, values)
        if any(item.package_id != self.package_id for item in self.artifacts):
            raise ValidationError("all artifacts must reference the registration package_id")

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    code: FindingCode
    severity: Severity
    blocking: bool
    affected_scope: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    actual_values: Mapping[str, Any]
    evidence_ids: tuple[str, ...]
    recommendation: str

    def __post_init__(self) -> None:
        _require_enum("code", self.code, FindingCode)
        _require_enum("severity", self.severity, Severity)
        if not isinstance(self.blocking, bool):
            raise ValidationError("blocking must be a boolean")
        validate_id("finding_id", self.finding_id)
        for evidence_id in self.evidence_ids:
            validate_id("evidence_id", evidence_id)
        _unique("evidence_ids", self.evidence_ids)
        object.__setattr__(self, "affected_scope", _freeze_mapping(self.affected_scope))
        object.__setattr__(self, "thresholds", _freeze_mapping(self.thresholds))
        object.__setattr__(self, "actual_values", _freeze_mapping(self.actual_values))
        nonblank("recommendation", self.recommendation)

    @classmethod
    def create(
        cls,
        *,
        code: FindingCode,
        severity: Severity,
        blocking: bool,
        affected_scope: Mapping[str, Any],
        thresholds: Mapping[str, Any],
        actual_values: Mapping[str, Any],
        evidence_ids: tuple[str, ...] = (),
        recommendation: str,
    ) -> "Finding":
        payload = {
            "code": code,
            "severity": severity,
            "blocking": blocking,
            "affected_scope": affected_scope,
            "thresholds": thresholds,
            "actual_values": actual_values,
            "evidence_ids": evidence_ids,
            "recommendation": recommendation,
        }
        return cls(
            finding_id=deterministic_id("finding", payload, digest_chars=48),
            code=code,
            severity=severity,
            blocking=blocking,
            affected_scope=affected_scope,
            thresholds=thresholds,
            actual_values=actual_values,
            evidence_ids=evidence_ids,
            recommendation=recommendation,
        )

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)
