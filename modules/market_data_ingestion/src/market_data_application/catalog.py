from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from market_data_ingestion.adapters import ExchangeAdapter
from market_data_ingestion.enums import MarketType
from market_data_ingestion.errors import AdapterError
from market_data_ingestion.registry import SQLiteDatasetRegistry, descriptor_from_dict

from .catalog_contracts import (
    DatasetDetails,
    DatasetListResponse,
    DataTypeCapabilityResponse,
    ExchangeCapabilitiesResponse,
    ExchangeListResponse,
    InstrumentMetadataResponse,
    MarketListResponse,
)
from .contracts import Pagination
from .errors import (
    DatasetNotFoundError,
    ExchangeNotFoundError,
    InstrumentNotFoundError,
    InstrumentResolutionError,
    MarketUnsupportedError,
)


class DatasetCatalog:
    """Read-only query model over the Registry and configured exchange adapters."""

    def __init__(
        self,
        registry: SQLiteDatasetRegistry,
        adapters: Mapping[str, ExchangeAdapter],
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)

    def list_datasets(self, *, limit: int = 50, offset: int = 0) -> DatasetListResponse:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid dataset pagination")
        with self._connection() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0])
            rows = connection.execute(
                """
                SELECT descriptor_json, registered_at
                FROM datasets
                ORDER BY registered_at DESC, dataset_id ASC, version DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return DatasetListResponse(
            items=tuple(self._details(str(row[0]), str(row[1])) for row in rows),
            pagination=Pagination(limit=limit, offset=offset, total=total),
        )

    def get_dataset(self, dataset_id: str, *, version: int | None = None) -> DatasetDetails:
        normalized = dataset_id.strip().lower()
        if not normalized:
            raise ValueError("dataset_id must be non-empty")
        if version is not None and version <= 0:
            raise ValueError("dataset version must be positive")
        query = (
            "SELECT descriptor_json, registered_at FROM datasets WHERE dataset_id=? AND version=?"
            if version is not None
            else (
                "SELECT descriptor_json, registered_at FROM datasets WHERE dataset_id=? "
                "ORDER BY version DESC LIMIT 1"
            )
        )
        params: tuple[object, ...] = (normalized, version) if version is not None else (normalized,)
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            raise DatasetNotFoundError(dataset_id=normalized, version=version)
        return self._details(str(row[0]), str(row[1]))

    def list_exchanges(self) -> ExchangeListResponse:
        return ExchangeListResponse(
            items=tuple(
                self._capabilities(exchange_id, adapter)
                for exchange_id, adapter in sorted(self._adapters.items())
            )
        )

    def get_exchange(self, exchange_id: str) -> ExchangeCapabilitiesResponse:
        normalized, adapter = self._adapter(exchange_id)
        return self._capabilities(normalized, adapter)

    def list_markets(self, exchange_id: str) -> MarketListResponse:
        exchange = self.get_exchange(exchange_id)
        return MarketListResponse(exchange_id=exchange.exchange_id, markets=exchange.markets)

    def resolve_instrument(
        self,
        exchange_id: str,
        market_type: MarketType,
        symbol: str,
    ) -> InstrumentMetadataResponse:
        normalized_exchange, adapter = self._adapter(exchange_id)
        normalized_symbol = symbol.strip().upper()
        capabilities = adapter.get_capabilities()
        if market_type not in capabilities.markets:
            raise MarketUnsupportedError(
                exchange_id=normalized_exchange,
                market_type=market_type.value,
            )
        if not normalized_symbol:
            raise InstrumentNotFoundError(
                exchange_id=normalized_exchange,
                market_type=market_type.value,
                symbol=normalized_symbol,
            )
        try:
            metadata = adapter.resolve_instrument(market_type, normalized_symbol)
        except AdapterError as error:
            if error.code == "INSTRUMENT_NOT_FOUND":
                raise InstrumentNotFoundError(
                    exchange_id=normalized_exchange,
                    market_type=market_type.value,
                    symbol=normalized_symbol,
                ) from error
            if error.code == "MARKET_UNSUPPORTED":
                raise MarketUnsupportedError(
                    exchange_id=normalized_exchange,
                    market_type=market_type.value,
                ) from error
            raise InstrumentResolutionError(
                exchange_id=normalized_exchange,
                market_type=market_type.value,
                symbol=normalized_symbol,
                backend_code=error.code,
                retryable=error.retryable,
            ) from error
        return InstrumentMetadataResponse(
            exchange_id=metadata.reference.exchange_id,
            market_type=metadata.reference.market_type,
            symbol=metadata.reference.symbol,
            canonical_id=metadata.reference.canonical_id,
            base_asset=metadata.base_asset,
            quote_asset=metadata.quote_asset,
            settlement_asset=metadata.settlement_asset,
            contract_type=metadata.contract_type,
            listing_date=metadata.listing_date,
            delisting_date=metadata.delisting_date,
            tick_size=metadata.tick_size,
            quantity_step=metadata.quantity_step,
            contract_multiplier=metadata.contract_multiplier,
            trading_status=metadata.trading_status,
            metadata_version=metadata.metadata_version,
        )

    def _adapter(self, exchange_id: str) -> tuple[str, ExchangeAdapter]:
        normalized = exchange_id.strip().lower()
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise ExchangeNotFoundError(normalized)
        return normalized, adapter

    @staticmethod
    def _capabilities(
        exchange_id: str,
        adapter: ExchangeAdapter,
    ) -> ExchangeCapabilitiesResponse:
        capabilities = adapter.get_capabilities()
        return ExchangeCapabilitiesResponse(
            exchange_id=exchange_id,
            adapter_version=capabilities.exchange.adapter_version,
            markets=capabilities.markets,
            data=tuple(
                DataTypeCapabilityResponse(
                    data_type=item.data_type,
                    timeframes=item.timeframes,
                    sources=item.sources,
                    markets=item.markets,
                    requires_api_key=item.requires_api_key,
                    earliest_available=item.earliest_available,
                    notes=item.notes,
                )
                for item in capabilities.data
            ),
            maximum_page_size=capabilities.maximum_page_size,
            timestamp_unit=capabilities.timestamp_unit,
            maturity=capabilities.maturity,
            rate_limit_policy=dict(capabilities.rate_limit_policy),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._registry.path, timeout=10)
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _public_storage_uri(storage_uri: str, *, dataset_id: str, version: int) -> str:
        parsed = urlsplit(storage_uri)
        if parsed.scheme.lower() != "file":
            return storage_uri
        safe_id = quote(dataset_id, safe="")
        return f"local://managed/{safe_id}/v{version}"

    @staticmethod
    def _details(payload_json: str, registered_at_text: str) -> DatasetDetails:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise TypeError("registry descriptor payload must be an object")
        descriptor = descriptor_from_dict(payload)
        registered_at = datetime.fromisoformat(registered_at_text)
        if registered_at.tzinfo is None or registered_at.utcoffset() is None:
            raise ValueError("registered_at must be timezone-aware")
        return DatasetDetails(
            dataset_id=descriptor.dataset_id,
            version=descriptor.version,
            dataset_hash=descriptor.dataset_hash,
            manifest_uri=descriptor.manifest_uri,
            storage_uri=DatasetCatalog._public_storage_uri(
                descriptor.storage_uri,
                dataset_id=descriptor.dataset_id,
                version=descriptor.version,
            ),
            exchange_id=descriptor.exchange_id,
            market_type=descriptor.market_type,
            instrument_id=descriptor.instrument_id,
            data_types=descriptor.data_types,
            timeframes=descriptor.timeframes,
            start=descriptor.start,
            end=descriptor.end,
            dataset_schema_version=descriptor.schema_version,
            validation_status=descriptor.validation_status,
            unresolved_issues=descriptor.unresolved_issues,
            lineage=descriptor.lineage,
            compatible_consumers=descriptor.compatible_consumers,
            descriptor_version=descriptor.descriptor_version,
            registered_at=registered_at.astimezone(UTC),
        )
