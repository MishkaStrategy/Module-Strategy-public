from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import Field, field_validator

from .contracts import ContractModel
from .service import Clock, utc_now

_MAX_POLICY_BYTES = 512 * 1024
_EXPECTED_FORECAST_OWNER = "system_resources_issue_370_pr_371"
_EXPECTED_UNKNOWN_MEANS = "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM"


class ExternalArtifactCapacitySnapshot(ContractModel):
    observed_at: datetime
    source_schema_version: Literal[2]
    source_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["external_quota_and_maintenance_bound"]
    status: Literal["PARTIAL_EXTERNAL_QUOTA"]
    proactive_usage_status: Literal["UNKNOWN_NO_ACCEPTED_USAGE_SOURCE"]
    critical_signal: Literal["CREATE_ARTIFACT_QUOTA_FAILURE"]
    maintenance_max_count: int = Field(gt=0)
    maintenance_max_bytes: int = Field(gt=0)
    rate_limit_reserve_requests: int = Field(gt=0)
    local_disk_watermark_applicable: Literal[False] = False
    operator_cleanup_available: Literal[False] = False

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)


class ExternalArtifactCapacityProvider(Protocol):
    def snapshot(self) -> ExternalArtifactCapacitySnapshot: ...


class ExternalArtifactCapacityControlService:
    def __init__(self, provider: ExternalArtifactCapacityProvider) -> None:
        if not callable(getattr(provider, "snapshot", None)):
            raise TypeError("provider must implement snapshot()")
        self._provider = provider

    def capacity(self) -> ExternalArtifactCapacitySnapshot:
        return self._provider.snapshot()


class JsonExternalArtifactCapacityProvider:
    """Project the external GitHub Actions artifact quota tier without cleanup authority."""

    def __init__(self, path: Path, *, clock: Clock = utc_now) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be pathlib.Path")
        if not path.is_absolute():
            raise ValueError("capacity policy path must be absolute")
        self._path = path
        self._clock = clock

    def snapshot(self) -> ExternalArtifactCapacitySnapshot:
        raw = _read_policy_bytes(self._path)
        policy = _mapping(json.loads(raw.decode("utf-8")), "policy document")
        if _integer(policy.get("schema_version"), "schema_version") != 2:
            raise ValueError("unsupported capacity policy schema_version")
        rules = _mapping(policy.get("policy"), "policy")
        if rules.get("forecast_owner") != _EXPECTED_FORECAST_OWNER:
            raise ValueError("unexpected capacity forecast owner")
        if rules.get("no_duplicate_monitor") is not True:
            raise ValueError("capacity policy must forbid duplicate monitors")
        if rules.get("unknown_means") != _EXPECTED_UNKNOWN_MEANS:
            raise ValueError("capacity policy unknown semantics changed")
        adoption = _mapping(policy.get("adoption_rule"), "adoption_rule")
        if adoption.get("unknown_is_not_deletion_permission") is not True:
            raise ValueError("unknown capacity state must not grant deletion permission")

        tiers = _mapping(policy.get("tiers"), "tiers")
        tier = _mapping(tiers.get("github_actions_artifacts"), "github_actions_artifacts")
        if tier.get("kind") != "external_quota_and_maintenance_bound":
            raise ValueError("unexpected external artifact capacity kind")
        if tier.get("status") != "PARTIAL_EXTERNAL_QUOTA":
            raise ValueError("unexpected external artifact capacity status")
        quota = _mapping(tier.get("quota"), "github_actions_artifacts.quota")
        if quota.get("high_water") is not None:
            raise ValueError("external artifact quota high-water must remain unknown")
        if quota.get("critical_signal") != "CREATE_ARTIFACT_QUOTA_FAILURE":
            raise ValueError("unexpected external artifact quota critical signal")
        if quota.get("proactive_usage_status") != "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE":
            raise ValueError("external artifact proactive usage must remain unknown")
        maintenance = _mapping(
            tier.get("failed_cancelled_diagnostic_maintenance"),
            "github_actions_artifacts.failed_cancelled_diagnostic_maintenance",
        )
        if maintenance.get("bound_type") != "MAINTENANCE_SAFETY_BOUND_NOT_QUOTA_WATERMARK":
            raise ValueError("artifact maintenance bound cannot be treated as quota watermark")
        if maintenance.get("requires_exact_batch_sha256") is not True:
            raise ValueError("artifact maintenance must retain exact batch digest requirement")

        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return ExternalArtifactCapacitySnapshot(
            observed_at=observed_at,
            source_schema_version=2,
            source_digest_sha256=sha256(raw).hexdigest(),
            kind="external_quota_and_maintenance_bound",
            status="PARTIAL_EXTERNAL_QUOTA",
            proactive_usage_status="UNKNOWN_NO_ACCEPTED_USAGE_SOURCE",
            critical_signal="CREATE_ARTIFACT_QUOTA_FAILURE",
            maintenance_max_count=_positive_integer(
                maintenance.get("operational_max_count"), "operational_max_count"
            ),
            maintenance_max_bytes=_positive_integer(
                maintenance.get("max_bytes"), "maintenance.max_bytes"
            ),
            rate_limit_reserve_requests=_positive_integer(
                maintenance.get("core_rate_limit_reserve_requests"),
                "core_rate_limit_reserve_requests",
            ),
        )


def external_artifact_capacity_service_from_environment(
    *, clock: Clock = utc_now
) -> ExternalArtifactCapacityControlService | None:
    value = os.environ.get("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH")
    if value is None or not value.strip():
        return None
    if "\x00" in value or len(value) > 4096:
        raise ValueError("capacity policy path configuration is invalid")
    provider = JsonExternalArtifactCapacityProvider(Path(value), clock=clock)
    return ExternalArtifactCapacityControlService(provider)


def _read_policy_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("capacity policy must be a regular file")
        if metadata.st_size > _MAX_POLICY_BYTES:
            raise ValueError("capacity policy exceeds maximum size")
        chunks: list[bytes] = []
        remaining = _MAX_POLICY_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > _MAX_POLICY_BYTES:
            raise ValueError("capacity policy exceeds maximum size")
        return value
    finally:
        os.close(descriptor)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_integer(value: object, name: str) -> int:
    result = _integer(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


__all__ = [
    "ExternalArtifactCapacityControlService",
    "ExternalArtifactCapacityProvider",
    "ExternalArtifactCapacitySnapshot",
    "JsonExternalArtifactCapacityProvider",
    "external_artifact_capacity_service_from_environment",
]
