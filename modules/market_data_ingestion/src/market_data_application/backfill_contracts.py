from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from market_data_ingestion.enums import DataType

from .contracts import ContractModel, StorageFormat

_BACKFILL_DATA_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)


class CreateDatasetBackfillRequest(ContractModel):
    version: int | None = Field(default=None, gt=0)
    data_type: DataType
    start: datetime
    end: datetime
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, value: DataType) -> DataType:
        if value not in _BACKFILL_DATA_TYPES:
            raise ValueError("data_type must be an implemented historical series")
        return value

    @field_validator("start", "end")
    @classmethod
    def normalize_times(cls, value: datetime, info: Any) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_range(self) -> CreateDatasetBackfillRequest:
        if self.start >= self.end:
            raise ValueError("backfill start must precede end")
        return self


class DatasetBackfillJobRequest(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    parent_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_type: DataType
    stream_timeframe: str | None = Field(default=None, min_length=1, max_length=16)
    start: datetime
    end: datetime
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)

    @field_validator("dataset_id")
    @classmethod
    def normalize_dataset_id(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, value: DataType) -> DataType:
        if value not in _BACKFILL_DATA_TYPES:
            raise ValueError("data_type must be an implemented historical series")
        return value

    @field_validator("stream_timeframe")
    @classmethod
    def normalize_timeframe(cls, value: str | None) -> str | None:
        return None if value is None else value.strip().lower()

    @field_validator("start", "end")
    @classmethod
    def normalize_times(cls, value: datetime, info: Any) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_contract(self) -> DatasetBackfillJobRequest:
        if self.start >= self.end:
            raise ValueError("backfill start must precede end")
        if self.data_type is DataType.FUNDING_RATE:
            if self.stream_timeframe is not None:
                raise ValueError("funding-rate backfill must not include a timeframe")
        elif self.stream_timeframe is None:
            raise ValueError("interval backfill requires the pinned base timeframe")
        return self
