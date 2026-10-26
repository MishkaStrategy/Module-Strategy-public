from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .artifact_store import FileLimits, read_verified_bytes
from .canonical import canonical_json_bytes, canonical_sha256, deep_freeze, deterministic_id
from .errors import IntegrityError, ValidationError
from .safe_io import atomic_write_bytes, ensure_safe_parent, path_lexists


@dataclass(frozen=True, slots=True)
class DerivedCacheKey:
    registry_revision: int
    query_specification: Mapping[str, Any] = field(default_factory=dict)
    metric_versions: tuple[str, ...] = ()
    slice_definition: Mapping[str, Any] = field(default_factory=dict)
    comparison_definition: Mapping[str, Any] = field(default_factory=dict)
    ranking_configuration: Mapping[str, Any] = field(default_factory=dict)
    report_schema: str = "none"
    key_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.registry_revision, bool)
            or not isinstance(self.registry_revision, int)
            or self.registry_revision < 0
        ):
            raise ValidationError("registry_revision must be a non-negative integer")
        if not isinstance(self.report_schema, str) or not self.report_schema.strip():
            raise ValidationError("report_schema must be non-blank")
        if self.report_schema != self.report_schema.strip():
            raise ValidationError("report_schema must be canonical text")
        for name in (
            "query_specification",
            "slice_definition",
            "comparison_definition",
            "ranking_configuration",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValidationError(f"{name} must be a mapping")
            object.__setattr__(self, name, deep_freeze(value))
        if not isinstance(self.metric_versions, tuple):
            raise ValidationError("metric_versions must be a tuple")
        if any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in self.metric_versions
        ):
            raise ValidationError("metric_versions must contain canonical non-empty strings")
        if len(set(self.metric_versions)) != len(self.metric_versions):
            raise ValidationError("metric_versions must contain unique values")
        object.__setattr__(self, "metric_versions", tuple(sorted(self.metric_versions)))
        canonical = self.to_mapping(include_id=False)
        object.__setattr__(self, "key_id", deterministic_id("cache", canonical))

    def to_mapping(self, *, include_id: bool = True) -> Mapping[str, Any]:
        value: dict[str, Any] = {
            "registry_revision": self.registry_revision,
            "query_specification": self.query_specification,
            "metric_versions": self.metric_versions,
            "slice_definition": self.slice_definition,
            "comparison_definition": self.comparison_definition,
            "ranking_configuration": self.ranking_configuration,
            "report_schema": self.report_schema,
        }
        if include_id:
            value["key_id"] = self.key_id
        return MappingProxyType(value)


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    corruptions: int
    writes: int


class DerivedViewCache:
    """Filesystem cache for reproducible derived views; never a source of truth."""

    def __init__(self, directory: str | Path, *, max_entry_bytes: int = 64 * 1024 * 1024) -> None:
        if max_entry_bytes < 1024:
            raise ValidationError("max_entry_bytes must be at least 1 KiB")
        probe = ensure_safe_parent(Path(directory) / ".cache-probe")
        self.directory = probe.parent
        self.max_entry_bytes = max_entry_bytes
        self._hits = 0
        self._misses = 0
        self._corruptions = 0
        self._writes = 0

    def _path(self, key: DerivedCacheKey) -> Path:
        return self.directory / f"{key.key_id}.json"

    def put(self, key: DerivedCacheKey, payload: Mapping[str, Any]) -> str:
        if not isinstance(key, DerivedCacheKey):
            raise TypeError("cache key must be a DerivedCacheKey")
        if not isinstance(payload, Mapping):
            raise TypeError("cache payload must be a mapping")
        frozen = deep_freeze(payload)
        payload_hash = canonical_sha256(frozen)
        envelope = {
            "schema_version": "derived-cache.v1",
            "key": key.to_mapping(),
            "key_hash": canonical_sha256(key.to_mapping()),
            "payload": frozen,
            "payload_hash": payload_hash,
        }
        content = canonical_json_bytes(envelope)
        if len(content) > self.max_entry_bytes:
            raise ValidationError("cache entry exceeds max_entry_bytes")
        atomic_write_bytes(self._path(key), content)
        self._writes += 1
        return payload_hash

    def get(self, key: DerivedCacheKey) -> Mapping[str, Any] | None:
        path = self._path(key)
        if not path_lexists(path):
            self._misses += 1
            return None
        try:
            _, content = read_verified_bytes(
                path,
                limits=FileLimits(max_file_bytes=self.max_entry_bytes, chunk_bytes=64 * 1024),
            )

            def reject_constant(value: str) -> None:
                raise ValueError(f"non-finite JSON constant is forbidden: {value}")

            value = json.loads(content, parse_constant=reject_constant)
            if not isinstance(value, dict):
                raise IntegrityError("cache envelope must be an object")
            if set(value) != {"schema_version", "key", "key_hash", "payload", "payload_hash"}:
                raise IntegrityError("cache envelope fields are invalid")
            if canonical_json_bytes(value) != content:
                raise IntegrityError("cache envelope is not canonically encoded")
            if value.get("schema_version") != "derived-cache.v1":
                raise IntegrityError("unsupported cache schema")
            expected_key_hash = canonical_sha256(key.to_mapping())
            embedded_key = value.get("key")
            if (
                not isinstance(embedded_key, dict)
                or value.get("key_hash") != expected_key_hash
                or canonical_sha256(embedded_key) != expected_key_hash
            ):
                raise IntegrityError("cache key mismatch")
            payload = value.get("payload")
            if not isinstance(payload, dict):
                raise IntegrityError("cache payload must be an object")
            if value.get("payload_hash") != canonical_sha256(payload):
                raise IntegrityError("cache payload hash mismatch")
        except (
            IntegrityError,
            ValidationError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
            OSError,
        ):
            self._corruptions += 1
            self._safe_invalidate(path)
            self._misses += 1
            return None
        self._hits += 1
        return deep_freeze(payload)

    def invalidate(self, key: DerivedCacheKey) -> bool:
        return self._safe_invalidate(self._path(key))

    def _safe_invalidate(self, path: Path) -> bool:
        if not path_lexists(path):
            return False
        try:
            info = path.lstat()
        except OSError as exc:
            raise IntegrityError("cache entry cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise IntegrityError("cache entry cannot be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityError("cache entry must be a regular file")
        path.unlink()
        return True

    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            corruptions=self._corruptions,
            writes=self._writes,
        )
