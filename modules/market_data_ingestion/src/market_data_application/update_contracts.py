from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from market_data_ingestion.enums import DataType

from .contracts import ContractModel, StorageFormat

_UPDATEABLE_DATA_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)


class CreateDatasetUpdateRequest(ContractModel):
    version: int | None = Field(default=None, gt=0)
    end: datetime
    required_data: tuple[DataType, ...] = ()
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)

    @field_validator("end")
    @classmethod
    def normalize_end(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("end must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("required_data")
    @classmethod
    def validate_required_data(cls, value: tuple[DataType, ...]) -> tuple[DataType, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required_data must be unique")
        unsupported = sorted(set(value) - _UPDATEABLE_DATA_TYPES, key=lambda item: item.value)
        if unsupported:
            raise ValueError("required_data must contain updateable historical series data types")
        return value


class DatasetUpdateJobRequest(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    parent_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_end: datetime
    end: datetime
    required_data: tuple[DataType, ...] = ()
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)

    @field_validator("dataset_id")
    @classmethod
    def normalize_dataset_id(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("parent_end", "end")
    @classmethod
    def normalize_times(cls, value: datetime, info: Any) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("required_data")
    @classmethod
    def validate_required_data(cls, value: tuple[DataType, ...]) -> tuple[DataType, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required_data must be unique")
        unsupported = sorted(set(value) - _UPDATEABLE_DATA_TYPES, key=lambda item: item.value)
        if unsupported:
            raise ValueError("required_data must contain updateable historical series data types")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> DatasetUpdateJobRequest:
        if self.end <= self.parent_end:
            raise ValueError("update end must be after the pinned parent end")
        return self
