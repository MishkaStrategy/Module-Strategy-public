from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import validate

from conftest import make_package
from modular_results_registry.errors import IntegrityError, ValidationError
from modular_results_registry.evidence import (
    EvidenceRequirement,
    StrategyAuditPackageInput,
    build_strategy_audit_package,
    evaluate_evidence_completeness,
)
from modular_results_registry.evidence_package import _sorted_mappings
from modular_results_registry.models import Finding, FindingCode, Severity


def _input() -> StrategyAuditPackageInput:
    package = make_package()
    completeness = evaluate_evidence_completeness(
        package.evidence, (EvidenceRequirement("bootstrap"),)
    )
    definition = package.metric_definitions[0]
    run = package.runs[0]
    return StrategyAuditPackageInput(
        registry_revision=1,
        registry_state_hash="a" * 64,
        strategy_id="strategy-001",
        baseline_ids=("baseline-001",),
        candidate_ids=("candidate-001",),
        experiment_ids=(package.experiments[0].experiment_id,),
        run_ids=(run.run_id,),
        configuration_ids=(package.configurations[0].configuration_id,),
        dataset_ids=(package.datasets[0].dataset_id,),
        dataset_hashes=(package.datasets[0].sha256,),
        commit_shas=(package.experiments[0].commit_sha,),
        module_versions=package.experiments[0].module_versions,
        backtest_engine_versions=(run.backtest_engine_version,),
        metric_definition_ids=(definition.definition_id,),
        metric_definition_hashes=(definition.content_hash,),
        metric_definition_versions=(definition.definition_version,),
        split_information={"split": "test"},
        metrics=({"metric": "a"},),
        artifact_references=(package.artifacts[0].sha256,),
        comparison_tables=({"comparison": "a"},),
        slices={"side": ({"side": "long"},)},
        pareto_result={"front": ()},
        evidence=package.evidence,
        completeness=completeness,
        reproducibility_status="PASS",
        registry_integrity_status="PASS",
    )


def _built():
    return build_strategy_audit_package(_input())


def test_package_revision_and_required_text_validation() -> None:
    package = _built()
    for value in (True, 1.5, "1"):
        with pytest.raises(ValidationError, match="registry_revision must be an integer"):
            replace(package, registry_revision=value)
    with pytest.raises(ValidationError, match="cannot be negative"):
        replace(package, registry_revision=-1)

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
        with pytest.raises(ValidationError, match=f"{name} must be non-blank"):
            replace(package, **{name: " "})


def test_package_hash_lineage_alignment_and_digest_validation() -> None:
    package = _built()
    for value in ("a", "A" * 64, "g" * 64):
        with pytest.raises(ValidationError, match="registry_state_hash"):
            replace(package, registry_state_hash=value)
    with pytest.raises(ValidationError, match="dataset IDs and hashes"):
        replace(package, dataset_hashes=())
    with pytest.raises(ValidationError, match="dataset hashes"):
        replace(package, dataset_hashes=("bad",))
    with pytest.raises(ValidationError, match="metric definition IDs, hashes and versions"):
        replace(package, metric_definition_versions=())
    with pytest.raises(ValidationError, match="metric definition hashes"):
        replace(package, metric_definition_hashes=("bad",))
    for value in ("bad", "A" * 64, "g" * 64):
        with pytest.raises(ValidationError, match="package_hash"):
            replace(package, package_hash=value)


def test_package_requires_sorted_unique_lineage() -> None:
    package = _built()
    for name in (
        "baseline_ids",
        "candidate_ids",
        "experiment_ids",
        "run_ids",
        "configuration_ids",
        "backtest_engine_versions",
        "artifact_references",
        "contamination_flags",
        "known_limitations",
    ):
        with pytest.raises(ValidationError, match="sorted and unique"):
            replace(package, **{name: ("z", "a")})
    with pytest.raises(ValidationError, match="commit_shas must be sorted and unique"):
        replace(package, commit_shas=("f" * 40, "a" * 40))
    with pytest.raises(ValidationError, match="commit SHAs"):
        replace(package, commit_shas=("z" * 40,))
    with pytest.raises(ValidationError, match="dataset ID/hash pairs"):
        replace(
            package,
            dataset_ids=("dataset-z", "dataset-a"),
            dataset_hashes=("1" * 64, "f" * 64),
        )
    with pytest.raises(ValidationError, match="metric definition ID/hash/version triples"):
        replace(
            package,
            metric_definition_ids=("z", "a"),
            metric_definition_hashes=("1" * 64, "f" * 64),
            metric_definition_versions=("1", "1"),
        )


def test_package_requires_canonical_analytics_and_ordered_records() -> None:
    package = _built()
    mappings = ({"value": "z"}, {"value": "a"})
    ordered = _sorted_mappings(mappings)
    reversed_values = tuple(reversed(ordered))
    assert reversed_values != ordered
    with pytest.raises(ValidationError, match="metrics must be canonically ordered"):
        replace(package, metrics=reversed_values)
    with pytest.raises(ValidationError, match="comparison tables must be canonically ordered"):
        replace(package, comparison_tables=reversed_values)

    finding_a = Finding.create(
        code=FindingCode.INSUFFICIENT_ROBUSTNESS,
        severity=Severity.HIGH,
        blocking=True,
        affected_scope={"id": "a"},
        thresholds={},
        actual_values={},
        recommendation="a",
    )
    finding_b = Finding.create(
        code=FindingCode.INSUFFICIENT_ROBUSTNESS,
        severity=Severity.HIGH,
        blocking=True,
        affected_scope={"id": "b"},
        thresholds={},
        actual_values={},
        recommendation="b",
    )
    ordered_findings = tuple(sorted((finding_a, finding_b), key=lambda item: item.finding_id))
    with pytest.raises(ValidationError, match="findings must be ordered"):
        replace(package, findings=tuple(reversed(ordered_findings)))

    evidence_a = package.evidence[0]
    evidence_b = replace(evidence_a, evidence_id="evidence-zzz")
    ordered_evidence = tuple(sorted((evidence_a, evidence_b), key=lambda item: item.evidence_id))
    with pytest.raises(ValidationError, match="evidence must be ordered"):
        replace(package, evidence=tuple(reversed(ordered_evidence)))


def test_package_id_and_hash_are_bound_to_content() -> None:
    package = _built()
    with pytest.raises(IntegrityError, match="ID mismatch"):
        replace(package, package_id="strategy-audit-" + "0" * 48)
    with pytest.raises(IntegrityError, match="hash mismatch"):
        replace(package, package_hash="0" * 64)
    mapping = package.to_mapping(include_hash=False)
    assert "package_hash" not in mapping
    assert package.content_hash == package.package_hash


def test_builder_requires_completeness_and_aligned_metric_definitions() -> None:
    data = _input()
    with pytest.raises(ValueError, match="completeness result is required"):
        build_strategy_audit_package(replace(data, completeness=None))
    with pytest.raises(ValidationError, match="metric definition IDs, hashes and versions"):
        build_strategy_audit_package(replace(data, metric_definition_hashes=()))


def test_repeated_metric_hash_and_version_serialize_against_schema() -> None:
    data = _input()
    digest = data.metric_definition_hashes[0]
    package = build_strategy_audit_package(
        replace(
            data,
            metric_definition_ids=("metric-a", "metric-b"),
            metric_definition_hashes=(digest, digest),
            metric_definition_versions=("core.metrics.v0.4.0", "core.metrics.v0.4.0"),
        )
    )

    validate(
        instance=json.loads(package.to_json_bytes()),
        schema=json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "strategy-audit-evidence-package.schema.json"
            ).read_text(encoding="utf-8")
        ),
    )


def test_builder_rejects_duplicate_dataset_and_metric_definition_ids() -> None:
    data = _input()
    with pytest.raises(ValidationError, match="dataset IDs must be unique"):
        build_strategy_audit_package(
            replace(
                data,
                dataset_ids=("dataset-duplicate", "dataset-duplicate"),
                dataset_hashes=("a" * 64, "b" * 64),
            )
        )
    with pytest.raises(ValidationError, match="metric definition IDs must be unique"):
        build_strategy_audit_package(
            replace(
                data,
                metric_definition_ids=("metric-duplicate", "metric-duplicate"),
                metric_definition_hashes=("a" * 64, "b" * 64),
                metric_definition_versions=("v1", "v2"),
            )
        )


def test_model_rejects_duplicate_dataset_and_metric_definition_ids() -> None:
    package = _built()
    dataset_id = package.dataset_ids[0]
    dataset_hash = package.dataset_hashes[0]
    with pytest.raises(ValidationError, match="dataset IDs must be unique"):
        replace(
            package,
            dataset_ids=(dataset_id, dataset_id),
            dataset_hashes=(dataset_hash, "a" * 64),
        )

    metric_id = package.metric_definition_ids[0]
    metric_hash = package.metric_definition_hashes[0]
    metric_version = package.metric_definition_versions[0]
    with pytest.raises(ValidationError, match="metric definition IDs must be unique"):
        replace(
            package,
            metric_definition_ids=(metric_id, metric_id),
            metric_definition_hashes=(metric_hash, "b" * 64),
            metric_definition_versions=(metric_version, "v2"),
        )
