from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApplicationSettings(BaseModel):
    """Validated composition settings shared by CLI and HTTP transports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    storage_root: Path = Path("./data")
    registry_filename: str = "registry.sqlite3"
    jobs_filename: str = "jobs.sqlite3"
    network_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    network_retries: int = Field(default=6, ge=1, le=32)
    worker_count: int = Field(default=2, ge=1, le=32)
    instance_id: str = Field(default_factory=lambda: f"mdi_worker_{uuid4().hex[:20]}")
    lease_ttl_seconds: float = Field(default=30.0, ge=2.0, le=3600.0)
    lease_heartbeat_seconds: float = Field(default=5.0, ge=0.25, le=600.0)
    repair_confirmation_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    artifact_max_bytes: int = Field(
        default=8 * 1024 * 1024 * 1024,
        ge=1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    artifact_retention_count: int = Field(default=25, ge=1, le=10_000)
    artifact_retention_total_bytes: int = Field(
        default=32 * 1024 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024 * 1024 * 1024,
    )
    include_fake_adapter: bool = False

    @field_validator("storage_root")
    @classmethod
    def normalize_storage_root(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("registry_filename", "jobs_filename")
    @classmethod
    def validate_database_filename(cls, value: str) -> str:
        normalized = value.strip()
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name in {".", ".."}
            or path.suffix not in {".sqlite", ".sqlite3", ".db"}
        ):
            raise ValueError("database filename must be a safe local SQLite filename")
        return normalized

    @field_validator("instance_id")
    @classmethod
    def validate_instance_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("instance_id must contain between 1 and 128 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("instance_id must not contain control characters")
        return normalized

    @model_validator(mode="after")
    def validate_cross_field_limits(self) -> ApplicationSettings:
        if self.lease_heartbeat_seconds * 2 >= self.lease_ttl_seconds:
            raise ValueError("lease heartbeat must be less than half of the lease ttl")
        if self.artifact_retention_total_bytes < self.artifact_max_bytes:
            raise ValueError("artifact retention bytes must be at least one artifact limit")
        return self

    @property
    def registry_path(self) -> Path:
        return self.storage_root / self.registry_filename

    @property
    def jobs_path(self) -> Path:
        return self.storage_root / self.jobs_filename

    @property
    def artifacts_path(self) -> Path:
        return self.storage_root / "artifacts"
