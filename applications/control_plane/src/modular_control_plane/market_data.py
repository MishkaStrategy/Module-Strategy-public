from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel

_PATH_SEPARATORS = ("/", "\\")


class MarketDatasetSummary(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    exchange_id: str = Field(min_length=1, max_length=64)
    market_type: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=128)
    data_types: tuple[str, ...] = Field(max_length=64)
    timeframes: tuple[str, ...] = Field(max_length=64)
    start: datetime
    end: datetime
    dataset_schema_version: int = Field(gt=0)
    validation_status: str = Field(min_length=1, max_length=64)
    unresolved_issue_count: int = Field(ge=0)
    lineage_count: int = Field(ge=0)
    compatible_consumers: tuple[str, ...] = Field(max_length=128)
    descriptor_version: int = Field(gt=0)
    registered_at: datetime

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        return _path_free_token(value, "dataset_id", maximum_length=128)

    @field_validator("exchange_id", "market_type", "validation_status")
    @classmethod
    def validate_short_identity(cls, value: str) -> str:
        return _path_free_token(value, "Dataset identity", maximum_length=64)

    @field_validator("instrument_id")
    @classmethod
    def validate_instrument_id(cls, value: str) -> str:
        return _path_free_token(value, "instrument_id", maximum_length=128)

    @field_validator("start", "end", "registered_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Market Dataset timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("data_types", "timeframes", "compatible_consumers")
    @classmethod
    def validate_bounded_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _path_free_token(item, "Market Dataset list value", maximum_length=128)
        return value

    @model_validator(mode="after")
    def validate_range(self) -> MarketDatasetSummary:
        if self.start >= self.end:
            raise ValueError("Market Dataset start must precede end")
        return self


class MarketDatasetList(ContractModel):
    items: tuple[MarketDatasetSummary, ...]
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page(self) -> MarketDatasetList:
        if len(self.items) > self.limit:
            raise ValueError("Market Dataset page exceeds requested limit")
        if self.offset + len(self.items) > self.total and self.items:
            raise ValueError("Market Dataset page exceeds total")
        return self


class MarketDatasetValidation(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_status: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern=r"^(ok|error)$")
    deep: bool
    partitions_checked: int = Field(ge=0)
    raw_artifacts_checked: int = Field(ge=0)
    rows_checked: int = Field(ge=0)
    issue_count: int = Field(ge=0)

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        return _path_free_token(value, "dataset_id", maximum_length=128)

    @field_validator("validation_status")
    @classmethod
    def validate_validation_status(cls, value: str) -> str:
        return _path_free_token(value, "validation_status", maximum_length=64)


class MarketDataCatalogReader(Protocol):
    def list_datasets(self, *, limit: int, offset: int) -> MarketDatasetList: ...

    def get_dataset(self, dataset_id: str, *, version: int | None) -> MarketDatasetSummary: ...

    def validate_dataset(
        self,
        dataset_id: str,
        *,
        version: int | None,
        deep: bool,
    ) -> MarketDatasetValidation: ...


class MarketDataCatalogControlService:
    """Read-only sanitized projection over the native Market Data application API."""

    def __init__(self, reader: MarketDataCatalogReader) -> None:
        for method_name in ("list_datasets", "get_dataset", "validate_dataset"):
            if not callable(getattr(reader, method_name, None)):
                raise TypeError(f"reader must implement {method_name}()")
        self._reader = reader

    def list_datasets(self, *, limit: int = 50, offset: int = 0) -> MarketDatasetList:
        _pagination(limit=limit, offset=offset)
        return self._reader.list_datasets(limit=limit, offset=offset)

    def get_dataset(self, dataset_id: str, *, version: int | None = None) -> MarketDatasetSummary:
        _dataset_id(dataset_id)
        _version(version)
        return self._reader.get_dataset(dataset_id, version=version)

    def validate_dataset(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        deep: bool = False,
    ) -> MarketDatasetValidation:
        _dataset_id(dataset_id)
        _version(version)
        if not isinstance(deep, bool):
            raise TypeError("deep must be bool")
        return self._reader.validate_dataset(dataset_id, version=version, deep=deep)


def _path_free_token(value: str, name: str, *, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if (
        not value
        or value != value.strip()
        or len(value) > maximum_length
        or "\x00" in value
        or any(separator in value for separator in _PATH_SEPARATORS)
    ):
        raise ValueError(f"{name} must be a bounded path-free token")
    return value


def _pagination(*, limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _dataset_id(value: str) -> None:
    _path_free_token(value, "dataset_id", maximum_length=128)


def _version(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("version must be a positive integer or None")
