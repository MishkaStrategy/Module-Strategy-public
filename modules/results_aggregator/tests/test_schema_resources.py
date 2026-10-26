from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from modular_results_registry.canonical import canonical_json_bytes
from modular_results_registry.errors import ValidationError
from modular_results_registry.schema_resources import (
    list_schema_names,
    read_schema,
    read_schema_bytes,
    schema_hash,
)


_MODULE_ROOT = Path(__file__).resolve().parents[1]
_RELEASE_SCHEMAS = _MODULE_ROOT / "schemas"


def test_bundled_schemas_match_release_bundle_sources() -> None:
    names = list_schema_names()
    assert "catalog.json" in names
    assert "artifact-package.schema.json" in names
    for name in names:
        source = _RELEASE_SCHEMAS / name
        assert source.read_bytes() == read_schema_bytes(name)
        value = read_schema(name)
        assert isinstance(value, Mapping)
        assert canonical_json_bytes(
            json.loads(source.read_text(encoding="utf-8"))
        ) == canonical_json_bytes(value)
        assert len(schema_hash(name)) == 64


def test_schema_resource_names_are_fail_closed() -> None:
    for name in ("", "../catalog.json", "schemas/catalog.json", "missing.json", "catalog.txt"):
        with pytest.raises(ValidationError):
            read_schema_bytes(name)


def test_strategy_audit_schema_matches_actual_package_contract(package) -> None:
    from modular_results_registry.evidence import (
        EvidenceRequirement,
        StrategyAuditPackageInput,
        build_strategy_audit_package,
        evaluate_evidence_completeness,
    )

    completeness = evaluate_evidence_completeness(
        package.evidence, (EvidenceRequirement("artifact_integrity"),)
    )
    built = build_strategy_audit_package(
        StrategyAuditPackageInput(
            registry_revision=1,
            registry_state_hash="a" * 64,
            strategy_id="strategy-001",
            baseline_ids=(),
            candidate_ids=(),
            experiment_ids=(package.experiments[0].experiment_id,),
            run_ids=(package.runs[0].run_id,),
            configuration_ids=(package.configurations[0].configuration_id,),
            dataset_ids=(package.datasets[0].dataset_id,),
            dataset_hashes=(package.datasets[0].sha256,),
            commit_shas=(package.experiments[0].commit_sha,),
            module_versions=package.experiments[0].module_versions,
            backtest_engine_versions=(package.runs[0].backtest_engine_version,),
            metric_definition_ids=(package.metric_definitions[0].definition_id,),
            metric_definition_hashes=(package.metric_definitions[0].content_hash,),
            metric_definition_versions=(package.metrics[0].definition_version,),
            split_information={},
            metrics=(),
            artifact_references=(),
            completeness=completeness,
        )
    )
    schema = read_schema("strategy-audit-evidence-package.schema.json")
    assert set(built.to_mapping()) == set(schema["properties"])
    assert set(schema["required"]) == set(schema["properties"])
    assert "package_hash" in built.to_mapping()
    assert "content_hash" not in built.to_mapping()


def test_schema_resources_are_deeply_immutable() -> None:
    schema = read_schema("artifact-package.schema.json")
    with pytest.raises(TypeError):
        schema["properties"]["package_id"] = {"type": "number"}


def test_rebuild_bundle_schema_matches_exported_contract(registry_path, package) -> None:
    from modular_results_registry.storage import SQLiteRegistry

    with SQLiteRegistry(registry_path) as registry:
        registry.register_package(package)
        bundle = registry.export_rebuild_bundle()
    schema = read_schema("rebuild-bundle.schema.json")
    assert set(bundle) == set(schema["properties"])
    assert set(schema["required"]) == set(schema["properties"])
    assert bundle["schema_version"] == "4"


def test_schema_resource_rejects_nonfinite_json(monkeypatch) -> None:
    import modular_results_registry.schema_resources as resources

    monkeypatch.setattr(resources, "read_schema_bytes", lambda name: b'{"value":NaN}')
    with pytest.raises(ValidationError, match="not valid JSON"):
        resources.read_schema("fake.json")
