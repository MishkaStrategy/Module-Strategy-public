from __future__ import annotations

from datetime import datetime

from pydantic import Field

from market_data_ingestion.enums import (
    AdapterMaturity,
    DataType,
    MarketType,
    SourceKind,
    ValidationStatus,
)

from .contracts import ContractModel, Pagination


class DatasetDetails(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_uri: str = Field(min_length=1)
    storage_uri: str = Field(min_length=1)
    exchange_id: str = Field(min_length=1, max_length=64)
    market_type: MarketType
    instrument_id: str = Field(min_length=1, max_length=128)
    data_types: tuple[DataType, ...]
    timeframes: tuple[str, ...]
    start: datetime
    end: datetime
    dataset_schema_version: int = Field(gt=0)
    validation_status: ValidationStatus
    unresolved_issues: tuple[str, ...]
    lineage: tuple[str, ...]
    compatible_consumers: tuple[str, ...]
    descriptor_version: int = Field(gt=0)
    registered_at: datetime


class DatasetListResponse(ContractModel):
    items: tuple[DatasetDetails, ...]
    pagination: Pagination


class DataTypeCapabilityResponse(ContractModel):
    data_type: DataType
    timeframes: tuple[str, ...]
    sources: tuple[SourceKind, ...]
    markets: tuple[MarketType, ...]
    requires_api_key: bool
    earliest_available: datetime | None
    notes: tuple[str, ...]


class ExchangeCapabilitiesResponse(ContractModel):
    exchange_id: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)
    markets: tuple[MarketType, ...]
    data: tuple[DataTypeCapabilityResponse, ...]
    maximum_page_size: int = Field(gt=0)
    timestamp_unit: str = Field(min_length=1, max_length=256)
    maturity: AdapterMaturity
    rate_limit_policy: dict[str, object]


class ExchangeListResponse(ContractModel):
    items: tuple[ExchangeCapabilitiesResponse, ...]


class MarketListResponse(ContractModel):
    exchange_id: str = Field(min_length=1, max_length=64)
    markets: tuple[MarketType, ...]


class InstrumentMetadataResponse(ContractModel):
    exchange_id: str = Field(min_length=1, max_length=64)
    market_type: MarketType
    symbol: str = Field(min_length=1, max_length=128)
    canonical_id: str = Field(min_length=1, max_length=128)
    base_asset: str = Field(min_length=1, max_length=64)
    quote_asset: str = Field(min_length=1, max_length=64)
    settlement_asset: str | None = Field(default=None, max_length=64)
    contract_type: str = Field(min_length=1, max_length=64)
    listing_date: datetime | None
    delisting_date: datetime | None
    tick_size: str = Field(min_length=1, max_length=64)
    quantity_step: str = Field(min_length=1, max_length=64)
    contract_multiplier: str = Field(min_length=1, max_length=64)
    trading_status: str = Field(min_length=1, max_length=64)
    metadata_version: str = Field(min_length=1, max_length=64)
