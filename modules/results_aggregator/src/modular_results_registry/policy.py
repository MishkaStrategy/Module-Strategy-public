from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from .errors import ResourceLimitError, ValidationError
from .intake import ArtifactPackageManifest


def _positive_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value


def _positive_number(name: str, value: object) -> int | float:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be numeric")
    if not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{name} must be finite and positive")
    return value


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    max_package_size_bytes: int = 100 * 1024 * 1024 * 1024
    max_artifact_count: int = 10_000
    max_trades_per_import: int = 10_000_000
    max_concurrent_imports: int = 1
    max_disk_use_bytes: int = 2 * 1024 * 1024 * 1024 * 1024
    min_free_disk_bytes: int = 10 * 1024 * 1024 * 1024
    query_timeout_seconds: float = 30.0
    report_timeout_seconds: float = 300.0
    max_export_size_bytes: int = 10 * 1024 * 1024 * 1024
    max_pareto_candidates_without_chunking: int = 50_000
    max_chart_points: int = 100_000

    def __post_init__(self) -> None:
        for name in (
            "max_package_size_bytes",
            "max_artifact_count",
            "max_trades_per_import",
            "max_concurrent_imports",
            "max_disk_use_bytes",
            "min_free_disk_bytes",
            "max_export_size_bytes",
            "max_pareto_candidates_without_chunking",
            "max_chart_points",
        ):
            _positive_integer(name, getattr(self, name))
        _positive_number("query_timeout_seconds", self.query_timeout_seconds)
        _positive_number("report_timeout_seconds", self.report_timeout_seconds)


def _absolute_without_symlink_components(path: str | Path, *, label: str) -> Path:
    target = Path(path).absolute()
    components = tuple(reversed(target.parents)) + (target,)
    for component in components:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValidationError(f"{label} path cannot be inspected safely") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValidationError(f"{label} path cannot contain symlink components")
    return target


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if current == current.parent:
                raise ValidationError("no existing filesystem ancestor is available")
            current = current.parent
            continue
        except OSError as exc:
            raise ValidationError("filesystem ancestor cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValidationError("filesystem ancestor cannot be a symlink")
        return current


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    registry_bytes: int
    artifact_store_bytes: int
    free_disk_bytes: int
    concurrent_imports: int = 0

    def __post_init__(self) -> None:
        for name in (
            "registry_bytes",
            "artifact_store_bytes",
            "free_disk_bytes",
            "concurrent_imports",
        ):
            _nonnegative_integer(name, getattr(self, name))

    @property
    def total_persistent_bytes(self) -> int:
        return self.registry_bytes + self.artifact_store_bytes


def disk_snapshot(
    *,
    registry_path: str | Path,
    artifact_store_path: str | Path,
    concurrent_imports: int = 0,
) -> ResourceSnapshot:
    registry = _absolute_without_symlink_components(registry_path, label="registry")
    artifacts = _absolute_without_symlink_components(artifact_store_path, label="artifact store")
    try:
        registry_info = os.lstat(registry)
    except FileNotFoundError:
        registry_bytes = 0
        probe = _nearest_existing_ancestor(registry.parent)
    except OSError as exc:
        raise ValidationError("registry path cannot be inspected safely") from exc
    else:
        if not stat.S_ISREG(registry_info.st_mode):
            raise ValidationError("registry path must be a regular file")
        registry_bytes = registry_info.st_size
        probe = registry
    try:
        usage = os.statvfs(probe)
    except OSError as exc:
        raise ValidationError("filesystem capacity cannot be inspected") from exc
    free = usage.f_bavail * usage.f_frsize
    return ResourceSnapshot(
        registry_bytes=registry_bytes,
        artifact_store_bytes=_directory_regular_file_bytes(artifacts),
        free_disk_bytes=free,
        concurrent_imports=concurrent_imports,
    )


def _directory_regular_file_bytes(root: Path) -> int:
    root = _absolute_without_symlink_components(root, label="artifact store")
    try:
        root_info = os.lstat(root)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ValidationError("artifact store path cannot be inspected safely") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValidationError("artifact store path must be a real directory")
    total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in dirnames:
            if (current / name).is_symlink():
                raise ValidationError("artifact store cannot contain symlink directories")
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = current / name
            if path.is_symlink():
                raise ValidationError("artifact store cannot contain symlink files")
            if path.is_file():
                total += path.stat().st_size
    return total


class ResourceGuard:
    def __init__(self, policy: ResourcePolicy = ResourcePolicy()) -> None:
        self.policy = policy

    def check_import(
        self,
        manifest: ArtifactPackageManifest,
        snapshot: ResourceSnapshot,
        *,
        trade_count: int | None = None,
    ) -> None:
        artifact_count = len(manifest.artifact_inventory)
        package_bytes = sum(item.size_bytes for item in manifest.artifact_inventory)
        if artifact_count > self.policy.max_artifact_count:
            raise ResourceLimitError("artifact count exceeds resource policy")
        if package_bytes > self.policy.max_package_size_bytes:
            raise ResourceLimitError("package size exceeds resource policy")
        if trade_count is not None:
            _nonnegative_integer("trade_count", trade_count)
            if trade_count > self.policy.max_trades_per_import:
                raise ResourceLimitError("trade count exceeds resource policy")
        if snapshot.concurrent_imports >= self.policy.max_concurrent_imports:
            raise ResourceLimitError("concurrent import limit reached")
        projected = snapshot.total_persistent_bytes + package_bytes
        if projected > self.policy.max_disk_use_bytes:
            raise ResourceLimitError("projected disk use exceeds resource policy")
        required_free = self.policy.min_free_disk_bytes + package_bytes
        if snapshot.free_disk_bytes < required_free:
            raise ResourceLimitError("insufficient free disk for atomic import")

    def check_export(self, size_bytes: int) -> None:
        _nonnegative_integer("size_bytes", size_bytes)
        if size_bytes > self.policy.max_export_size_bytes:
            raise ResourceLimitError("export exceeds resource policy")

    def check_pareto(self, candidate_count: int, *, chunked: bool) -> None:
        _nonnegative_integer("candidate_count", candidate_count)
        if candidate_count > self.policy.max_pareto_candidates_without_chunking and not chunked:
            raise ResourceLimitError("large Pareto set requires chunking")

    def check_chart(self, point_count: int) -> None:
        _nonnegative_integer("point_count", point_count)
        if point_count > self.policy.max_chart_points:
            raise ResourceLimitError("chart point count exceeds resource policy")


class RetentionCategory(str, Enum):
    DERIVED_CACHE = "DERIVED_CACHE"
    GENERATED_REPORT = "GENERATED_REPORT"
    TEMPORARY_EXPORT = "TEMPORARY_EXPORT"
    IMMUTABLE_ARTIFACT = "IMMUTABLE_ARTIFACT"
    AUDIT_LOG = "AUDIT_LOG"


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    identifier: str
    category: RetentionCategory
    created_at: datetime
    referenced: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identifier, str)
            or not self.identifier
            or self.identifier != self.identifier.strip()
        ):
            raise ValidationError("retention identifier must be canonical non-empty text")
        if not isinstance(self.category, RetentionCategory):
            raise ValidationError("retention category must be a RetentionCategory")
        if not isinstance(self.referenced, bool):
            raise ValidationError("retention referenced must be boolean")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValidationError("retention timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    eligible_ids: tuple[str, ...]
    protected_ids: tuple[str, ...]
    cutoff_utc: datetime


def build_retention_plan(
    candidates: Iterable[RetentionCandidate],
    *,
    retention_days: int,
    now: datetime | None = None,
) -> RetentionPlan:
    _positive_integer("retention_days", retention_days)
    resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None or resolved_now.utcoffset() is None:
        raise ValidationError("retention now must be timezone-aware")
    cutoff = resolved_now.astimezone(timezone.utc) - timedelta(days=retention_days)
    eligible: list[str] = []
    protected: list[str] = []
    materialized = tuple(candidates)
    identifiers = [candidate.identifier for candidate in materialized]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError("retention candidate identifiers must be unique")
    deletable = {
        RetentionCategory.DERIVED_CACHE,
        RetentionCategory.GENERATED_REPORT,
        RetentionCategory.TEMPORARY_EXPORT,
    }
    for candidate in sorted(materialized, key=lambda item: item.identifier):
        old = candidate.created_at.astimezone(timezone.utc) < cutoff
        if candidate.category in deletable and old and not candidate.referenced:
            eligible.append(candidate.identifier)
        else:
            protected.append(candidate.identifier)
    return RetentionPlan(tuple(eligible), tuple(protected), cutoff)
