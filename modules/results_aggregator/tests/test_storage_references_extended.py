from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_package
from modular_results_registry.errors import IntegrityError
from modular_results_registry.storage import SQLiteRegistry


def _register_fails(tmp_path: Path, package, message: str) -> None:
    with SQLiteRegistry(tmp_path / (message.replace(" ", "-") + ".sqlite")) as registry:
        with pytest.raises(IntegrityError, match=message):
            registry.register_package(package)
        assert registry.revision() == 0


def test_reference_validation_rejects_missing_experiment_dependencies(tmp_path: Path) -> None:
    package = make_package()
    experiment = package.experiments[0]
    _register_fails(
        tmp_path,
        replace(
            package, experiments=(replace(experiment, parent_experiment_id="experiment-missing"),)
        ),
        "missing parent",
    )
    _register_fails(
        tmp_path,
        replace(package, experiments=(replace(experiment, patch_artifact_id="artifact-missing"),)),
        "missing patch artifact",
    )


def test_reference_validation_rejects_missing_run_dependencies(tmp_path: Path) -> None:
    package = make_package()
    run = package.runs[0]
    cases = [
        (replace(run, experiment_id="experiment-missing"), "missing experiment"),
        (replace(run, configuration_id="configuration-missing"), "missing configuration"),
        (replace(run, dataset_ids=("dataset-missing",)), "missing datasets"),
        (replace(run, artifact_ids=("artifact-missing",)), "missing artifacts"),
    ]
    for changed, message in cases:
        _register_fails(tmp_path, replace(package, runs=(changed,)), message)


def test_reference_validation_rejects_metric_lineage_and_contract_mismatch(tmp_path: Path) -> None:
    package = make_package()
    metric = package.metrics[0]
    cases = [
        (replace(metric, run_id="run-missing"), package.metric_definitions, "missing run"),
        (
            replace(metric, definition_version="v2"),
            package.metric_definitions,
            "missing metric definition",
        ),
        (replace(metric, unit="percent"), package.metric_definitions, "unit does not match"),
        (replace(metric, precision=2), package.metric_definitions, "precision does not match"),
        (replace(metric, producer="other"), package.metric_definitions, "producer does not match"),
    ]
    for changed, definitions, message in cases:
        _register_fails(
            tmp_path,
            replace(package, metrics=(changed,), metric_definitions=definitions),
            message,
        )


def test_metric_can_resolve_definition_from_existing_registry(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    package = make_package()
    with SQLiteRegistry(path) as registry:
        registry.register_package(package)
        duplicate_metric_package = replace(
            package,
            package_id="package-metric-only",
            artifacts=(),
            datasets=(),
            configurations=(),
            experiments=(),
            runs=(),
            metric_definitions=(),
            evidence=(),
            roles=(),
        )
        outcome = registry.register_package(duplicate_metric_package)
        assert outcome.duplicate_entities == 1
        assert outcome.registered_entities == 0


def test_existing_definition_contract_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    package = make_package()
    with SQLiteRegistry(path) as registry:
        registry.register_package(package)
        metric_only = replace(
            package,
            package_id="package-metric-mismatch",
            artifacts=(),
            datasets=(),
            configurations=(),
            experiments=(),
            runs=(),
            metrics=(replace(package.metrics[0], unit="percent"),),
            metric_definitions=(),
            evidence=(),
            roles=(),
        )
        with pytest.raises(IntegrityError, match="unit does not match"):
            registry.register_package(metric_only)


def test_reference_validation_rejects_evidence_and_role_dependencies(tmp_path: Path) -> None:
    package = make_package()
    evidence = package.evidence[0]
    role = package.roles[0]
    cases = [
        (
            replace(package, evidence=(replace(evidence, experiment_id="experiment-missing"),)),
            "evidence .* missing experiment",
        ),
        (replace(package, evidence=(replace(evidence, run_ids=("run-missing",)),)), "missing runs"),
        (
            replace(package, evidence=(replace(evidence, artifact_ids=("artifact-missing",)),)),
            "missing artifacts",
        ),
        (
            replace(package, roles=(replace(role, experiment_id="experiment-missing"),)),
            "role .* missing experiment",
        ),
        (
            replace(package, roles=(replace(role, configuration_id="configuration-missing"),)),
            "role .* missing configuration",
        ),
    ]
    for changed, message in cases:
        _register_fails(tmp_path, changed, message)


def test_experiment_parent_cycle_is_rejected(tmp_path: Path) -> None:
    package = make_package()
    first = replace(package.experiments[0], parent_experiment_id="experiment-002")
    second = replace(
        package.experiments[0],
        experiment_id="experiment-002",
        parent_experiment_id=first.experiment_id,
        title="Second experiment",
    )
    cyclic = replace(package, experiments=(first, second))
    _register_fails(tmp_path, cyclic, "parent graph contains a cycle")
