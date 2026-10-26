from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .canonical import canonical_sha256, deterministic_id
from .errors import ValidationError
from .model_common import (
    CandidateRole,
    EvidenceStatus,
    ObjectiveDirection,
    Severity,
    _freeze_mapping,
    _require_enum,
    _unique,
    finite_metric,
    nonblank,
    nonnegative_integer,
    utc,
    validate_id,
    validate_schema,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class MetricDefinitionRecord:
    metric_name: str
    definition_version: str
    formula: str
    unit: str
    precision: int | None
    aggregation_method: str
    missing_value_policy: str
    producer: str
    owner_module: str
    direction: ObjectiveDirection | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        validate_id("metric_name", self.metric_name)
        validate_schema("definition_version", self.definition_version)
        validate_schema("schema_version", self.schema_version)
        if self.schema_version != "1":
            raise ValidationError("unsupported metric definition schema_version")
        for name in (
            "formula",
            "unit",
            "aggregation_method",
            "missing_value_policy",
            "producer",
            "owner_module",
        ):
            nonblank(name, getattr(self, name))
        if self.precision is not None:
            nonnegative_integer("precision", self.precision)
        if self.direction is not None and not isinstance(self.direction, ObjectiveDirection):
            raise ValidationError("direction must be an ObjectiveDirection or null")

    @property
    def definition_id(self) -> str:
        return deterministic_id(
            "metricdef",
            {
                "metric_name": self.metric_name,
                "definition_version": self.definition_version,
            },
            digest_chars=48,
        )

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    run_id: str
    metric_name: str
    value: int | float | str | bool | None
    unit: str
    precision: int | None
    definition_version: str
    producer: str
    split: str
    period: str
    scenario: str
    warnings: tuple[str, ...] = ()
    confidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_id("run_id", self.run_id)
        validate_id("metric_name", self.metric_name)
        for name in ("unit", "definition_version", "producer", "split", "period", "scenario"):
            nonblank(name, getattr(self, name))
        if self.precision is not None:
            nonnegative_integer("precision", self.precision)
        finite_metric(self.value)
        _unique("warnings", self.warnings)
        object.__setattr__(self, "confidence", _freeze_mapping(self.confidence))

    @property
    def definition_id(self) -> str:
        return deterministic_id(
            "metricdef",
            {
                "metric_name": self.metric_name,
                "definition_version": self.definition_version,
            },
            digest_chars=48,
        )

    @property
    def metric_key(self) -> str:
        return deterministic_id(
            "metric",
            {
                "run_id": self.run_id,
                "metric_name": self.metric_name,
                "definition_version": self.definition_version,
                "split": self.split,
                "period": self.period,
                "scenario": self.scenario,
            },
            digest_chars=48,
        )

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class AuditEvidenceRecord:
    evidence_id: str
    experiment_id: str
    run_ids: tuple[str, ...]
    evidence_type: str
    producer: str
    producer_version: str
    status: EvidenceStatus
    severity: Severity
    artifact_ids: tuple[str, ...]
    created_at: datetime
    scope: Mapping[str, Any]
    evidence_hash: str
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_enum("status", self.status, EvidenceStatus)
        _require_enum("severity", self.severity, Severity)
        validate_id("evidence_id", self.evidence_id)
        validate_id("experiment_id", self.experiment_id)
        for run_id in self.run_ids:
            validate_id("run_id", run_id)
        for artifact_id in self.artifact_ids:
            validate_id("artifact_id", artifact_id)
        _unique("run_ids", self.run_ids)
        _unique("artifact_ids", self.artifact_ids)
        for name in ("evidence_type", "producer", "producer_version"):
            nonblank(name, getattr(self, name))
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))
        object.__setattr__(self, "scope", _freeze_mapping(self.scope))
        validate_sha256("evidence_hash", self.evidence_hash)
        _unique("limitations", self.limitations)

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class RoleDecision:
    experiment_id: str
    configuration_id: str
    role: CandidateRole
    reason: str
    actor: str = "results-registry"

    def __post_init__(self) -> None:
        _require_enum("role", self.role, CandidateRole)
        validate_id("experiment_id", self.experiment_id)
        validate_id("configuration_id", self.configuration_id)
        nonblank("reason", self.reason)
        nonblank("actor", self.actor)

    @property
    def decision_id(self) -> str:
        return deterministic_id("role", self, digest_chars=48)

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)
