from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .artifact_store import FileLimits, read_verified_bytes
from .errors import IntegrityError, ResourceLimitError, ValidationError
from .intake_models import ArtifactInventoryItem, ArtifactPackageManifest

_MANIFEST_FILENAME = "artifact-package.json"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _load_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        _, data = read_verified_bytes(path, limits=FileLimits(max_file_bytes=max_bytes))
    except ResourceLimitError as exc:
        raise ResourceLimitError("manifest exceeds size limit") from exc

    def reject_constant(value: str) -> None:
        raise IntegrityError(f"non-finite JSON constant is forbidden: {value}")

    try:
        value = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("artifact package manifest is invalid JSON") from exc
    if not isinstance(value, dict):
        raise IntegrityError("artifact package manifest must be an object")
    return value


def _string_value(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{name} must be a string list")
    return tuple(value)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be an object")
    return value


def manifest_from_mapping(value: Mapping[str, Any]) -> ArtifactPackageManifest:
    allowed = {
        "schema_id",
        "schema_version",
        "package_id",
        "package_version",
        "producer_module_id",
        "producer_module_version",
        "producer_schema_version",
        "experiment_id",
        "research_id",
        "stage_id",
        "run_id",
        "parent_experiment_id",
        "strategy_id",
        "strategy_version",
        "module_api_version",
        "backtest_engine_version",
        "metric_schema_version",
        "configuration_hash",
        "dataset_ids",
        "dataset_hashes",
        "commit_sha",
        "branch",
        "working_tree_status",
        "diff_hash",
        "patch_artifact_path",
        "dependency_lock_hash",
        "environment_manifest",
        "split_information",
        "seed",
        "scenario_id",
        "created_at",
        "artifact_inventory",
        "expected_references",
        "audit_evidence",
        "completion_status",
        "integrity_checksum",
    }
    if set(value) != allowed:
        raise ValidationError("artifact package manifest fields are invalid")
    inventory_raw = value["artifact_inventory"]
    if not isinstance(inventory_raw, list):
        raise ValidationError("artifact_inventory must be a list")
    inventory: list[ArtifactInventoryItem] = []
    inventory_fields = {
        "relative_path",
        "sha256",
        "size_bytes",
        "media_type",
        "schema_id",
        "schema_version",
    }
    for raw in inventory_raw:
        item = _mapping(raw, "artifact inventory item")
        if set(item) != inventory_fields:
            raise ValidationError("artifact inventory item fields are invalid")
        size = item["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValidationError("artifact size_bytes must be an integer")
        inventory.append(
            ArtifactInventoryItem(
                relative_path=_string_value(item["relative_path"], "relative_path"),
                sha256=_string_value(item["sha256"], "sha256"),
                size_bytes=size,
                media_type=_string_value(item["media_type"], "media_type"),
                schema_id=_string_value(item["schema_id"], "schema_id"),
                schema_version=_string_value(item["schema_version"], "schema_version"),
            )
        )
    expected_raw = _mapping(value.get("expected_references", {}), "expected_references")
    expected: dict[str, tuple[str, ...]] = {}
    for key, item in expected_raw.items():
        if not isinstance(key, str):
            raise ValidationError("expected_references keys must be strings")
        expected[key] = _str_tuple(item, f"expected_references.{key}")
    created_raw = value.get("created_at")
    if not isinstance(created_raw, str):
        raise ValidationError("created_at must be an ISO timestamp")
    try:
        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("created_at is invalid") from exc
    return ArtifactPackageManifest(
        schema_id=_string_value(value["schema_id"], "schema_id"),
        schema_version=_string_value(value["schema_version"], "schema_version"),
        package_id=_string_value(value["package_id"], "package_id"),
        package_version=_string_value(value["package_version"], "package_version"),
        producer_module_id=_string_value(value["producer_module_id"], "producer_module_id"),
        producer_module_version=_string_value(
            value["producer_module_version"], "producer_module_version"
        ),
        producer_schema_version=_string_value(
            value["producer_schema_version"], "producer_schema_version"
        ),
        experiment_id=_string_value(value["experiment_id"], "experiment_id"),
        research_id=_string_value(value["research_id"], "research_id"),
        stage_id=_string_value(value["stage_id"], "stage_id"),
        run_id=_string_value(value["run_id"], "run_id"),
        parent_experiment_id=value.get("parent_experiment_id"),
        strategy_id=_string_value(value["strategy_id"], "strategy_id"),
        strategy_version=_string_value(value["strategy_version"], "strategy_version"),
        module_api_version=_string_value(value["module_api_version"], "module_api_version"),
        backtest_engine_version=_string_value(
            value["backtest_engine_version"], "backtest_engine_version"
        ),
        metric_schema_version=_string_value(
            value["metric_schema_version"], "metric_schema_version"
        ),
        configuration_hash=_string_value(value["configuration_hash"], "configuration_hash"),
        dataset_ids=_str_tuple(value["dataset_ids"], "dataset_ids"),
        dataset_hashes=_str_tuple(value["dataset_hashes"], "dataset_hashes"),
        commit_sha=_string_value(value["commit_sha"], "commit_sha"),
        branch=_string_value(value["branch"], "branch"),
        working_tree_status=_string_value(value["working_tree_status"], "working_tree_status"),
        diff_hash=value.get("diff_hash"),
        patch_artifact_path=value.get("patch_artifact_path"),
        dependency_lock_hash=_string_value(value["dependency_lock_hash"], "dependency_lock_hash"),
        environment_manifest=_mapping(
            value.get("environment_manifest", {}), "environment_manifest"
        ),
        split_information=_mapping(value.get("split_information", {}), "split_information"),
        seed=value["seed"],
        scenario_id=_string_value(value["scenario_id"], "scenario_id"),
        created_at=created_at,
        artifact_inventory=tuple(inventory),
        expected_references=expected,
        audit_evidence=_str_tuple(value.get("audit_evidence", []), "audit_evidence"),
        completion_status=_string_value(value["completion_status"], "completion_status"),
        integrity_checksum=_string_value(value["integrity_checksum"], "integrity_checksum"),
    )
