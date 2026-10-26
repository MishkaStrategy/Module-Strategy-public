from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import NOW
from modular_results_registry.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    deterministic_id,
)
from modular_results_registry.errors import (
    IntegrityError,
    ResourceLimitError,
    UnsupportedSchemaError,
    ValidationError,
)
from modular_results_registry.intake import (
    ArtifactInventoryItem,
    ArtifactPackageManifest,
    IntakeLimits,
    ProducerProfile,
    inspect_legacy_directory,
    manifest_from_mapping,
    validate_artifact_package,
)


def _inventory(path: str, data: bytes) -> ArtifactInventoryItem:
    return ArtifactInventoryItem(
        relative_path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        media_type="application/json",
        schema_id="producer.result",
        schema_version="1",
    )


def _manifest(items: tuple[ArtifactInventoryItem, ...]) -> ArtifactPackageManifest:
    value = ArtifactPackageManifest(
        schema_id="artifact-package",
        schema_version="1",
        package_id="package-intake",
        package_version="1",
        producer_module_id="producer-test",
        producer_module_version="1.0.0",
        producer_schema_version="1",
        experiment_id="experiment-intake",
        research_id="research-intake",
        stage_id="stage-intake",
        run_id="run-intake",
        parent_experiment_id=None,
        strategy_id="strategy-intake",
        strategy_version="1",
        module_api_version="1",
        backtest_engine_version="0.4.0",
        metric_schema_version="metrics-v1",
        configuration_hash="a" * 64,
        dataset_ids=("dataset-intake",),
        dataset_hashes=("b" * 64,),
        commit_sha="c" * 40,
        branch="research/intake",
        working_tree_status="CLEAN",
        diff_hash=None,
        patch_artifact_path=None,
        dependency_lock_hash="d" * 64,
        environment_manifest={"python": "3.12"},
        split_information={"split": "test"},
        seed=1,
        scenario_id="scenario-intake",
        created_at=NOW,
        artifact_inventory=items,
        expected_references={"runs": ("run-intake",)},
        audit_evidence=("evidence-intake",),
        completion_status="COMPLETED",
        integrity_checksum="0" * 64,
    )
    return replace(value, integrity_checksum=canonical_sha256(value.checksum_payload()))


def _mapping(manifest: ArtifactPackageManifest) -> dict[str, object]:
    raw = json.loads(canonical_json_bytes(manifest))
    assert isinstance(raw, dict)
    return raw


def _profile(**changes: object) -> ProducerProfile:
    values: dict[str, object] = {
        "producer_module_id": "producer-test",
        "allowed_producer_schema_versions": frozenset({"1"}),
        "allowed_artifact_schema_prefixes": ("producer.",),
        "required_artifact_paths": frozenset({"data.json"}),
        "allowed_package_schema_versions": frozenset({"1"}),
    }
    values.update(changes)
    return ProducerProfile(**values)  # type: ignore[arg-type]


def _package_dir(
    tmp_path: Path, *, duplicate: bool = False
) -> tuple[Path, ArtifactPackageManifest]:
    root = tmp_path / "package"
    root.mkdir()
    data = b"{}"
    (root / "data.json").write_bytes(data)
    items = [_inventory("data.json", data)]
    if duplicate:
        (root / "copy.json").write_bytes(data)
        items.append(_inventory("copy.json", data))
    manifest = _manifest(tuple(items))
    (root / "artifact-package.json").write_bytes(canonical_json_bytes(manifest))
    return root, manifest


def test_validate_artifact_package_and_duplicate_content(tmp_path: Path) -> None:
    root, manifest = _package_dir(tmp_path, duplicate=True)
    profile = _profile(required_artifact_paths=frozenset({"data.json", "copy.json"}))
    result = validate_artifact_package(root, profile)
    assert result.manifest == manifest
    assert len(result.verified_files) == 2
    assert tuple(result.duplicate_content.values()) == (("copy.json", "data.json"),)
    assert len(result.validation_hash) == 64


def test_validate_artifact_package_rejects_extra_missing_and_tamper(tmp_path: Path) -> None:
    root, _ = _package_dir(tmp_path)
    (root / "extra.txt").write_text("x", encoding="utf-8")
    with pytest.raises(IntegrityError, match="inventory mismatch"):
        validate_artifact_package(root, _profile())
    result = validate_artifact_package(root, _profile(), reject_extra_files=False)
    assert len(result.verified_files) == 1

    (root / "extra.txt").unlink()
    (root / "data.json").unlink()
    with pytest.raises(IntegrityError, match="inventory mismatch"):
        validate_artifact_package(root, _profile())


def test_validate_artifact_package_rejects_checksum_profile_and_limits(tmp_path: Path) -> None:
    root, manifest = _package_dir(tmp_path)
    bad = replace(manifest, integrity_checksum="f" * 64)
    (root / "artifact-package.json").write_bytes(canonical_json_bytes(bad))
    with pytest.raises(IntegrityError, match="checksum"):
        validate_artifact_package(root, _profile())

    (root / "artifact-package.json").write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ValidationError, match="producer"):
        validate_artifact_package(root, _profile(producer_module_id="other"))
    duplicate_root = tmp_path / "duplicate-limit"
    duplicate_root.mkdir()
    data = b"{}"
    (duplicate_root / "data.json").write_bytes(data)
    (duplicate_root / "copy.json").write_bytes(data)
    duplicate_manifest = _manifest((_inventory("data.json", data), _inventory("copy.json", data)))
    (duplicate_root / "artifact-package.json").write_bytes(canonical_json_bytes(duplicate_manifest))
    with pytest.raises(ResourceLimitError, match="artifact count"):
        validate_artifact_package(
            duplicate_root,
            _profile(required_artifact_paths=frozenset({"data.json", "copy.json"})),
            limits=IntakeLimits(max_artifact_count=1),
        )
    with pytest.raises(ResourceLimitError, match="total byte"):
        validate_artifact_package(root, _profile(), limits=IntakeLimits(max_total_bytes=1))


def test_legacy_directory_is_hashed_but_never_verified(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "a.csv").write_text("a", encoding="utf-8")
    (root / "b.csv").write_text("b", encoding="utf-8")
    report = inspect_legacy_directory(root)
    assert report.profile == "LEGACY_UNVERIFIED_IMPORT"
    assert len(report.files) == 2
    assert "commit SHA" in report.missing_lineage
    assert report.report_id == deterministic_id(
        "legacy",
        {
            "files": tuple({"path": i.relative_path, "sha256": i.sha256} for i in report.files),
            "missing": report.missing_lineage,
        },
        digest_chars=48,
    )
    with pytest.raises(ResourceLimitError, match="count"):
        inspect_legacy_directory(root, limits=IntakeLimits(max_artifact_count=1))
    with pytest.raises(ResourceLimitError, match="total byte"):
        inspect_legacy_directory(root, limits=IntakeLimits(max_total_bytes=1))


def test_manifest_mapping_round_trip_and_strict_fields(tmp_path: Path) -> None:
    _, manifest = _package_dir(tmp_path)
    mapping = _mapping(manifest)
    assert manifest_from_mapping(mapping) == manifest
    extra = dict(mapping)
    extra["unknown"] = 1
    with pytest.raises(ValidationError, match="fields"):
        manifest_from_mapping(extra)
    missing = dict(mapping)
    missing.pop("seed")
    with pytest.raises(ValidationError, match="fields"):
        manifest_from_mapping(missing)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("artifact_inventory", {}, "must be a list"),
        ("dataset_ids", "dataset", "string list"),
        ("created_at", 1, "ISO timestamp"),
        ("created_at", "bad", "created_at is invalid"),
        ("environment_manifest", [], "must be an object"),
        ("seed", True, "non-negative integer"),
    ],
)
def test_manifest_mapping_rejects_invalid_types(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    _, manifest = _package_dir(tmp_path)
    mapping = _mapping(manifest)
    mapping[field] = value
    with pytest.raises(ValidationError, match=message):
        manifest_from_mapping(mapping)


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_producer_schema_versions": set()},
        {"allowed_producer_schema_versions": frozenset()},
        {"allowed_package_schema_versions": frozenset()},
        {"allowed_artifact_schema_prefixes": ()},
        {"required_artifact_paths": {"data.json"}},
    ],
)
def test_producer_profile_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _profile(**changes)


def test_profile_rejects_schema_and_required_path_mismatch(tmp_path: Path) -> None:
    root, _ = _package_dir(tmp_path)
    with pytest.raises(UnsupportedSchemaError):
        validate_artifact_package(
            root,
            _profile(allowed_producer_schema_versions=frozenset({"2"})),
        )
    with pytest.raises(IntegrityError, match="required producer artifacts"):
        validate_artifact_package(
            root,
            _profile(required_artifact_paths=frozenset({"missing.json"})),
        )


def test_intake_rejects_non_directory_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="real directory"):
        inspect_legacy_directory(tmp_path / "missing")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValidationError, match="real directory"):
        validate_artifact_package(link, _profile())
