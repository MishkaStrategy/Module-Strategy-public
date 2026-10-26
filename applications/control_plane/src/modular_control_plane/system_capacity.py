from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .service import Clock, utc_now

_MAX_POLICY_BYTES = 512 * 1024
_EXPECTED_FORECAST_OWNER = "system_resources_issue_370_pr_371"
_EXPECTED_UNKNOWN_MEANS: Literal["NO_DESTRUCTIVE_OR_CAPACITY_CLAIM"] = (
    "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM"
)
_STALE_PENDING_STATUSES = frozenset(
    {
        "UNKNOWN_PENDING_NEAR_BOUNDARY_ADMISSION_AND_RECOVERY_PROOF",
        "UNKNOWN_PENDING_ADMISSION_HEADROOM_INTERPRETATION",
        "UNKNOWN_PENDING_OWNER_ADMISSION_AND_RECOVERY_HEADROOM",
    }
)
_GIB = 1024**3


class RunnerCapacityRole(StrEnum):
    FAST = "fast"
    DOCKER = "docker"
    MACOS = "macos"
    BACKTESTER = "backtester"


class CapacityTierId(StrEnum):
    SYSTEM_ACCEPTANCE = "system_acceptance"
    MARKET_DATA_REGISTRY = "market_data_registry_storage"
    MARKET_DATA_APPLICATION = "market_data_application_control_plane_storage"
    RESEARCH_PLATFORM = "research_platform"
    CORE_PAPER_TRADING = "core_paper_trading_durable_state"


class CapacityMeasurementUnit(StrEnum):
    BYTES = "bytes"
    BYTES_PER_RUN = "bytes_per_run"


class CapacityMeasurement(ContractModel):
    metric: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9_]+$")
    value: float = Field(ge=0.0)
    unit: CapacityMeasurementUnit


class RunnerCapacityWatermark(ContractModel):
    role: RunnerCapacityRole
    high_water_free_bytes: int = Field(gt=0)
    critical_free_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> RunnerCapacityWatermark:
        if self.high_water_free_bytes <= self.critical_free_bytes:
            raise ValueError("high-water free bytes must exceed critical free bytes")
        return self


class CapacityPhysicalBoundary(ContractModel):
    status: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9_]+$")
    high_water_free_bytes: int | None = Field(default=None, ge=0)
    critical_free_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> CapacityPhysicalBoundary:
        if (self.high_water_free_bytes is None) != (self.critical_free_bytes is None):
            raise ValueError(
                "physical high-water and critical thresholds must be both known or both unknown"
            )
        if (
            self.high_water_free_bytes is not None
            and self.critical_free_bytes is not None
            and self.high_water_free_bytes <= self.critical_free_bytes
        ):
            raise ValueError("physical high-water free bytes must exceed critical free bytes")
        return self


class SystemCapacityTier(ContractModel):
    tier_id: CapacityTierId
    kind: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")
    status: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9_]+$")
    physical_free_disk: CapacityPhysicalBoundary
    deletion_authority: Literal["NONE"] = "NONE"
    next_publication_admission: bool = False
    recovery_reserve_bytes: int | None = Field(default=None, ge=0)
    owner_hard_limit_bytes: int | None = Field(default=None, ge=0)
    owner_payload_limit_bytes: int | None = Field(default=None, ge=0)
    measurements_aggregatable: Literal[False] = False
    measurements: tuple[CapacityMeasurement, ...] = Field(default=(), max_length=16)


class SystemCapacitySnapshot(ContractModel):
    observed_at: datetime
    source_schema_version: Literal[2]
    source_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_status: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9_]+$")
    unknown_means: Literal["NO_DESTRUCTIVE_OR_CAPACITY_CLAIM"]
    global_local_disk_watermark_available: Literal[False] = False
    runner_watermarks: tuple[RunnerCapacityWatermark, ...] = Field(min_length=4, max_length=4)
    tiers: tuple[SystemCapacityTier, ...] = Field(min_length=5, max_length=5)

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_unique_projection(self) -> SystemCapacitySnapshot:
        if len({item.role for item in self.runner_watermarks}) != len(self.runner_watermarks):
            raise ValueError("runner watermarks must be unique by role")
        if len({item.tier_id for item in self.tiers}) != len(self.tiers):
            raise ValueError("capacity tiers must be unique by tier_id")
        return self


class SystemCapacityProvider(Protocol):
    def snapshot(self) -> SystemCapacitySnapshot: ...


class SystemCapacityControlService:
    """Read-only projection of the canonical GAP-013 storage-pressure policy."""

    def __init__(self, provider: SystemCapacityProvider) -> None:
        if not callable(getattr(provider, "snapshot", None)):
            raise TypeError("provider must implement snapshot()")
        self._provider = provider

    def capacity(self) -> SystemCapacitySnapshot:
        return self._provider.snapshot()


class JsonSystemCapacityProvider:
    """Load one trusted server-side canonical policy file and project only allowlisted fields."""

    def __init__(self, path: Path, *, clock: Clock = utc_now) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be pathlib.Path")
        if not path.is_absolute():
            raise ValueError("capacity policy path must be absolute")
        self._path = path
        self._clock = clock

    def snapshot(self) -> SystemCapacitySnapshot:
        raw = _read_policy_bytes(self._path)
        policy = _mapping(json.loads(raw.decode("utf-8")), "policy document")
        serialized_strings = _all_strings(policy)
        if serialized_strings & _STALE_PENDING_STATUSES:
            raise ValueError("capacity policy contains stale pending owner-proof status")
        if _integer(policy.get("schema_version"), "schema_version") != 2:
            raise ValueError("unsupported capacity policy schema_version")
        policy_status = _status(policy.get("status"), "status")
        policy_rules = _mapping(policy.get("policy"), "policy")
        if policy_rules.get("forecast_owner") != _EXPECTED_FORECAST_OWNER:
            raise ValueError("unexpected capacity forecast owner")
        if policy_rules.get("no_duplicate_monitor") is not True:
            raise ValueError("capacity policy must forbid duplicate monitors")
        if policy_rules.get("unknown_means") != _EXPECTED_UNKNOWN_MEANS:
            raise ValueError("capacity policy unknown semantics changed")
        adoption = _mapping(policy.get("adoption_rule"), "adoption_rule")
        if adoption.get("unknown_is_not_deletion_permission") is not True:
            raise ValueError("unknown capacity state must not grant deletion permission")
        tiers = _mapping(policy.get("tiers"), "tiers")
        expected_tiers = {"self_hosted_runner", *(item.value for item in CapacityTierId)}
        if not expected_tiers.issubset(tiers):
            raise ValueError("capacity policy is missing required tiers")
        runner_watermarks = _runner_watermarks(
            _mapping(tiers["self_hosted_runner"], "self_hosted_runner")
        )
        projected = (
            _system_acceptance(
                _mapping(
                    tiers[CapacityTierId.SYSTEM_ACCEPTANCE.value],
                    CapacityTierId.SYSTEM_ACCEPTANCE.value,
                )
            ),
            _market_data_registry(
                _mapping(
                    tiers[CapacityTierId.MARKET_DATA_REGISTRY.value],
                    CapacityTierId.MARKET_DATA_REGISTRY.value,
                )
            ),
            _market_data_application(
                _mapping(
                    tiers[CapacityTierId.MARKET_DATA_APPLICATION.value],
                    CapacityTierId.MARKET_DATA_APPLICATION.value,
                )
            ),
            _research_platform(
                _mapping(
                    tiers[CapacityTierId.RESEARCH_PLATFORM.value],
                    CapacityTierId.RESEARCH_PLATFORM.value,
                )
            ),
            _core_paper_trading(
                _mapping(
                    tiers[CapacityTierId.CORE_PAPER_TRADING.value],
                    CapacityTierId.CORE_PAPER_TRADING.value,
                )
            ),
        )
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return SystemCapacitySnapshot(
            observed_at=observed_at,
            source_schema_version=2,
            source_digest_sha256=sha256(raw).hexdigest(),
            policy_status=policy_status,
            unknown_means=_EXPECTED_UNKNOWN_MEANS,
            runner_watermarks=runner_watermarks,
            tiers=projected,
        )


def system_capacity_service_from_environment(
    *, clock: Clock = utc_now
) -> SystemCapacityControlService | None:
    value = os.environ.get("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH")
    if value is None or not value.strip():
        return None
    if "\x00" in value or len(value) > 4096:
        raise ValueError("capacity policy path configuration is invalid")
    provider = JsonSystemCapacityProvider(Path(value), clock=clock)
    return SystemCapacityControlService(provider)


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


def _runner_watermarks(tier: dict[str, Any]) -> tuple[RunnerCapacityWatermark, ...]:
    if _status(tier.get("status"), "self_hosted_runner.status") != "ACCEPTED":
        raise ValueError("runner capacity policy is not accepted")
    capabilities = _mapping(tier.get("capabilities"), "self_hosted_runner.capabilities")
    if set(capabilities) != {item.value for item in RunnerCapacityRole}:
        raise ValueError("runner capacity roles changed unexpectedly")
    result: list[RunnerCapacityWatermark] = []
    for role in RunnerCapacityRole:
        values = _mapping(capabilities[role.value], f"runner.{role.value}")
        high_gib = _integer(
            values.get("high_water_free_gib"), f"runner.{role.value}.high_water_free_gib"
        )
        critical_gib = _integer(
            values.get("critical_free_gib"), f"runner.{role.value}.critical_free_gib"
        )
        result.append(
            RunnerCapacityWatermark(
                role=role,
                high_water_free_bytes=high_gib * _GIB,
                critical_free_bytes=critical_gib * _GIB,
            )
        )
    return tuple(result)


def _system_acceptance(tier: dict[str, Any]) -> SystemCapacityTier:
    bounds = _mapping(tier.get("hard_owner_bounds"), "system_acceptance.hard_owner_bounds")
    measurement = _mapping(
        tier.get("accepted_measurement"), "system_acceptance.accepted_measurement"
    )
    return _tier(
        tier_id=CapacityTierId.SYSTEM_ACCEPTANCE,
        tier=tier,
        physical=_mapping(tier.get("physical_free_disk"), "system_acceptance.physical_free_disk"),
        owner_hard_limit_bytes=_integer(
            bounds.get("critical_database_bytes"), "critical_database_bytes"
        ),
        owner_payload_limit_bytes=_integer(
            bounds.get("max_encoded_run_payload_bytes"), "max_encoded_run_payload_bytes"
        ),
        measurements=(
            _measurement(
                "live_total_at_1000_runs", measurement.get("live_total_bytes_at_1000_runs")
            ),
            _measurement(
                "settled_database_at_1000_runs",
                measurement.get("settled_database_bytes_at_1000_runs"),
            ),
            _measurement(
                "growth_per_run_at_1000_runs",
                measurement.get("growth_bytes_per_run_at_1000_runs"),
                unit=CapacityMeasurementUnit.BYTES_PER_RUN,
            ),
        ),
    )


def _market_data_registry(tier: dict[str, Any]) -> SystemCapacityTier:
    measurement = _mapping(
        tier.get("accepted_measurement"), "market_data_registry.accepted_measurement"
    )
    admission = _mapping(
        tier.get("accepted_publication_admission"),
        "market_data_registry.accepted_publication_admission",
    )
    if admission.get("reject_before_first_version_write") is not True:
        raise ValueError("Market Data publication admission no longer rejects before first write")
    return _tier(
        tier_id=CapacityTierId.MARKET_DATA_REGISTRY,
        tier=tier,
        physical=_mapping(
            tier.get("physical_free_disk"), "market_data_registry.physical_free_disk"
        ),
        next_publication_admission=True,
        recovery_reserve_bytes=_integer(
            admission.get("registry_transaction_recovery_reserve_bytes"),
            "registry_transaction_recovery_reserve_bytes",
        ),
        measurements=(
            _measurement(
                "representative_settled_publication",
                measurement.get("representative_settled_publication_bytes"),
            ),
            _measurement(
                "representative_largest_atomic_payload",
                measurement.get("representative_largest_atomic_payload_bytes"),
            ),
            _measurement(
                "representative_manifest", measurement.get("representative_manifest_bytes")
            ),
        ),
    )


def _market_data_application(tier: dict[str, Any]) -> SystemCapacityTier:
    measurement = _mapping(
        tier.get("accepted_measurement"), "market_data_application.accepted_measurement"
    )
    if measurement.get("do_not_sum_component_maxima_into_synthetic_total") is not True:
        raise ValueError("Market Data component maxima must remain non-aggregatable")
    operational = _mapping(
        measurement.get("operational_jobs_scale_1000"), "operational_jobs_scale_1000"
    )
    security = _mapping(
        measurement.get("security_token_audit_scale_1000"), "security_token_audit_scale_1000"
    )
    shared = _mapping(
        measurement.get("shared_jobs_sqlite_scale_1000"), "shared_jobs_sqlite_scale_1000"
    )
    return _tier(
        tier_id=CapacityTierId.MARKET_DATA_APPLICATION,
        tier=tier,
        physical=_mapping(
            tier.get("physical_free_disk"), "market_data_application.physical_free_disk"
        ),
        measurements=(
            _measurement(
                "operational_live_total_scale_1000",
                operational.get("max_observed_live_total_bytes"),
            ),
            _measurement(
                "security_live_total_scale_1000", security.get("max_observed_live_total_bytes")
            ),
            _measurement(
                "shared_jobs_live_total_scale_1000", shared.get("max_observed_live_total_bytes")
            ),
        ),
    )


def _research_platform(tier: dict[str, Any]) -> SystemCapacityTier:
    measurement = _mapping(
        tier.get("accepted_measurement"), "research_platform.accepted_measurement"
    )
    return _tier(
        tier_id=CapacityTierId.RESEARCH_PLATFORM,
        tier=tier,
        physical=_mapping(tier.get("physical_free_disk"), "research_platform.physical_free_disk"),
        measurements=(
            _measurement(
                "max_scheduler_temporary", measurement.get("max_scheduler_temporary_bytes")
            ),
            _measurement("max_results_registry", measurement.get("max_results_registry_bytes")),
            _measurement(
                "max_artifact_per_experiment",
                measurement.get("max_immutable_artifact_bytes_per_experiment"),
            ),
        ),
    )


def _core_paper_trading(tier: dict[str, Any]) -> SystemCapacityTier:
    physical = {
        "status": tier.get("status"),
        "high_water_free_bytes": tier.get("high_water_free_bytes"),
        "critical_free_bytes": tier.get("critical_free_bytes"),
    }
    return _tier(
        tier_id=CapacityTierId.CORE_PAPER_TRADING,
        tier=tier,
        physical=physical,
        measurements=(),
    )


def _tier(
    *,
    tier_id: CapacityTierId,
    tier: dict[str, Any],
    physical: dict[str, Any],
    measurements: tuple[CapacityMeasurement, ...],
    next_publication_admission: bool = False,
    recovery_reserve_bytes: int | None = None,
    owner_hard_limit_bytes: int | None = None,
    owner_payload_limit_bytes: int | None = None,
) -> SystemCapacityTier:
    if tier.get("deletion_authority") != "NONE":
        raise ValueError(f"{tier_id.value} deletion authority must remain NONE")
    return SystemCapacityTier(
        tier_id=tier_id,
        kind=_kind(tier.get("kind"), f"{tier_id.value}.kind"),
        status=_status(tier.get("status"), f"{tier_id.value}.status"),
        physical_free_disk=_physical_boundary(physical, tier_id=tier_id),
        deletion_authority="NONE",
        next_publication_admission=next_publication_admission,
        recovery_reserve_bytes=recovery_reserve_bytes,
        owner_hard_limit_bytes=owner_hard_limit_bytes,
        owner_payload_limit_bytes=owner_payload_limit_bytes,
        measurements=measurements,
    )


def _physical_boundary(
    value: dict[str, Any], *, tier_id: CapacityTierId
) -> CapacityPhysicalBoundary:
    high = _optional_integer(
        value.get("high_water_free_bytes"), f"{tier_id.value}.high_water_free_bytes"
    )
    critical = _optional_integer(
        value.get("critical_free_bytes"), f"{tier_id.value}.critical_free_bytes"
    )
    return CapacityPhysicalBoundary(
        status=_status(value.get("status"), f"{tier_id.value}.physical_status"),
        high_water_free_bytes=high,
        critical_free_bytes=critical,
    )


def _measurement(
    metric: str,
    value: object,
    *,
    unit: CapacityMeasurementUnit = CapacityMeasurementUnit.BYTES,
) -> CapacityMeasurement:
    return CapacityMeasurement(metric=metric, value=_number(value, metric), unit=unit)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


def _status(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{name} must be a bounded status string")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value):
        raise ValueError(f"{name} contains unsupported status characters")
    return value


def _kind(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{name} must be a bounded kind string")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value):
        raise ValueError(f"{name} contains unsupported kind characters")
    return value


def _all_strings(value: object) -> set[str]:
    result: set[str] = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            result.add(current)
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return result


__all__ = [
    "CapacityMeasurement",
    "CapacityMeasurementUnit",
    "CapacityPhysicalBoundary",
    "CapacityTierId",
    "JsonSystemCapacityProvider",
    "RunnerCapacityRole",
    "RunnerCapacityWatermark",
    "SystemCapacityControlService",
    "SystemCapacityProvider",
    "SystemCapacitySnapshot",
    "SystemCapacityTier",
    "system_capacity_service_from_environment",
]
