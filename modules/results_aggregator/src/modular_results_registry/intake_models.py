from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .artifact_store import FileLimits, VerifiedFile
from .canonical import canonical_sha256, deep_freeze, deterministic_id
from .errors import IntegrityError, UnsupportedSchemaError, ValidationError
from .models import (
    nonblank,
    safe_relative_path,
    utc,
    validate_commit_sha,
    validate_id,
    validate_schema,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class ArtifactInventoryItem:
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    schema_id: str
    schema_version: str

    def __post_init__(self) -> None:
        safe_relative_path(self.relative_path)
        validate_sha256("sha256", self.sha256)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValidationError("artifact size must be an integer")
        if self.size_bytes < 0:
            raise ValidationError("artifact size cannot be negative")
        nonblank("media_type", self.media_type)
        validate_schema("schema_id", self.schema_id)
        validate_schema("schema_version", self.schema_version)


@dataclass(frozen=True, slots=True)
class ArtifactPackageManifest:
    schema_id: str
    schema_version: str
    package_id: str
    package_version: str
    producer_module_id: str
    producer_module_version: str
    producer_schema_version: str
    experiment_id: str
    research_id: str
    stage_id: str
    run_id: str
    parent_experiment_id: str | None
    strategy_id: str
    strategy_version: str
    module_api_version: str
    backtest_engine_version: str
    metric_schema_version: str
    configuration_hash: str
    dataset_ids: tuple[str, ...]
    dataset_hashes: tuple[str, ...]
    commit_sha: str
    branch: str
    working_tree_status: str
    diff_hash: str | None
    patch_artifact_path: str | None
    dependency_lock_hash: str
    environment_manifest: Mapping[str, Any]
    split_information: Mapping[str, Any]
    seed: int
    scenario_id: str
    created_at: datetime
    artifact_inventory: tuple[ArtifactInventoryItem, ...]
    expected_references: Mapping[str, tuple[str, ...]]
    audit_evidence: tuple[str, ...]
    completion_status: str
    integrity_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_ids, tuple) or not isinstance(self.dataset_hashes, tuple):
            raise ValidationError("dataset_ids and dataset_hashes must be tuples")
        if not isinstance(self.artifact_inventory, tuple) or not all(
            isinstance(item, ArtifactInventoryItem) for item in self.artifact_inventory
        ):
            raise ValidationError("artifact_inventory must be a tuple of ArtifactInventoryItem")
        if not isinstance(self.audit_evidence, tuple):
            raise ValidationError("audit_evidence must be a tuple")
        if not isinstance(self.expected_references, Mapping):
            raise ValidationError("expected_references must be a mapping")
        if not isinstance(self.environment_manifest, Mapping) or not isinstance(
            self.split_information, Mapping
        ):
            raise ValidationError("environment_manifest and split_information must be mappings")
        for name in (
            "schema_id",
            "schema_version",
            "package_version",
            "producer_schema_version",
            "module_api_version",
            "metric_schema_version",
        ):
            validate_schema(name, getattr(self, name))
        for name in (
            "package_id",
            "experiment_id",
            "research_id",
            "stage_id",
            "run_id",
            "strategy_id",
            "scenario_id",
        ):
            validate_id(name, getattr(self, name))
        if self.parent_experiment_id is not None:
            validate_id("parent_experiment_id", self.parent_experiment_id)
        for name in (
            "producer_module_id",
            "producer_module_version",
            "strategy_version",
            "backtest_engine_version",
            "branch",
            "working_tree_status",
            "completion_status",
        ):
            nonblank(name, getattr(self, name))
        validate_sha256("configuration_hash", self.configuration_hash)
        validate_sha256("dependency_lock_hash", self.dependency_lock_hash)
        validate_commit_sha(self.commit_sha)
        if self.diff_hash is not None:
            validate_sha256("diff_hash", self.diff_hash)
        if self.patch_artifact_path is not None:
            safe_relative_path(self.patch_artifact_path)
        if self.working_tree_status != "CLEAN" and self.patch_artifact_path is None:
            raise ValidationError("dirty package requires patch_artifact_path")
        if len(self.dataset_ids) != len(self.dataset_hashes) or not self.dataset_ids:
            raise ValidationError("dataset IDs and hashes must be non-empty and aligned")
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValidationError("dataset IDs must be unique")
        for dataset_id in self.dataset_ids:
            validate_id("dataset_id", dataset_id)
        for digest in self.dataset_hashes:
            validate_sha256("dataset_hash", digest)
        paths = tuple(item.relative_path for item in self.artifact_inventory)
        if not paths or len(set(paths)) != len(paths):
            raise ValidationError("artifact inventory paths must be non-empty and unique")
        if self.patch_artifact_path is not None and self.patch_artifact_path not in paths:
            raise ValidationError("patch_artifact_path must exist in artifact inventory")
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValidationError("seed must be a non-negative integer")
        normalized_references: dict[str, tuple[str, ...]] = {}
        for key, values in self.expected_references.items():
            nonblank("expected reference category", key)
            if not isinstance(values, tuple):
                raise ValidationError("expected reference values must be tuples")
            if len(set(values)) != len(values):
                raise ValidationError("expected reference values must be unique")
            for value in values:
                validate_id("expected reference", value)
            normalized_references[key] = tuple(sorted(values))
        object.__setattr__(
            self, "expected_references", deep_freeze(dict(sorted(normalized_references.items())))
        )
        for evidence_id in self.audit_evidence:
            validate_id("audit_evidence_id", evidence_id)
        if len(set(self.audit_evidence)) != len(self.audit_evidence):
            raise ValidationError("audit_evidence IDs must be unique")
        object.__setattr__(self, "environment_manifest", deep_freeze(self.environment_manifest))
        object.__setattr__(self, "split_information", deep_freeze(self.split_information))
        validate_sha256("integrity_checksum", self.integrity_checksum)

    def checksum_payload(self) -> Mapping[str, Any]:
        from dataclasses import fields

        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "integrity_checksum"
        }

    def verify_checksum(self) -> None:
        if canonical_sha256(self.checksum_payload()) != self.integrity_checksum:
            raise IntegrityError("artifact package integrity checksum mismatch")


@dataclass(frozen=True, slots=True)
class ProducerProfile:
    producer_module_id: str
    allowed_producer_schema_versions: frozenset[str]
    allowed_artifact_schema_prefixes: tuple[str, ...]
    required_artifact_paths: frozenset[str] = frozenset()
    allowed_package_schema_versions: frozenset[str] = frozenset({"1"})

    def __post_init__(self) -> None:
        nonblank("producer_module_id", self.producer_module_id)
        for name in (
            "allowed_producer_schema_versions",
            "required_artifact_paths",
            "allowed_package_schema_versions",
        ):
            value = getattr(self, name)
            if not isinstance(value, frozenset):
                raise ValidationError(f"{name} must be a frozenset")
        if not self.allowed_producer_schema_versions:
            raise ValidationError("allowed_producer_schema_versions cannot be empty")
        if not self.allowed_package_schema_versions:
            raise ValidationError("allowed_package_schema_versions cannot be empty")
        if (
            not isinstance(self.allowed_artifact_schema_prefixes, tuple)
            or not self.allowed_artifact_schema_prefixes
        ):
            raise ValidationError("allowed_artifact_schema_prefixes must be a non-empty tuple")
        for value in self.allowed_producer_schema_versions:
            nonblank("allowed producer schema version", value)
        for value in self.allowed_package_schema_versions:
            nonblank("allowed package schema version", value)
        for value in self.allowed_artifact_schema_prefixes:
            nonblank("allowed artifact schema prefix", value)
        for value in self.required_artifact_paths:
            safe_relative_path(value)

    def validate(self, manifest: ArtifactPackageManifest) -> None:
        if not isinstance(manifest, ArtifactPackageManifest):
            raise ValidationError("manifest must be an ArtifactPackageManifest")
        if manifest.producer_module_id != self.producer_module_id:
            raise ValidationError("producer profile does not match manifest")
        if manifest.schema_version not in self.allowed_package_schema_versions:
            raise UnsupportedSchemaError("unsupported artifact package schema version")
        if manifest.producer_schema_version not in self.allowed_producer_schema_versions:
            raise UnsupportedSchemaError("unsupported producer schema version")
        paths = {item.relative_path for item in manifest.artifact_inventory}
        missing = sorted(self.required_artifact_paths - paths)
        if missing:
            raise IntegrityError(f"required producer artifacts are missing: {missing}")
        for item in manifest.artifact_inventory:
            if not any(
                item.schema_id.startswith(prefix)
                for prefix in self.allowed_artifact_schema_prefixes
            ):
                raise UnsupportedSchemaError(
                    f"artifact schema {item.schema_id!r} is not allowed for producer"
                )


@dataclass(frozen=True, slots=True)
class IntakeLimits:
    max_artifact_count: int = 10_000
    max_total_bytes: int = 100 * 1024 * 1024 * 1024
    file_limits: FileLimits = FileLimits()

    def __post_init__(self) -> None:
        for name in ("max_artifact_count", "max_total_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("intake limits must be positive integers")
        if not isinstance(self.file_limits, FileLimits):
            raise ValueError("file_limits must be FileLimits")


@dataclass(frozen=True, slots=True)
class VerifiedArtifactPackage:
    manifest: ArtifactPackageManifest
    verified_files: tuple[VerifiedFile, ...]
    validation_hash: str
    duplicate_content: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ArtifactPackageManifest):
            raise ValidationError("manifest must be an ArtifactPackageManifest")
        if not isinstance(self.verified_files, tuple) or not all(
            isinstance(item, VerifiedFile) for item in self.verified_files
        ):
            raise ValidationError("verified_files must be a tuple of VerifiedFile")
        validate_sha256("validation_hash", self.validation_hash)
        normalized: dict[str, tuple[str, ...]] = {}
        for digest, paths in self.duplicate_content.items():
            validate_sha256("duplicate content hash", digest)
            if not isinstance(paths, tuple) or len(paths) < 2:
                raise ValidationError("duplicate content paths must contain at least two paths")
            for path in paths:
                safe_relative_path(path)
            normalized[digest] = tuple(sorted(paths))
        object.__setattr__(self, "duplicate_content", deep_freeze(dict(sorted(normalized.items()))))


@dataclass(frozen=True, slots=True)
class LegacyFileRecord:
    relative_path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        safe_relative_path(self.relative_path)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValidationError("legacy file size must be an integer")
        if self.size_bytes < 0:
            raise ValidationError("legacy file size cannot be negative")
        validate_sha256("legacy file SHA-256", self.sha256)


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    profile: str
    files: tuple[LegacyFileRecord, ...]
    missing_lineage: tuple[str, ...]
    limitations: tuple[str, ...]
    report_id: str

    def __post_init__(self) -> None:
        if self.profile != "LEGACY_UNVERIFIED_IMPORT":
            raise ValidationError("legacy report profile must remain unverified")
        if not isinstance(self.files, tuple) or not all(
            isinstance(item, LegacyFileRecord) for item in self.files
        ):
            raise ValidationError("legacy files must be a tuple of LegacyFileRecord")
        for name in ("missing_lineage", "limitations"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) and item.strip() for item in values
            ):
                raise ValidationError(f"{name} must be a tuple of non-blank strings")
            if len(set(values)) != len(values):
                raise ValidationError(f"{name} must be unique")
        expected = deterministic_id(
            "legacy",
            {
                "files": tuple(
                    {"path": item.relative_path, "sha256": item.sha256} for item in self.files
                ),
                "missing": self.missing_lineage,
            },
            digest_chars=48,
        )
        if self.report_id != expected:
            raise IntegrityError("legacy report ID mismatch")
