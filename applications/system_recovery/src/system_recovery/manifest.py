from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RecoveryManifestError(ValueError):
    """Raised when recovery metadata is unsafe, ambiguous, or non-canonical."""


class RecoveryClass(StrEnum):
    RECONSTRUCTABLE_IMMUTABLE = "reconstructable_immutable_source"
    PROTECTED_IMMUTABLE_EVIDENCE = "protected_immutable_evidence"
    REQUIRED_CONFIGURATION = "required_configuration"
    MUTABLE_RUNTIME_STATE = "mutable_runtime_state"
    EPHEMERAL_CACHE = "ephemeral_cache"
    SECRET_PRIVATE = "secret_private"


class SnapshotKind(StrEnum):
    REGULAR_FILE = "regular_file"
    SQLITE = "sqlite"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    logical_id: str
    owner: str
    recovery_class: RecoveryClass
    snapshot_kind: SnapshotKind
    source: str
    restore_target: str | None
    sha256: str | None = None
    byte_size: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.logical_id, "logical_id")
        _identifier(self.owner, "owner")
        _relative_path(self.source, "source")
        if self.restore_target is not None:
            _relative_path(self.restore_target, "restore_target")
        if self.snapshot_kind is SnapshotKind.REFERENCE:
            if self.recovery_class is RecoveryClass.MUTABLE_RUNTIME_STATE:
                raise RecoveryManifestError("mutable runtime state cannot be reference-only")
            if self.sha256 is not None or self.byte_size is not None:
                raise RecoveryManifestError("reference entries must not declare snapshot bytes")
        else:
            if self.recovery_class in {
                RecoveryClass.EPHEMERAL_CACHE,
                RecoveryClass.SECRET_PRIVATE,
                RecoveryClass.RECONSTRUCTABLE_IMMUTABLE,
            }:
                raise RecoveryManifestError(
                    "non-restored classes cannot be embedded in a backup snapshot"
                )
            if self.restore_target is None:
                raise RecoveryManifestError("snapshot entries require restore_target")
            if self.sha256 is None or _SHA256.fullmatch(self.sha256) is None:
                raise RecoveryManifestError("snapshot entries require lowercase SHA-256")
            if (
                not isinstance(self.byte_size, int)
                or isinstance(self.byte_size, bool)
                or self.byte_size < 0
            ):
                raise RecoveryManifestError("snapshot entries require non-negative byte_size")

    def to_payload(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "owner": self.owner,
            "recovery_class": self.recovery_class.value,
            "snapshot_kind": self.snapshot_kind.value,
            "source": self.source,
            "restore_target": self.restore_target,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True, slots=True)
class RecoveryManifest:
    source_commit: str
    platform_release_id: str
    entries: tuple[RecoveryEntry, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RecoveryManifestError("unsupported recovery manifest schema")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise RecoveryManifestError("source_commit must be a full lowercase Git SHA")
        _identifier(self.platform_release_id, "platform_release_id")
        if not self.entries:
            raise RecoveryManifestError("recovery manifest entries must not be empty")
        logical_ids = [item.logical_id for item in self.entries]
        if logical_ids != sorted(logical_ids):
            raise RecoveryManifestError("recovery entries must be sorted by logical_id")
        if len(logical_ids) != len(set(logical_ids)):
            raise RecoveryManifestError("recovery logical_id values must be unique")
        targets = [
            item.restore_target
            for item in self.entries
            if item.restore_target is not None and item.snapshot_kind is not SnapshotKind.REFERENCE
        ]
        if len(targets) != len(set(targets)):
            raise RecoveryManifestError("recovery restore_target values must be unique")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_commit": self.source_commit,
            "platform_release_id": self.platform_release_id,
            "entries": [item.to_payload() for item in self.entries],
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def manifest_from_payload(payload: Mapping[str, object]) -> RecoveryManifest:
    expected = {"schema_version", "source_commit", "platform_release_id", "entries"}
    if set(payload) != expected:
        raise RecoveryManifestError("recovery manifest fields mismatch")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise RecoveryManifestError("entries must be an array")
    entries: list[RecoveryEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise RecoveryManifestError("entry must be an object")
        expected_entry = {
            "logical_id",
            "owner",
            "recovery_class",
            "snapshot_kind",
            "source",
            "restore_target",
            "sha256",
            "byte_size",
        }
        if set(raw) != expected_entry:
            raise RecoveryManifestError("recovery entry fields mismatch")
        try:
            recovery_class = RecoveryClass(_string(raw.get("recovery_class"), "recovery_class"))
            snapshot_kind = SnapshotKind(_string(raw.get("snapshot_kind"), "snapshot_kind"))
        except ValueError as exc:
            raise RecoveryManifestError("unknown recovery classification or snapshot kind") from exc
        restore_target = raw.get("restore_target")
        if restore_target is not None and not isinstance(restore_target, str):
            raise RecoveryManifestError("restore_target must be a string or null")
        sha256 = raw.get("sha256")
        if sha256 is not None and not isinstance(sha256, str):
            raise RecoveryManifestError("sha256 must be a string or null")
        byte_size = raw.get("byte_size")
        if byte_size is not None and (
            not isinstance(byte_size, int) or isinstance(byte_size, bool)
        ):
            raise RecoveryManifestError("byte_size must be an integer or null")
        entries.append(
            RecoveryEntry(
                logical_id=_string(raw.get("logical_id"), "logical_id"),
                owner=_string(raw.get("owner"), "owner"),
                recovery_class=recovery_class,
                snapshot_kind=snapshot_kind,
                source=_string(raw.get("source"), "source"),
                restore_target=restore_target,
                sha256=sha256,
                byte_size=byte_size,
            )
        )
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise RecoveryManifestError("schema_version must be an integer")
    return RecoveryManifest(
        schema_version=schema_version,
        source_commit=_string(payload.get("source_commit"), "source_commit"),
        platform_release_id=_string(payload.get("platform_release_id"), "platform_release_id"),
        entries=tuple(entries),
    )


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise RecoveryManifestError(f"{name} must be a canonical identifier")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RecoveryManifestError(f"{name} must be a string")
    return value


def _relative_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RecoveryManifestError(f"{name} must be a canonical POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RecoveryManifestError(f"{name} must be a canonical POSIX relative path")
    if str(path) != value:
        raise RecoveryManifestError(f"{name} must be a canonical POSIX relative path")
    return value
