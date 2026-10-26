from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .effective_config import (
    EFFECTIVE_CONFIG_FILENAME,
    EFFECTIVE_CONFIG_SCHEMA_ID,
    EFFECTIVE_CONFIG_SCHEMA_VERSION,
    read_sidecar_for_job,
    sidecar_path_for_job,
)
from .engine import BacktestJob
from .manifest import ExperimentManifest
from .models import ResearchSpecification, SpecificationError
from .results import RunResultEnvelope

_MANIFEST_FILENAME = "artifact-package.json"
_ENGINE_MANIFEST_FILENAME = "artifact-manifest.json"
_REQUIRED_FILES = (
    "run.json",
    "metrics.json",
    "journal.json",
    "trades.csv",
    "equity.csv",
    "application-run.json",
    "metrics-extended.json",
    "data-validation.json",
    "restart-evidence.json",
    _ENGINE_MANIFEST_FILENAME,
)
_EVENT_STREAM_FILES = ("events.jsonl", "events.jsonl.gz")
_SHARDED_EVENT_MANIFEST = "events.manifest.json"
_SHARDED_EVENT_PREFIX = "events.jsonl.gz.part-"
_SHARDED_EVENT_ENCODING = "gzip-byte-shards-v1"
_OPTIONAL_FILES = (
    "replay-checkpoint.json",
    "environment.json",
    "SHA256SUMS.txt",
)
_CORE_FILE_SCHEMAS: Mapping[str, tuple[str, str, str]] = {
    "run.json": ("application/json", "modular-core.run-export", "0.4.0"),
    "metrics.json": ("application/json", "modular-core.metrics", "0.4.0"),
    "journal.json": ("application/json", "modular-core.journal", "0.4.0"),
    "trades.csv": ("text/csv", "modular-core.trades", "0.4.0"),
    "equity.csv": ("text/csv", "modular-core.equity", "0.4.0"),
    "events.jsonl": ("application/x-ndjson", "modular-core.events", "0.4.0"),
    "events.jsonl.gz": ("application/gzip", "modular-core.events", "0.4.0"),
    _SHARDED_EVENT_MANIFEST: (
        "application/json",
        "modular-core.events-shard-manifest",
        "1",
    ),
}
_ENGINE_MEDIA_TYPES: Mapping[str, str] = {
    "artifact-manifest.json": "application/json",
    "application-run.json": "application/json",
    "metrics-extended.json": "application/json",
    "data-validation.json": "application/json",
    "restart-evidence.json": "application/json",
    "replay-checkpoint.json": "application/json",
    "environment.json": "application/json",
    "SHA256SUMS.txt": "text/plain",
}
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class RegistryPackageReference:
    package_id: str
    package_directory: Path
    manifest_sha256: str
    manifest_size_bytes: int
    inventory_sha256: str


class RegistryPackageSink(Protocol):
    def publish(
        self,
        *,
        envelope: RunResultEnvelope,
        experiment_manifest: ExperimentManifest,
        specification: ResearchSpecification,
        job: BacktestJob,
    ) -> RegistryPackageReference: ...


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SpecificationError("registry package timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _registry_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _registry_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_registry_json_bytes(value)).hexdigest()


def _file_reference(
    path: Path,
    *,
    max_file_bytes: int,
    aggregate_digest: Any | None = None,
) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise SpecificationError(f"registry package artifact is not a regular file: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > max_file_bytes:
                raise SpecificationError(f"registry package artifact exceeds limit: {path.name}")
            digest.update(chunk)
            if aggregate_digest is not None:
                aggregate_digest.update(chunk)
    return digest.hexdigest(), size


def _load_json_mapping(path: Path, *, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SpecificationError(f"{label} is missing or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecificationError(f"{label} is invalid") from exc
    if not isinstance(value, Mapping):
        raise SpecificationError(f"{label} must be an object")
    return value


def _load_engine_manifest(root: Path) -> Mapping[str, Any]:
    path = root / _ENGINE_MANIFEST_FILENAME
    if path.is_symlink() or not path.is_file():
        raise SpecificationError("Backtest Engine output is missing artifact-manifest.json")
    value = _load_json_mapping(path, label="Backtest Engine artifact manifest")
    if value.get("schema_version") != "1":
        raise SpecificationError("unsupported Backtest Engine artifact manifest schema")
    files = value.get("files")
    if not isinstance(files, Mapping) or not files:
        raise SpecificationError("Backtest Engine artifact manifest files are invalid")
    return value


def _lower_sha256(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise SpecificationError(f"{label} must be a lowercase SHA-256")
    return value


def _positive_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SpecificationError(f"{label} must be a positive integer")
    return value


def _load_sharded_event_contract(
    root: Path,
    actual_files: set[str],
    *,
    max_file_bytes: int,
) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
    manifest = _load_json_mapping(
        root / _SHARDED_EVENT_MANIFEST,
        label="Backtest Engine sharded event manifest",
    )
    if manifest.get("schema_version") != "1":
        raise SpecificationError("unsupported Backtest Engine sharded event manifest schema")
    if manifest.get("encoding") != _SHARDED_EVENT_ENCODING:
        raise SpecificationError("Backtest Engine sharded event encoding changed")
    if (
        manifest.get("source_encoding") != "gzip"
        or manifest.get("source_path") != "events.jsonl.gz"
    ):
        raise SpecificationError("Backtest Engine sharded event source contract changed")
    _positive_int(manifest.get("compressed_bytes"), label="sharded compressed_bytes")
    _positive_int(manifest.get("logical_bytes"), label="sharded logical_bytes")
    _positive_int(manifest.get("event_count"), label="sharded event_count")
    _lower_sha256(manifest.get("compressed_sha256"), label="sharded compressed_sha256")
    _lower_sha256(manifest.get("semantic_sha256"), label="sharded semantic_sha256")

    raw_parts = manifest.get("parts")
    if not isinstance(raw_parts, list) or len(raw_parts) < 2:
        raise SpecificationError("Backtest Engine sharded events require at least two parts")
    parts: dict[str, Mapping[str, Any]] = {}
    for expected_index, raw in enumerate(raw_parts, start=1):
        if not isinstance(raw, Mapping):
            raise SpecificationError("Backtest Engine sharded event part must be an object")
        if raw.get("index") != expected_index:
            raise SpecificationError("Backtest Engine sharded event indices are not ordered")
        expected_path = f"{_SHARDED_EVENT_PREFIX}{expected_index:06d}"
        if raw.get("path") != expected_path:
            raise SpecificationError("Backtest Engine sharded event path is not deterministic")
        size = _positive_int(raw.get("bytes"), label=f"{expected_path} bytes")
        if size >= max_file_bytes:
            raise SpecificationError(
                "Backtest Engine event shard does not retain Registry headroom"
            )
        _lower_sha256(raw.get("sha256"), label=f"{expected_path} sha256")
        parts[expected_path] = raw

    actual_part_files = {name for name in actual_files if name.startswith(_SHARDED_EVENT_PREFIX)}
    if actual_part_files != set(parts):
        raise SpecificationError(
            "Backtest Engine sharded event part inventory changed; "
            f"expected={sorted(parts)}, actual={sorted(actual_part_files)}"
        )

    journal = _load_json_mapping(root / "journal.json", label="Backtest Engine journal")
    stream = journal.get("event_stream")
    if journal.get("schema_version") != "4" or not isinstance(stream, Mapping):
        raise SpecificationError("Backtest Engine sharded events require journal schema v4")
    expected_pairs = (
        (stream.get("path"), _SHARDED_EVENT_MANIFEST, "path"),
        (stream.get("encoding"), _SHARDED_EVENT_ENCODING, "encoding"),
        (stream.get("bytes"), manifest.get("compressed_bytes"), "compressed bytes"),
        (stream.get("logical_bytes"), manifest.get("logical_bytes"), "logical bytes"),
        (stream.get("count"), manifest.get("event_count"), "event count"),
        (stream.get("semantic_sha256"), manifest.get("semantic_sha256"), "semantic SHA-256"),
        (stream.get("compressed_sha256"), manifest.get("compressed_sha256"), "compressed SHA-256"),
        (stream.get("part_count"), len(parts), "part count"),
    )
    for actual, expected, label in expected_pairs:
        if actual != expected:
            raise SpecificationError(f"Backtest Engine sharded event journal {label} changed")

    application = _load_json_mapping(
        root / "application-run.json",
        label="Backtest Engine application-run.json",
    )
    if application.get("artifact_schema_version") != "4":
        raise SpecificationError(
            "Backtest Engine sharded events require application artifact schema v4"
        )
    environment_path = root / "environment.json"
    if environment_path.exists() or environment_path.is_symlink():
        environment = _load_json_mapping(
            environment_path,
            label="Backtest Engine environment.json",
        )
        if environment.get("artifact_schema_version") != "4":
            raise SpecificationError(
                "Backtest Engine sharded events require environment artifact schema v4"
            )
    return manifest, parts


def _copy_verified(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    max_file_bytes: int,
) -> None:
    if source.is_symlink() or not source.is_file():
        raise SpecificationError(f"source artifact is not a regular file: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    actual_sha256, actual_size = _file_reference(
        destination,
        max_file_bytes=max_file_bytes,
    )
    if actual_sha256 != expected_sha256 or actual_size != expected_size_bytes:
        raise SpecificationError(f"copied registry artifact failed verification: {source.name}")


class LocalResultsAggregatorPackageSink:
    """Builds an immutable Aggregator package without modifying Engine output files."""

    def __init__(
        self,
        root: Path,
        *,
        producer_module_version: str,
        producer_schema_version: str = "1",
        package_version: str = "1",
        max_file_bytes: int = 16 * 1024 * 1024 * 1024,
        max_total_bytes: int = 100 * 1024 * 1024 * 1024,
    ) -> None:
        for name, value in (
            ("producer_module_version", producer_module_version),
            ("producer_schema_version", producer_schema_version),
            ("package_version", package_version),
        ):
            if not value.strip():
                raise SpecificationError(f"{name} must be non-empty")
        for limit_name, limit_value in (
            ("max_file_bytes", max_file_bytes),
            ("max_total_bytes", max_total_bytes),
        ):
            if not isinstance(limit_value, int) or isinstance(limit_value, bool) or limit_value < 1:
                raise SpecificationError(f"{limit_name} must be a positive integer")
        self.root = root
        if self.root.is_symlink():
            raise SpecificationError("registry package root must not be a symlink")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SpecificationError("cannot create registry package root") from exc
        if not self.root.is_dir():
            raise SpecificationError("registry package root must be a directory")
        self.producer_module_version = producer_module_version
        self.producer_schema_version = producer_schema_version
        self.package_version = package_version
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def _inventory(
        self,
        source_root: Path,
        *,
        engine_version: str,
        expected_engine_run_id: str,
    ) -> list[dict[str, Any]]:
        engine_manifest = _load_engine_manifest(source_root)
        if engine_manifest.get("run_id") != expected_engine_run_id:
            raise SpecificationError("Backtest Engine artifact manifest run_id has changed")
        declared = engine_manifest["files"]
        if not isinstance(declared, Mapping):
            raise SpecificationError("Backtest Engine artifact manifest files are invalid")
        source_paths = tuple(source_root.rglob("*"))
        symlinks = sorted(
            path.relative_to(source_root).as_posix() for path in source_paths if path.is_symlink()
        )
        if symlinks:
            raise SpecificationError(f"Backtest Engine artifacts contain symlinks: {symlinks}")
        actual_files = {
            path.relative_to(source_root).as_posix() for path in source_paths if path.is_file()
        }
        legacy_event_files = actual_files.intersection(_EVENT_STREAM_FILES)
        has_sharded_manifest = _SHARDED_EVENT_MANIFEST in actual_files
        if legacy_event_files and has_sharded_manifest:
            raise SpecificationError("Backtest Engine event stream representations are ambiguous")
        shard_manifest: Mapping[str, Any] | None = None
        shard_parts: dict[str, Mapping[str, Any]] = {}
        if has_sharded_manifest:
            shard_manifest, shard_parts = _load_sharded_event_contract(
                source_root,
                actual_files,
                max_file_bytes=self.max_file_bytes,
            )
        elif len(legacy_event_files) != 1:
            raise SpecificationError(
                "Backtest Engine artifact inventory mismatch; "
                f"event_streams={sorted(legacy_event_files)}"
            )

        allowed_files = set(_REQUIRED_FILES + _EVENT_STREAM_FILES + _OPTIONAL_FILES)
        if has_sharded_manifest:
            allowed_files.add(_SHARDED_EVENT_MANIFEST)
            allowed_files.update(shard_parts)
        missing = sorted(set(_REQUIRED_FILES) - actual_files)
        extras = sorted(actual_files - allowed_files)
        if missing or extras:
            raise SpecificationError(
                f"Backtest Engine artifact inventory mismatch; missing={missing}, extras={extras}"
            )
        declared_names = set(declared)
        actual_without_manifest = actual_files - {_ENGINE_MANIFEST_FILENAME}
        if declared_names != actual_without_manifest:
            raise SpecificationError("Backtest Engine artifact manifest file set has changed")

        aggregate = hashlib.sha256() if shard_manifest is not None else None
        shard_total = 0
        inventory: list[dict[str, Any]] = []
        total_size = 0
        for relative_path in sorted(actual_files):
            part_contract = shard_parts.get(relative_path)
            digest, size = _file_reference(
                source_root / relative_path,
                max_file_bytes=self.max_file_bytes,
                aggregate_digest=aggregate if part_contract is not None else None,
            )
            total_size += size
            if total_size > self.max_total_bytes:
                raise SpecificationError("registry package exceeds total byte limit")
            if relative_path != _ENGINE_MANIFEST_FILENAME:
                expected_digest = declared.get(relative_path)
                if expected_digest != digest:
                    raise SpecificationError(
                        f"Backtest Engine artifact checksum mismatch: {relative_path}"
                    )
            if part_contract is not None:
                if part_contract.get("sha256") != digest or part_contract.get("bytes") != size:
                    raise SpecificationError(
                        f"Backtest Engine sharded event part contract changed: {relative_path}"
                    )
                shard_total += size

            if relative_path in _CORE_FILE_SCHEMAS:
                media_type, schema_id, schema_version = _CORE_FILE_SCHEMAS[relative_path]
            elif part_contract is not None:
                media_type = "application/octet-stream"
                schema_id = "modular-core.events-gzip-byte-shard"
                schema_version = "1"
            else:
                media_type = _ENGINE_MEDIA_TYPES[relative_path]
                schema_id = f"modular-backtest.{Path(relative_path).stem}"
                schema_version = (
                    "1" if relative_path == _ENGINE_MANIFEST_FILENAME else engine_version
                )
            inventory.append(
                {
                    "relative_path": relative_path,
                    "sha256": digest,
                    "size_bytes": size,
                    "media_type": media_type,
                    "schema_id": schema_id,
                    "schema_version": schema_version,
                }
            )

        if shard_manifest is not None:
            if aggregate is None:
                raise SpecificationError("sharded event aggregate verifier was not initialized")
            expected_total = shard_manifest.get("compressed_bytes")
            expected_sha = shard_manifest.get("compressed_sha256")
            if shard_total != expected_total or aggregate.hexdigest() != expected_sha:
                raise SpecificationError("Backtest Engine sharded event aggregate identity changed")
        return inventory

    def publish(
        self,
        *,
        envelope: RunResultEnvelope,
        experiment_manifest: ExperimentManifest,
        specification: ResearchSpecification,
        job: BacktestJob,
    ) -> RegistryPackageReference:
        job_context = (
            job.run_id,
            job.experiment_id,
            job.stage_id,
            job.stage_role,
            job.split_id,
            job.dataset_id,
            job.scenario_id,
            job.seed,
        )
        envelope_context = (
            envelope.run_id,
            envelope.experiment_id,
            envelope.stage_id,
            envelope.stage_role,
            envelope.split_id,
            envelope.dataset_id,
            envelope.scenario_id,
            envelope.seed,
        )
        if envelope_context != job_context:
            raise SpecificationError("registry package context does not match the result envelope")
        if experiment_manifest.experiment_id != job.experiment_id:
            raise SpecificationError("registry package manifest belongs to another experiment")
        if experiment_manifest.specification_sha256 != specification.fingerprint:
            raise SpecificationError("registry package specification lineage has changed")
        if (
            experiment_manifest.strategy_id != specification.strategy_id
            or experiment_manifest.strategy_version != specification.strategy_version
        ):
            raise SpecificationError("registry package strategy lineage has changed")
        configuration_hash = job.metadata.get("configuration_sha256")
        if (
            not isinstance(configuration_hash, str)
            or configuration_hash != envelope.configuration_sha256
            or configuration_hash not in experiment_manifest.configuration_hashes
        ):
            raise SpecificationError("registry package configuration lineage has changed")
        if experiment_manifest.dataset_hashes.get(job.dataset_id) != envelope.dataset_sha256:
            raise SpecificationError("registry package dataset lineage has changed")
        if experiment_manifest.metric_schema_version != envelope.metrics_schema_version:
            raise SpecificationError("registry package metric schema lineage has changed")
        if not experiment_manifest.git.clean:
            raise SpecificationError("registry package publication requires clean Git provenance")
        dependency_lock_hash = experiment_manifest.environment.dependency_lock_sha256
        if dependency_lock_hash is None:
            raise SpecificationError("registry package requires dependency_lock_sha256")
        if job.period is None:
            raise SpecificationError("registry package job must define an evaluation period")
        if envelope.seed < 0:
            raise SpecificationError("registry package seed must be non-negative")

        source_root = envelope.engine.output_directory
        if source_root.is_symlink() or not source_root.is_dir():
            raise SpecificationError("Backtest Engine output must be a real directory")
        inventory = self._inventory(
            source_root,
            engine_version=experiment_manifest.backtest_engine_version,
            expected_engine_run_id=envelope.engine.engine_run_id,
        )
        extra_source_paths: dict[str, Path] = {}
        effective_reference = envelope.engine.effective_config_artifact
        if envelope.schema_version == 2:
            if effective_reference is None:
                raise SpecificationError("v2 result envelope is missing effective config proof")
            try:
                _, effective_document = read_sidecar_for_job(
                    job.output_directory, effective_reference
                )
            except SpecificationError as exc:
                raise SpecificationError("effective config proof is missing or invalid") from exc
            context = effective_document["context"]
            expected_effective_context = {
                "run_id": job.run_id,
                "engine_run_id": envelope.engine.engine_run_id,
                "experiment_id": job.experiment_id,
                "stage_id": job.stage_id,
                "stage_role": job.stage_role,
                "split_id": job.split_id,
                "dataset_id": job.dataset_id,
                "dataset_sha256": envelope.dataset_sha256,
                "scenario_id": job.scenario_id,
                "seed": job.seed,
                "configuration_sha256": envelope.configuration_sha256,
            }
            if any(
                context.get(key) != expected for key, expected in expected_effective_context.items()
            ):
                raise SpecificationError(
                    "effective config proof context differs from result envelope"
                )
            if job.period is None or (
                context.get("period_start") != _iso_timestamp(job.period.start)
                or context.get("period_end") != _iso_timestamp(job.period.end)
            ):
                raise SpecificationError("effective config proof period differs from Research job")
            if EFFECTIVE_CONFIG_FILENAME in {item["relative_path"] for item in inventory}:
                raise SpecificationError("effective config proof collides with Engine output")
            inventory.append(
                {
                    "relative_path": EFFECTIVE_CONFIG_FILENAME,
                    "sha256": effective_reference.sha256,
                    "size_bytes": effective_reference.size_bytes,
                    "media_type": "application/json",
                    "schema_id": EFFECTIVE_CONFIG_SCHEMA_ID,
                    "schema_version": EFFECTIVE_CONFIG_SCHEMA_VERSION,
                }
            )
            extra_source_paths[EFFECTIVE_CONFIG_FILENAME] = sidecar_path_for_job(
                job.output_directory
            )
        elif effective_reference is not None:
            raise SpecificationError("v1 result envelope cannot carry effective config proof")
        sharded_events = any(
            item.get("relative_path") == _SHARDED_EVENT_MANIFEST for item in inventory
        )
        compressed_events = any(
            item.get("relative_path") == "events.jsonl.gz" for item in inventory
        )
        package_schema_version = "3" if sharded_events else "2" if compressed_events else "1"
        base_document: dict[str, Any] = {
            "schema_id": "modular-results-registry.artifact-package",
            "schema_version": package_schema_version,
            "package_version": self.package_version,
            "producer_module_id": "modular-research-runner",
            "producer_module_version": self.producer_module_version,
            "producer_schema_version": self.producer_schema_version,
            "experiment_id": envelope.experiment_id,
            "research_id": experiment_manifest.research_id,
            "stage_id": envelope.stage_id,
            "run_id": envelope.run_id,
            "parent_experiment_id": experiment_manifest.parent_experiment_id,
            "strategy_id": specification.strategy_id,
            "strategy_version": specification.strategy_version,
            "module_api_version": experiment_manifest.module_api_version,
            "backtest_engine_version": experiment_manifest.backtest_engine_version,
            "metric_schema_version": envelope.metrics_schema_version,
            "configuration_hash": envelope.configuration_sha256,
            "dataset_ids": [envelope.dataset_id],
            "dataset_hashes": [envelope.dataset_sha256],
            "commit_sha": experiment_manifest.git.commit_sha,
            "branch": experiment_manifest.git.branch,
            "working_tree_status": "CLEAN",
            "diff_hash": None,
            "patch_artifact_path": None,
            "dependency_lock_hash": dependency_lock_hash,
            "environment_manifest": {
                "python_version": experiment_manifest.environment.python_version,
                "platform": experiment_manifest.environment.platform,
                "dependency_lock_sha256": dependency_lock_hash,
            },
            "split_information": {
                "split_id": envelope.split_id,
                "stage_role": envelope.stage_role,
                "period_start": _iso_timestamp(job.period.start),
                "period_end": _iso_timestamp(job.period.end),
            },
            "seed": envelope.seed,
            "scenario_id": envelope.scenario_id,
            "created_at": _iso_timestamp(envelope.created_at),
            "artifact_inventory": inventory,
            "expected_references": {},
            "audit_evidence": [],
            "completion_status": "COMPLETED",
        }
        package_id = f"rrpkg-{_registry_sha256(base_document)[:32]}"
        document: dict[str, Any] = {"package_id": package_id, **base_document}
        document["integrity_checksum"] = _registry_sha256(document)
        payload = _registry_json_bytes(document)
        destination = self.root / package_id
        if destination.exists() or destination.is_symlink():
            return self._verify_existing(destination, document, payload)

        staging = Path(tempfile.mkdtemp(prefix=f".{package_id}.staging-", dir=self.root))
        try:
            for item in inventory:
                relative_path = str(item["relative_path"])
                _copy_verified(
                    extra_source_paths.get(relative_path, source_root / relative_path),
                    staging / relative_path,
                    expected_sha256=str(item["sha256"]),
                    expected_size_bytes=int(item["size_bytes"]),
                    max_file_bytes=self.max_file_bytes,
                )
            manifest_path = staging / _MANIFEST_FILENAME
            manifest_path.write_bytes(payload)
            with manifest_path.open("rb") as handle:
                os.fsync(handle.fileno())
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            try:
                staging.rename(destination)
                root_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            except OSError:
                if not destination.exists():
                    raise
                return self._verify_existing(destination, document, payload)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self._reference(destination, package_id, inventory, payload)

    def _verify_existing(
        self,
        destination: Path,
        document: Mapping[str, Any],
        payload: bytes,
    ) -> RegistryPackageReference:
        if destination.is_symlink() or not destination.is_dir():
            raise SpecificationError("registry package destination is not a real directory")
        expected_paths = {
            str(item["relative_path"])
            for item in document["artifact_inventory"]
            if isinstance(item, Mapping)
        } | {_MANIFEST_FILENAME}
        actual_paths = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if not path.is_dir()
        }
        if actual_paths != expected_paths:
            raise SpecificationError("existing registry package inventory conflicts")
        manifest_path = destination / _MANIFEST_FILENAME
        if manifest_path.is_symlink() or manifest_path.read_bytes() != payload:
            raise SpecificationError("existing registry package manifest conflicts")
        inventory = document.get("artifact_inventory")
        if not isinstance(inventory, list):
            raise SpecificationError("existing registry package inventory is invalid")
        for item in inventory:
            if not isinstance(item, Mapping):
                raise SpecificationError("existing registry package inventory item is invalid")
            relative_path = str(item["relative_path"])
            digest, size = _file_reference(
                destination / relative_path,
                max_file_bytes=self.max_file_bytes,
            )
            if digest != item["sha256"] or size != item["size_bytes"]:
                raise SpecificationError("existing registry package artifact conflicts")
        return self._reference(
            destination,
            str(document["package_id"]),
            inventory,
            payload,
        )

    @staticmethod
    def _reference(
        destination: Path,
        package_id: str,
        inventory: Sequence[Mapping[str, Any]],
        payload: bytes,
    ) -> RegistryPackageReference:
        return RegistryPackageReference(
            package_id=package_id,
            package_directory=destination,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
            manifest_size_bytes=len(payload),
            inventory_sha256=_registry_sha256({"artifact_inventory": inventory}),
        )
