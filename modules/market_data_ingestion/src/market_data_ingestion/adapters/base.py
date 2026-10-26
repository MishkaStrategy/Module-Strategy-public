from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ..contracts import (
    DownloadChunk,
    DownloadPlan,
    ExchangeCapabilities,
    InstrumentMetadata,
    InstrumentReference,
    MarketDataRequest,
    validate_source_uri,
)
from ..enums import DataType, MarketType, SourceKind
from ..errors import AdapterError
from ..records import KlineRecord, NormalizedRecord
from ..serialization import json_bytes


@dataclass(frozen=True, slots=True)
class InstrumentResolution:
    """Resolved instrument plus immutable source evidence for metadata lineage."""

    metadata: InstrumentMetadata
    source: SourceKind
    source_uri: str
    raw_payload: bytes
    response_metadata: dict[str, object]
    upstream_checksum: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, InstrumentMetadata):
            raise TypeError("metadata must be InstrumentMetadata")
        if not isinstance(self.source, SourceKind):
            raise TypeError("source must be SourceKind")
        if not isinstance(self.raw_payload, bytes) or not self.raw_payload:
            raise ValueError("raw_payload must be non-empty bytes")
        object.__setattr__(self, "source_uri", validate_source_uri(self.source_uri))
        if not isinstance(self.response_metadata, dict):
            raise TypeError("response_metadata must be a dictionary")
        object.__setattr__(self, "response_metadata", dict(self.response_metadata))


@dataclass(frozen=True, slots=True)
class AcquiredData:
    chunk: DownloadChunk
    source_uri: str
    raw_payload: bytes
    records: tuple[NormalizedRecord, ...]
    response_metadata: dict[str, object]
    upstream_checksum: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chunk, DownloadChunk):
            raise TypeError("chunk must be DownloadChunk")
        object.__setattr__(self, "source_uri", validate_source_uri(self.source_uri))
        if not isinstance(self.raw_payload, bytes) or not self.raw_payload:
            raise ValueError("raw_payload must be non-empty bytes")
        if not isinstance(self.records, tuple):
            raise TypeError("records must be a tuple")
        if not isinstance(self.response_metadata, dict):
            raise TypeError("response_metadata must be a dictionary")
        object.__setattr__(self, "response_metadata", dict(self.response_metadata))
        if self.upstream_checksum is not None and (
            not isinstance(self.upstream_checksum, str) or not self.upstream_checksum.strip()
        ):
            raise ValueError("upstream_checksum must be a non-empty string")
        if any(record.data_type is not self.chunk.data_type for record in self.records):
            raise ValueError("acquired records must match the download chunk data type")


# Compatibility alias retained for consumers of the first local cycle.
AcquiredKlines = AcquiredData


class ExchangeAdapter(ABC):
    exchange_id: str
    adapter_version: str

    @abstractmethod
    def get_capabilities(self) -> ExchangeCapabilities:
        """Return only capabilities implemented and covered by adapter tests."""

    @abstractmethod
    def resolve_instrument(self, market_type: object, symbol: str) -> InstrumentMetadata:
        """Resolve an exact exchange instrument without fuzzy substitution."""

    def resolve_instrument_with_evidence(
        self, market_type: object, symbol: str
    ) -> InstrumentResolution:
        """Resolve an instrument and retain deterministic adapter evidence.

        Production adapters should override this method to preserve the exact
        official response bytes. The default exists for fake/contract adapters.
        """

        metadata = self.resolve_instrument(market_type, symbol)
        return InstrumentResolution(
            metadata=metadata,
            source=SourceKind.OFFICIAL_REST,
            source_uri=f"adapter://{self.exchange_id}/instrument-metadata",
            raw_payload=json_bytes(metadata, pretty=True),
            response_metadata={"records": 1, "generated_by_adapter_contract": True},
        )

    def restore_instrument_resolution(
        self, market_type: object, symbol: str, raw_payload: bytes
    ) -> InstrumentResolution:
        """Restore instrument metadata from immutable adapter evidence.

        Production adapters should override when the evidence is an exchange-native
        envelope. The default understands the deterministic metadata JSON emitted by
        :meth:`resolve_instrument_with_evidence`.
        """

        if not isinstance(market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AdapterError(
                "METADATA_RAW_RESTORE_FAILED",
                "instrument metadata evidence is not valid JSON",
                False,
                "restore",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("reference"), dict):
            raise AdapterError(
                "METADATA_RAW_RESTORE_FAILED",
                "instrument metadata evidence has an invalid schema",
                False,
                "restore",
            )
        reference_payload = payload["reference"]
        normalized = symbol.replace("/", "").upper()
        reference = InstrumentReference(
            exchange_id=str(reference_payload["exchange_id"]),
            market_type=MarketType(str(reference_payload["market_type"])),
            symbol=str(reference_payload["symbol"]),
            canonical_id=str(reference_payload["canonical_id"]),
        )
        if reference.market_type is not market_type or reference.symbol != normalized:
            raise AdapterError(
                "METADATA_IDENTITY_MISMATCH",
                "raw instrument metadata does not match requested identity",
                False,
                "restore",
            )

        def parsed_time(name: str) -> datetime | None:
            value = payload.get(name)
            return (
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if value is not None
                else None
            )

        metadata = InstrumentMetadata(
            reference=reference,
            base_asset=str(payload["base_asset"]),
            quote_asset=str(payload["quote_asset"]),
            settlement_asset=(
                str(payload["settlement_asset"])
                if payload.get("settlement_asset") is not None
                else None
            ),
            contract_type=str(payload["contract_type"]),
            listing_date=parsed_time("listing_date"),
            delisting_date=parsed_time("delisting_date"),
            tick_size=str(payload["tick_size"]),
            quantity_step=str(payload["quantity_step"]),
            contract_multiplier=str(payload["contract_multiplier"]),
            trading_status=str(payload["trading_status"]),
        )
        return InstrumentResolution(
            metadata=metadata,
            source=SourceKind.OFFICIAL_REST,
            source_uri=f"adapter://{self.exchange_id}/instrument-metadata",
            raw_payload=raw_payload,
            response_metadata={"records": 1, "restored_from_raw": True},
        )

    @abstractmethod
    def plan_download(
        self, request: MarketDataRequest, instrument: InstrumentReference
    ) -> DownloadPlan:
        """Create deterministic, non-overlapping chunks for every requested type."""

    @abstractmethod
    def acquire(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        """Acquire one chunk and return raw evidence plus normalized records."""

    @abstractmethod
    def restore(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> AcquiredData:
        """Restore records from an already verified immutable raw artifact."""

    def acquire_klines(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        if chunk.data_type is not DataType.KLINES:
            raise AdapterError(
                "CHUNK_CONTRACT",
                "acquire_klines requires a klines chunk",
                False,
                "download",
            )
        result = self.acquire(chunk, instrument)
        if any(not isinstance(item, KlineRecord) for item in result.records):
            raise AdapterError(
                "NORMALIZATION_CONTRACT",
                "kline acquisition returned a non-kline record",
                False,
                "normalize",
            )
        return result

    def restore_klines(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> AcquiredData:
        if chunk.data_type is not DataType.KLINES:
            raise AdapterError(
                "CHUNK_CONTRACT",
                "restore_klines requires a klines chunk",
                False,
                "restore",
            )
        result = self.restore(chunk, instrument, raw_payload)
        if any(not isinstance(item, KlineRecord) for item in result.records):
            raise AdapterError(
                "NORMALIZATION_CONTRACT",
                "restored klines contain a non-kline record",
                False,
                "restore",
            )
        return result


def assert_non_overlapping(chunks: Iterable[DownloadChunk]) -> None:
    ordered = sorted(
        chunks,
        key=lambda item: (
            item.data_type.value,
            item.timeframe or "",
            item.start,
            item.end,
        ),
    )
    previous: DownloadChunk | None = None
    for chunk in ordered:
        same_stream = (
            previous is not None
            and previous.data_type is chunk.data_type
            and previous.timeframe == chunk.timeframe
        )
        if same_stream and previous is not None and chunk.start < previous.end:
            raise ValueError(f"download chunks overlap: {previous.chunk_id} / {chunk.chunk_id}")
        previous = chunk
