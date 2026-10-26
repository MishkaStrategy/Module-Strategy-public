from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import canonical_json_bytes, canonical_sha256, deep_freeze, deterministic_id
from .errors import IntegrityError, ValidationError
from .models import AuditEvidenceRecord, Finding

from .evidence_completeness import EvidenceCompletenessResult


@dataclass(frozen=True, slots=True)
class StrategyAuditEvidencePackage:
    package_id: str
    schema_version: str
    generator_version: str
    registry_revision: int
    registry_state_hash: str
    strategy_id: str
    baseline_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    experiment_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    configuration_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    dataset_hashes: tuple[str, ...]
    commit_shas: tuple[str, ...]
    module_versions: Mapping[str, str]
    backtest_engine_versions: tuple[str, ...]
    metric_definition_ids: tuple[str, ...]
    metric_definition_hashes: tuple[str, ...]
    metric_definition_versions: tuple[str, ...]
    split_information: Mapping[str, Any]
    metrics: tuple[Mapping[str, Any], ...]
    artifact_references: tuple[str, ...]
    comparison_tables: tuple[Mapping[str, Any], ...]
    slices: Mapping[str, tuple[Mapping[str, Any], ...]]
    pareto_result: Mapping[str, Any]
    findings: tuple[Finding, ...]
    evidence: tuple[AuditEvidenceRecord, ...]
    completeness: EvidenceCompletenessResult
    contamination_flags: tuple[str, ...]
    reproducibility_status: str
    registry_integrity_status: str
    known_limitations: tuple[str, ...]
    package_hash: str

    @property
    def content_hash(self) -> str:
        """Backward-compatible read-only alias for the package hash."""
        return self.package_hash

    def __post_init__(self) -> None:
        object.__setattr__(self, "module_versions", deep_freeze(self.module_versions))
        object.__setattr__(self, "split_information", deep_freeze(self.split_information))
        object.__setattr__(self, "metrics", tuple(deep_freeze(item) for item in self.metrics))
        object.__setattr__(
            self, "comparison_tables", tuple(deep_freeze(item) for item in self.comparison_tables)
        )
        object.__setattr__(self, "slices", _canonical_slices(self.slices))
        object.__setattr__(self, "pareto_result", deep_freeze(self.pareto_result))
        if isinstance(self.registry_revision, bool) or not isinstance(self.registry_revision, int):
            raise ValidationError("registry_revision must be an integer")
        if self.registry_revision < 0:
            raise ValidationError("registry_revision cannot be negative")
        for name in (
            "package_id",
            "schema_version",
            "generator_version",
            "registry_state_hash",
            "strategy_id",
            "reproducibility_status",
            "registry_integrity_status",
            "package_hash",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{name} must be non-blank")
        if len(self.registry_state_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.registry_state_hash
        ):
            raise ValidationError("registry_state_hash must be lowercase SHA-256")
        if len(self.dataset_ids) != len(self.dataset_hashes):
            raise ValidationError("dataset IDs and hashes must remain aligned")
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValidationError("dataset IDs must be unique")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in self.dataset_hashes
        ):
            raise ValidationError("dataset hashes must be lowercase SHA-256 values")
        if not (
            len(self.metric_definition_ids)
            == len(self.metric_definition_hashes)
            == len(self.metric_definition_versions)
        ):
            raise ValidationError("metric definition IDs, hashes and versions must remain aligned")
        if len(set(self.metric_definition_ids)) != len(self.metric_definition_ids):
            raise ValidationError("metric definition IDs must be unique")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in self.metric_definition_hashes
        ):
            raise ValidationError("metric definition hashes must be lowercase SHA-256 values")
        definition_triples = tuple(
            zip(
                self.metric_definition_ids,
                self.metric_definition_hashes,
                self.metric_definition_versions,
                strict=True,
            )
        )
        if definition_triples != tuple(sorted(set(definition_triples))):
            raise ValidationError(
                "metric definition ID/hash/version triples must be sorted and unique"
            )
        if len(self.package_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.package_hash
        ):
            raise ValidationError("package_hash must be lowercase SHA-256")
        ordered_fields = (
            "baseline_ids",
            "candidate_ids",
            "experiment_ids",
            "run_ids",
            "configuration_ids",
            "commit_shas",
            "backtest_engine_versions",
            "artifact_references",
            "contamination_flags",
            "known_limitations",
        )
        for name in ordered_fields:
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValidationError(f"{name} must be sorted and unique")
        if tuple(zip(self.dataset_ids, self.dataset_hashes)) != tuple(
            sorted(set(zip(self.dataset_ids, self.dataset_hashes)))
        ):
            raise ValidationError("dataset ID/hash pairs must be sorted and unique")
        if any(
            len(value) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.commit_shas
        ):
            raise ValidationError("commit SHAs must be lowercase hexadecimal")
        if self.metrics != _sorted_mappings(self.metrics):
            raise ValidationError("metrics must be canonically ordered")
        if self.comparison_tables != _sorted_mappings(self.comparison_tables):
            raise ValidationError("comparison tables must be canonically ordered")
        if self.slices != _canonical_slices(self.slices):
            raise ValidationError("slices must be canonically ordered")
        if self.findings != tuple(sorted(self.findings, key=lambda item: item.finding_id)):
            raise ValidationError("findings must be ordered by finding_id")
        if self.evidence != tuple(sorted(self.evidence, key=lambda item: item.evidence_id)):
            raise ValidationError("evidence must be ordered by evidence_id")
        base = self.to_mapping(include_hash=False)
        base.pop("package_id")
        expected_id = deterministic_id("strategy-audit", base, digest_chars=48)
        if self.package_id != expected_id:
            raise IntegrityError("Strategy Audit package ID mismatch")
        expected_hash = canonical_sha256({"package_id": self.package_id, **base})
        if self.package_hash != expected_hash:
            raise IntegrityError("Strategy Audit package hash mismatch")

    def to_mapping(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "package_id": self.package_id,
            "schema_version": self.schema_version,
            "generator_version": self.generator_version,
            "registry_revision": self.registry_revision,
            "registry_state_hash": self.registry_state_hash,
            "strategy_id": self.strategy_id,
            "baseline_ids": self.baseline_ids,
            "candidate_ids": self.candidate_ids,
            "experiment_ids": self.experiment_ids,
            "run_ids": self.run_ids,
            "configuration_ids": self.configuration_ids,
            "dataset_ids": self.dataset_ids,
            "dataset_hashes": self.dataset_hashes,
            "commit_shas": self.commit_shas,
            "module_versions": self.module_versions,
            "backtest_engine_versions": self.backtest_engine_versions,
            "metric_definition_ids": self.metric_definition_ids,
            "metric_definition_hashes": self.metric_definition_hashes,
            "metric_definition_versions": self.metric_definition_versions,
            "split_information": self.split_information,
            "metrics": self.metrics,
            "artifact_references": self.artifact_references,
            "comparison_tables": self.comparison_tables,
            "slices": self.slices,
            "pareto_result": self.pareto_result,
            "findings": self.findings,
            "evidence": self.evidence,
            "completeness": self.completeness,
            "contamination_flags": self.contamination_flags,
            "reproducibility_status": self.reproducibility_status,
            "registry_integrity_status": self.registry_integrity_status,
            "known_limitations": self.known_limitations,
        }
        if include_hash:
            result["package_hash"] = self.package_hash
        return result

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


@dataclass(frozen=True, slots=True)
class StrategyAuditPackageInput:
    registry_revision: int
    registry_state_hash: str
    strategy_id: str
    baseline_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    experiment_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    configuration_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    dataset_hashes: tuple[str, ...]
    commit_shas: tuple[str, ...]
    module_versions: Mapping[str, str]
    backtest_engine_versions: tuple[str, ...]
    metric_definition_ids: tuple[str, ...]
    metric_definition_hashes: tuple[str, ...]
    metric_definition_versions: tuple[str, ...]
    split_information: Mapping[str, Any]
    metrics: tuple[Mapping[str, Any], ...]
    artifact_references: tuple[str, ...]
    comparison_tables: tuple[Mapping[str, Any], ...] = ()
    slices: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    pareto_result: Mapping[str, Any] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    evidence: tuple[AuditEvidenceRecord, ...] = ()
    completeness: EvidenceCompletenessResult | None = None
    contamination_flags: tuple[str, ...] = ()
    reproducibility_status: str = "UNKNOWN"
    registry_integrity_status: str = "UNKNOWN"
    known_limitations: tuple[str, ...] = ()


def _sorted_mappings(values: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
    frozen = tuple(deep_freeze(item) for item in values)
    return tuple(sorted(frozen, key=canonical_sha256))


def _canonical_slices(
    values: Mapping[str, tuple[Mapping[str, Any], ...]],
) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    return deep_freeze(
        {key: _sorted_mappings(tuple(items)) for key, items in sorted(values.items())}
    )


def build_strategy_audit_package(
    data: StrategyAuditPackageInput,
    *,
    schema_version: str = "2",
    generator_version: str = "0.1.0.dev0",
) -> StrategyAuditEvidencePackage:
    if data.completeness is None:
        raise ValueError("completeness result is required")
    if len(data.dataset_ids) != len(data.dataset_hashes):
        raise ValidationError("dataset IDs and hashes must be aligned")
    if len(set(data.dataset_ids)) != len(data.dataset_ids):
        raise ValidationError("dataset IDs must be unique")
    if not (
        len(data.metric_definition_ids)
        == len(data.metric_definition_hashes)
        == len(data.metric_definition_versions)
    ):
        raise ValidationError("metric definition IDs, hashes and versions must be aligned")
    if len(set(data.metric_definition_ids)) != len(data.metric_definition_ids):
        raise ValidationError("metric definition IDs must be unique")
    dataset_pairs = tuple(sorted(zip(data.dataset_ids, data.dataset_hashes)))
    dataset_ids = tuple(item[0] for item in dataset_pairs)
    dataset_hashes = tuple(item[1] for item in dataset_pairs)
    definition_triples = tuple(
        sorted(
            zip(
                data.metric_definition_ids,
                data.metric_definition_hashes,
                data.metric_definition_versions,
                strict=True,
            )
        )
    )
    metric_definition_ids = tuple(item[0] for item in definition_triples)
    metric_definition_hashes = tuple(item[1] for item in definition_triples)
    metric_definition_versions = tuple(item[2] for item in definition_triples)
    base = {
        "schema_version": schema_version,
        "generator_version": generator_version,
        "registry_revision": data.registry_revision,
        "registry_state_hash": data.registry_state_hash,
        "strategy_id": data.strategy_id,
        "baseline_ids": tuple(sorted(data.baseline_ids)),
        "candidate_ids": tuple(sorted(data.candidate_ids)),
        "experiment_ids": tuple(sorted(data.experiment_ids)),
        "run_ids": tuple(sorted(data.run_ids)),
        "configuration_ids": tuple(sorted(data.configuration_ids)),
        "dataset_ids": dataset_ids,
        "dataset_hashes": dataset_hashes,
        "commit_shas": tuple(sorted(data.commit_shas)),
        "module_versions": deep_freeze(data.module_versions),
        "backtest_engine_versions": tuple(sorted(data.backtest_engine_versions)),
        "metric_definition_ids": metric_definition_ids,
        "metric_definition_hashes": metric_definition_hashes,
        "metric_definition_versions": metric_definition_versions,
        "split_information": deep_freeze(data.split_information),
        "metrics": _sorted_mappings(data.metrics),
        "artifact_references": tuple(sorted(data.artifact_references)),
        "comparison_tables": _sorted_mappings(data.comparison_tables),
        "slices": _canonical_slices(data.slices),
        "pareto_result": deep_freeze(data.pareto_result),
        "findings": tuple(sorted(data.findings, key=lambda item: item.finding_id)),
        "evidence": tuple(sorted(data.evidence, key=lambda item: item.evidence_id)),
        "completeness": data.completeness,
        "contamination_flags": tuple(sorted(data.contamination_flags)),
        "reproducibility_status": data.reproducibility_status,
        "registry_integrity_status": data.registry_integrity_status,
        "known_limitations": tuple(sorted(data.known_limitations)),
    }
    package_id = deterministic_id("strategy-audit", base, digest_chars=48)
    package_hash = canonical_sha256({"package_id": package_id, **base})
    return StrategyAuditEvidencePackage(
        package_id=package_id,
        package_hash=package_hash,
        **base,
    )
