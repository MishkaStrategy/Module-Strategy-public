from __future__ import annotations

from dataclasses import replace

import pytest

from modular_results_registry.evidence import (
    EvidenceRequirement,
    StrategyAuditPackageInput,
    build_strategy_audit_package,
    evaluate_evidence_completeness,
)
from modular_results_registry.errors import IntegrityError
from modular_results_registry.models import (
    CompletenessStatus,
    EvidenceStatus,
    Finding,
    FindingCode,
    Severity,
)
from conftest import package as package_fixture


def test_evidence_completeness_pass_and_missing() -> None:
    package = package_fixture.__wrapped__()  # type: ignore[attr-defined]
    requirements = (
        EvidenceRequirement("bootstrap"),
        EvidenceRequirement("monte_carlo"),
    )
    result = evaluate_evidence_completeness(package.evidence, requirements)
    assert result.status is CompletenessStatus.BLOCKED
    assert result.satisfied == ("bootstrap",)
    assert result.missing == ("monte_carlo",)
    assert result.blocking_reasons == ("MISSING:monte_carlo",)


def test_contaminated_evidence_dominates_status() -> None:
    package = package_fixture.__wrapped__()  # type: ignore[attr-defined]
    contaminated = replace(package.evidence[0], status=EvidenceStatus.CONTAMINATED)
    result = evaluate_evidence_completeness((contaminated,), (EvidenceRequirement("bootstrap"),))
    assert result.status is CompletenessStatus.CONTAMINATED
    assert result.contaminated == ("bootstrap",)


def test_strategy_audit_package_is_deterministic_and_hash_bound() -> None:
    package = package_fixture.__wrapped__()  # type: ignore[attr-defined]
    completeness = evaluate_evidence_completeness(
        package.evidence, (EvidenceRequirement("bootstrap"),)
    )
    finding = Finding.create(
        code=FindingCode.INSUFFICIENT_ROBUSTNESS,
        severity=Severity.HIGH,
        blocking=True,
        affected_scope={"candidate": "configuration-001"},
        thresholds={"required": True},
        actual_values={"complete": False},
        recommendation="complete robustness",
    )
    run = package.runs[0]
    definition = package.metric_definitions[0]
    data = StrategyAuditPackageInput(
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
        metrics=({"profit_factor": 1.5},),
        artifact_references=tuple(item.sha256 for item in package.artifacts),
        findings=(finding,),
        evidence=package.evidence,
        completeness=completeness,
        reproducibility_status="REPRODUCIBLE_SOURCE_CAPTURED",
        registry_integrity_status="PASS",
        known_limitations=("synthetic fixture",),
    )
    left = build_strategy_audit_package(data)
    right = build_strategy_audit_package(data)
    assert left.package_id == right.package_id
    assert left.package_hash == right.package_hash
    assert left.to_json_bytes() == right.to_json_bytes()
    with pytest.raises(IntegrityError):
        replace(left, package_hash="0" * 64)


def test_strategy_audit_package_preserves_dataset_id_hash_pairing() -> None:
    package = package_fixture.__wrapped__()  # type: ignore[attr-defined]
    completeness = evaluate_evidence_completeness(
        package.evidence, (EvidenceRequirement("bootstrap"),)
    )
    definition = package.metric_definitions[0]
    data = StrategyAuditPackageInput(
        registry_revision=1,
        registry_state_hash="a" * 64,
        strategy_id="strategy-001",
        baseline_ids=(),
        candidate_ids=(),
        experiment_ids=(),
        run_ids=(),
        configuration_ids=(),
        dataset_ids=("dataset-z", "dataset-a"),
        dataset_hashes=("1" * 64, "f" * 64),
        commit_shas=(),
        module_versions={},
        backtest_engine_versions=(),
        metric_definition_ids=(definition.definition_id,),
        metric_definition_hashes=(definition.content_hash,),
        metric_definition_versions=(definition.definition_version,),
        split_information={},
        metrics=(),
        artifact_references=(),
        evidence=package.evidence,
        completeness=completeness,
    )
    result = build_strategy_audit_package(data)
    assert tuple(zip(result.dataset_ids, result.dataset_hashes)) == (
        ("dataset-a", "f" * 64),
        ("dataset-z", "1" * 64),
    )


def test_strategy_audit_package_rejects_misaligned_dataset_lineage() -> None:
    package = package_fixture.__wrapped__()  # type: ignore[attr-defined]
    completeness = evaluate_evidence_completeness(
        package.evidence, (EvidenceRequirement("bootstrap"),)
    )
    definition = package.metric_definitions[0]
    data = StrategyAuditPackageInput(
        registry_revision=1,
        registry_state_hash="a" * 64,
        strategy_id="strategy-001",
        baseline_ids=(),
        candidate_ids=(),
        experiment_ids=(),
        run_ids=(),
        configuration_ids=(),
        dataset_ids=("dataset-a",),
        dataset_hashes=(),
        commit_shas=(),
        module_versions={},
        backtest_engine_versions=(),
        metric_definition_ids=(definition.definition_id,),
        metric_definition_hashes=(definition.content_hash,),
        metric_definition_versions=(definition.definition_version,),
        split_information={},
        metrics=(),
        artifact_references=(),
        evidence=package.evidence,
        completeness=completeness,
    )
    with pytest.raises(ValueError, match="aligned"):
        build_strategy_audit_package(data)
