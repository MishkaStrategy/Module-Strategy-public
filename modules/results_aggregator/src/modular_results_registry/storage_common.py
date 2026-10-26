from __future__ import annotations

import os
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import canonical_json_bytes
from .errors import IntegrityError
from .safe_io import _open_directory_chain_nofollow

_SCHEMA_VERSION = 4
_MAX_ANNOTATION_ACTOR_BYTES = 512
_MAX_ANNOTATION_REASON_BYTES = 2 * 1024
_MAX_ANNOTATION_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAX_REGISTRATION_PACKAGE_METADATA_BYTES = 256 * 1024 * 1024
_MAX_REGISTRATION_ENTITIES = 100_000
_SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class RegisterOutcome:
    package_id: str
    state: str
    registry_revision: int
    registered_entities: int
    duplicate_entities: int


@dataclass(frozen=True, slots=True)
class _ConflictDetails:
    entity_type: str
    entity_id: str
    existing_content_hash: str
    incoming_content_hash: str
    incoming_payload: bytes
    correlation_id: str


class _DetectedConflict(Exception):
    def __init__(self, details: _ConflictDetails) -> None:
        super().__init__(f"{details.entity_type}:{details.entity_id} content conflict")
        self.details = details


_ENTITY_QUERIES: Mapping[str, tuple[str, str]] = {
    "artifact": ("artifacts", "artifact_id"),
    "dataset": ("datasets", "dataset_id"),
    "configuration": ("configurations", "configuration_id"),
    "experiment": ("experiments", "experiment_id"),
    "run": ("runs", "run_id"),
    "metric_definition": ("metric_definitions", "definition_id"),
    "metric": ("metrics", "metric_key"),
    "evidence": ("evidence", "evidence_id"),
    "role": ("experiment_roles", "decision_id"),
    "package": ("packages", "package_id"),
    "annotation": ("registry_annotations", "annotation_id"),
}


_ENTITY_STATE_KEYS: Mapping[str, str] = {
    entity_type: ("metric_definitions" if entity_type == "metric_definition" else f"{entity_type}s")
    for entity_type in _ENTITY_QUERIES
}

_FILTER_COLUMNS: Mapping[str, frozenset[str]] = {
    "artifact": frozenset({"package_id", "sha256", "status"}),
    "dataset": frozenset({"sha256"}),
    "configuration": frozenset({"strategy_id", "configuration_hash"}),
    "experiment": frozenset({"research_id", "parent_experiment_id", "strategy_id", "status"}),
    "run": frozenset({"experiment_id", "configuration_id", "status", "result_hash"}),
    "metric_definition": frozenset({"metric_name", "definition_version", "owner_module"}),
    "metric": frozenset({"run_id", "metric_name", "definition_version", "definition_id"}),
    "evidence": frozenset({"experiment_id", "evidence_type", "status"}),
    "role": frozenset({"experiment_id", "configuration_id", "role"}),
    "package": frozenset(),
    "annotation": frozenset({"entity_type", "entity_id", "actor", "reason"}),
}

_APPEND_ONLY_TRIGGER_SPECS: Mapping[str, tuple[str, str]] = {
    "prevent_packages_update": ("UPDATE", "packages"),
    "prevent_packages_delete": ("DELETE", "packages"),
    "prevent_artifacts_update": ("UPDATE", "artifacts"),
    "prevent_artifacts_delete": ("DELETE", "artifacts"),
    "prevent_datasets_update": ("UPDATE", "datasets"),
    "prevent_datasets_delete": ("DELETE", "datasets"),
    "prevent_configurations_update": ("UPDATE", "configurations"),
    "prevent_configurations_delete": ("DELETE", "configurations"),
    "prevent_experiments_update": ("UPDATE", "experiments"),
    "prevent_experiments_delete": ("DELETE", "experiments"),
    "prevent_runs_update": ("UPDATE", "runs"),
    "prevent_runs_delete": ("DELETE", "runs"),
    "prevent_metric_definitions_update": ("UPDATE", "metric_definitions"),
    "prevent_metric_definitions_delete": ("DELETE", "metric_definitions"),
    "prevent_metrics_update": ("UPDATE", "metrics"),
    "prevent_metrics_delete": ("DELETE", "metrics"),
    "prevent_evidence_update": ("UPDATE", "evidence"),
    "prevent_evidence_delete": ("DELETE", "evidence"),
    "prevent_roles_update": ("UPDATE", "experiment_roles"),
    "prevent_roles_delete": ("DELETE", "experiment_roles"),
    "prevent_run_datasets_update": ("UPDATE", "run_datasets"),
    "prevent_run_datasets_delete": ("DELETE", "run_datasets"),
    "prevent_run_artifacts_update": ("UPDATE", "run_artifacts"),
    "prevent_run_artifacts_delete": ("DELETE", "run_artifacts"),
    "prevent_evidence_runs_update": ("UPDATE", "evidence_runs"),
    "prevent_evidence_runs_delete": ("DELETE", "evidence_runs"),
    "prevent_evidence_artifacts_update": ("UPDATE", "evidence_artifacts"),
    "prevent_evidence_artifacts_delete": ("DELETE", "evidence_artifacts"),
    "prevent_schema_migrations_update": ("UPDATE", "schema_migrations"),
    "prevent_schema_migrations_delete": ("DELETE", "schema_migrations"),
    "prevent_audit_update": ("UPDATE", "audit_log"),
    "prevent_audit_delete": ("DELETE", "audit_log"),
    "prevent_annotation_update": ("UPDATE", "registry_annotations"),
    "prevent_annotation_delete": ("DELETE", "registry_annotations"),
    "prevent_quarantine_update": ("UPDATE", "quarantine"),
    "prevent_quarantine_delete": ("DELETE", "quarantine"),
}

_ENTITY_RECORD_SPECS: Mapping[str, tuple[str, str | None, tuple[str, ...]]] = {
    "packages": ("package_id", "package_id", ()),
    "artifacts": ("artifact_id", "artifact_id", ("package_id", "sha256", "status")),
    "datasets": ("dataset_id", "dataset_id", ("sha256",)),
    "configurations": (
        "configuration_id",
        "configuration_id",
        ("strategy_id", "configuration_hash"),
    ),
    "experiments": (
        "experiment_id",
        "experiment_id",
        ("research_id", "parent_experiment_id", "strategy_id", "status"),
    ),
    "runs": (
        "run_id",
        "run_id",
        ("experiment_id", "configuration_id", "status", "result_hash"),
    ),
    "metric_definitions": (
        "definition_id",
        None,
        ("metric_name", "definition_version", "owner_module"),
    ),
    "metrics": (
        "metric_key",
        None,
        ("run_id", "metric_name", "definition_version"),
    ),
    "evidence": (
        "evidence_id",
        "evidence_id",
        ("experiment_id", "evidence_type", "status"),
    ),
    "experiment_roles": ("decision_id", None, ("experiment_id", "configuration_id", "role")),
}

_REGISTERED_REVISION_TABLES = (
    "packages",
    "artifacts",
    "datasets",
    "configurations",
    "experiments",
    "runs",
    "metric_definitions",
    "metrics",
    "evidence",
    "experiment_roles",
)

_SQL_IDENTIFIERS = frozenset(
    {
        "artifacts",
        "datasets",
        "configurations",
        "experiments",
        "runs",
        "metric_definitions",
        "metrics",
        "evidence",
        "experiment_roles",
        "packages",
        "registry_annotations",
        "artifact_id",
        "package_id",
        "sha256",
        "content_hash",
        "payload_json",
        "status",
        "registered_revision",
        "dataset_id",
        "configuration_id",
        "configuration_hash",
        "strategy_id",
        "experiment_id",
        "research_id",
        "parent_experiment_id",
        "run_id",
        "result_hash",
        "metric_key",
        "definition_id",
        "metric_name",
        "definition_version",
        "owner_module",
        "evidence_id",
        "evidence_type",
        "decision_id",
        "role",
        "entity_type",
        "entity_id",
        "actor",
        "reason",
        "annotation_id",
        "payload_hash",
    }
)


def _sql_identifier(value: str) -> str:
    """Return a SQL identifier only when it belongs to the fixed schema allowlist."""
    if value not in _SQL_IDENTIFIERS:
        raise IntegrityError(f"unsafe SQL identifier: {value!r}")
    return value


def _sql_identifier_list(values: Sequence[str]) -> str:
    return ",".join(_sql_identifier(value) for value in values)


def _decode_canonical_json_object(
    payload_raw: object, *, context: str
) -> tuple[dict[str, Any], bytes]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    try:
        if not isinstance(payload_raw, (bytes, bytearray, memoryview)):
            raise TypeError("stored payload must be bytes-like")
        payload_bytes = bytes(payload_raw)
        value = json.loads(
            payload_bytes.decode("utf-8"),
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{context} is not valid JSON; canonical JSON is required") from exc
    if not isinstance(value, dict):
        raise IntegrityError(f"{context} is not a JSON object")
    if canonical_json_bytes(value) != payload_bytes:
        raise IntegrityError(f"{context} is not canonically encoded")
    return value, payload_bytes


def _open_registry_guard(target: Path) -> tuple[int, int, os.stat_result, bool]:
    try:
        parent_fd = _open_directory_chain_nofollow(target.parent)
    except IntegrityError as exc:
        raise IntegrityError("registry parent cannot be opened safely") from exc
    try:
        opened_parent = os.fstat(parent_fd)
        current_parent = os.lstat(target.parent)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or not stat.S_ISDIR(current_parent.st_mode)
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
        ):
            raise IntegrityError("registry parent changed during safe open")
    except BaseException:
        os.close(parent_fd)
        raise
    descriptor = -1
    created = False
    try:
        flags = os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    target.name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityError("registry path must be a regular file")
        return parent_fd, descriptor, info, created
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)
        raise IntegrityError("registry path cannot be opened safely") from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)
        raise


def _same_registry_entry(parent_fd: int, target: Path, expected: os.stat_result) -> bool:
    try:
        relative = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        absolute = os.lstat(target)
    except OSError:
        return False
    return (
        stat.S_ISREG(relative.st_mode)
        and stat.S_ISREG(absolute.st_mode)
        and (relative.st_dev, relative.st_ino) == (expected.st_dev, expected.st_ino)
        and (absolute.st_dev, absolute.st_ino) == (expected.st_dev, expected.st_ino)
    )


def _validate_sqlite_sidecars(target: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if not os.path.lexists(sidecar):
            continue
        try:
            info = os.lstat(sidecar)
        except OSError as exc:
            raise IntegrityError("SQLite sidecar cannot be inspected safely") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise IntegrityError("SQLite sidecar must be a regular file")


def _cleanup_created_registry(
    parent_fd: int, target: Path, expected: os.stat_result, created: bool
) -> None:
    if not created or not _same_registry_entry(parent_fd, target, expected):
        return
    try:
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        # Do not mask the initialization failure; a same-inode empty/partial file may remain.
        pass
