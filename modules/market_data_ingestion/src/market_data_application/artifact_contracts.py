from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .contracts import ContractModel


class CreateDatasetExportRequest(ContractModel):
    """Transport request for exporting one immutable registered dataset version."""

    version: int | None = Field(default=None, gt=0)


class DatasetExportJobRequest(ContractModel):
    """Persisted export request with an exact immutable Dataset Registry identity."""

    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)

    @field_validator("dataset_id")
    @classmethod
    def normalize_dataset_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("dataset_id must be a non-empty printable identifier")
        return normalized


class ArtifactDescriptor(ContractModel):
    """Public metadata for an immutable application-generated artifact."""

    artifact_id: str = Field(pattern=r"^mdi_artifact_[0-9a-f]{24}$")
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(pattern=r"^mdi_artifact_[0-9a-f]{24}\.zip$")
    content_type: Literal["application/zip"] = "application/zip"
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    file_count: int = Field(ge=2)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    descriptor: ArtifactDescriptor
    path: Path
