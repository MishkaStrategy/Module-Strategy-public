from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel


class ResourceUsage(ContractModel):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    percent: float = Field(ge=0.0, le=100.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> ResourceUsage:
        if self.used_bytes > self.total_bytes:
            raise ValueError("used_bytes must not exceed total_bytes")
        if self.available_bytes > self.total_bytes:
            raise ValueError("available_bytes must not exceed total_bytes")
        return self


class SystemResourcesSnapshot(ContractModel):
    collected_at: datetime
    cpu_percent: float = Field(ge=0.0, le=100.0)
    logical_cpu_count: int | None = Field(default=None, ge=1)
    memory: ResourceUsage
    disk: ResourceUsage
    uptime_seconds: float = Field(ge=0.0)
    process_count: int = Field(ge=1)
    control_process_rss_bytes: int = Field(ge=0)
    control_process_threads: int = Field(ge=1)

    @field_validator("collected_at")
    @classmethod
    def normalize_collected_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")
        return value.astimezone(UTC)


class SystemResourcesProvider(Protocol):
    def snapshot(self) -> SystemResourcesSnapshot: ...


class SystemResourcesControlService:
    """Read-only operational projection owned by the Control Plane process."""

    def __init__(self, provider: SystemResourcesProvider) -> None:
        if not callable(getattr(provider, "snapshot", None)):
            raise TypeError("provider must implement snapshot()")
        self._provider = provider

    def resources(self) -> SystemResourcesSnapshot:
        return self._provider.snapshot()
