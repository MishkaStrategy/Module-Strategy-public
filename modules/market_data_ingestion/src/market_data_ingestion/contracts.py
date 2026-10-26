from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .canonical import sha256_canonical, utc_datetime
from .enums import (
    AdapterMaturity,
    DataType,
    GapReason,
    MarketType,
    RepairStatus,
    RunState,
    Severity,
    SourceKind,
    ValidationStatus,
)
from .timeframes import timeframe_seconds

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def validate_source_uri(value: str, name: str = "source_uri") -> str:
    """Return a persistence-safe source URI without credentials or query data."""

    normalized = _text(value, name)
    parsed = urlsplit(normalized)
    if not parsed.scheme:
        raise ValueError(f"{name} must be an absolute URI")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain query or fragment data")
    return normalized


def validate_relative_storage_uri(value: str, name: str = "storage_uri") -> str:
    normalized = _text(value, name)
    if "\\" in normalized:
        raise ValueError(f"{name} must use POSIX separators")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be a safe relative path")
    return normalized


def _identifier(value: str, name: str) -> str:
    result = _text(value, name).lower()
    if _ID.fullmatch(result) is None:
        raise ValueError(f"{name} contains unsupported characters")
    return result


def _sha(value: str, name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class ExchangeReference:
    id: str
    adapter_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "exchange id"))
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter version"))


@dataclass(frozen=True, slots=True)
class MarketReference:
    exchange_id: str
    market_type: MarketType
    market_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange_id", _identifier(self.exchange_id, "exchange id"))
        object.__setattr__(self, "market_id", _identifier(self.market_id, "market id"))
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")


@dataclass(frozen=True, slots=True)
class InstrumentReference:
    exchange_id: str
    market_type: MarketType
    symbol: str
    canonical_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange_id", _identifier(self.exchange_id, "exchange id"))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol").upper())
        object.__setattr__(self, "canonical_id", _identifier(self.canonical_id, "canonical id"))
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")


@dataclass(frozen=True, slots=True)
class InstrumentMetadata:
    reference: InstrumentReference
    base_asset: str
    quote_asset: str
    settlement_asset: str | None
    contract_type: str
    listing_date: datetime | None
    delisting_date: datetime | None
    tick_size: str
    quantity_step: str
    contract_multiplier: str
    trading_status: str
    metadata_version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.reference, InstrumentReference):
            raise TypeError("reference must be InstrumentReference")
        for name in (
            "base_asset",
            "quote_asset",
            "contract_type",
            "tick_size",
            "quantity_step",
            "contract_multiplier",
            "trading_status",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.settlement_asset is not None:
            object.__setattr__(
                self,
                "settlement_asset",
                _text(self.settlement_asset, "settlement_asset"),
            )
        if self.listing_date is not None:
            object.__setattr__(
                self,
                "listing_date",
                utc_datetime(self.listing_date, "listing_date"),
            )
        if self.delisting_date is not None:
            object.__setattr__(
                self,
                "delisting_date",
                utc_datetime(self.delisting_date, "delisting_date"),
            )
        if self.listing_date and self.delisting_date and self.listing_date >= self.delisting_date:
            raise ValueError("listing_date must be before delisting_date")


@dataclass(frozen=True, slots=True)
class DataTypeCapability:
    data_type: DataType
    timeframes: tuple[str, ...] = ()
    sources: tuple[SourceKind, ...] = ()
    markets: tuple[MarketType, ...] = ()
    requires_api_key: bool = False
    earliest_available: datetime | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.data_type, DataType):
            raise TypeError("data_type must be DataType")
        normalized = tuple(item.strip().lower() for item in self.timeframes)
        for item in normalized:
            timeframe_seconds(item)
        object.__setattr__(self, "timeframes", normalized)
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability timeframes must be unique")
        if any(not isinstance(item, SourceKind) for item in self.sources):
            raise TypeError("sources must contain SourceKind")
        if any(not isinstance(item, MarketType) for item in self.markets):
            raise TypeError("markets must contain MarketType")
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("capability markets must be unique")
        if self.earliest_available is not None:
            object.__setattr__(self, "earliest_available", utc_datetime(self.earliest_available))

    def supports_market(self, market_type: MarketType) -> bool:
        if not isinstance(market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        return not self.markets or market_type in self.markets


@dataclass(frozen=True, slots=True)
class ExchangeCapabilities:
    exchange: ExchangeReference
    markets: tuple[MarketType, ...]
    data: tuple[DataTypeCapability, ...]
    maximum_page_size: int
    timestamp_unit: str
    maturity: AdapterMaturity
    rate_limit_policy: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.maximum_page_size <= 0:
            raise ValueError("maximum_page_size must be positive")
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("markets must be unique")
        if any(not isinstance(item, MarketType) for item in self.markets):
            raise TypeError("markets must contain MarketType")
        if len({item.data_type for item in self.data}) != len(self.data):
            raise ValueError("data capabilities must be unique by data_type")


@dataclass(frozen=True, slots=True)
class MarketDataRequest:
    request_id: str
    exchange_id: str
    market_type: MarketType
    symbol: str
    required_data: tuple[DataType, ...]
    optional_data: tuple[DataType, ...]
    base_timeframe: str
    derived_timeframes: tuple[str, ...]
    start: datetime | None
    end: datetime
    full_available_history: bool = False
    allow_provisional: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "exchange_id", _identifier(self.exchange_id, "exchange_id"))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol").upper())
        object.__setattr__(self, "base_timeframe", self.base_timeframe.strip().lower())
        timeframe_seconds(self.base_timeframe)
        normalized_derived = tuple(item.strip().lower() for item in self.derived_timeframes)
        for item in normalized_derived:
            timeframe_seconds(item)
            if timeframe_seconds(item) % timeframe_seconds(self.base_timeframe):
                raise ValueError("derived timeframe must be an integer multiple of base timeframe")
        object.__setattr__(self, "derived_timeframes", normalized_derived)
        if not self.required_data:
            raise ValueError("at least one required data type is required")
        if any(
            not isinstance(item, DataType) for item in (*self.required_data, *self.optional_data)
        ):
            raise TypeError("required_data and optional_data must contain DataType")
        if set(self.required_data) & set(self.optional_data):
            raise ValueError("required_data and optional_data must not overlap")
        if len(set(self.required_data)) != len(self.required_data):
            raise ValueError("required_data must be unique")
        if len(set(self.optional_data)) != len(self.optional_data):
            raise ValueError("optional_data must be unique")
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        if self.start is not None:
            object.__setattr__(self, "start", utc_datetime(self.start, "start"))
        object.__setattr__(self, "end", utc_datetime(self.end, "end"))
        if self.start is None and not self.full_available_history:
            raise ValueError("start is required unless full_available_history is true")
        if self.start is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        if self.schema_version != 1:
            raise ValueError("unsupported MarketDataRequest schema_version")

    @property
    def canonical_hash(self) -> str:
        payload = asdict(self)
        payload.pop("request_id")
        return sha256_canonical(payload)


@dataclass(frozen=True, slots=True)
class DownloadChunk:
    chunk_id: str
    data_type: DataType
    start: datetime
    end: datetime
    source: SourceKind
    timeframe: str | None = None
    expected_uri: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunk_id", _identifier(self.chunk_id, "chunk_id"))
        object.__setattr__(self, "start", utc_datetime(self.start, "start"))
        object.__setattr__(self, "end", utc_datetime(self.end, "end"))
        if self.start >= self.end:
            raise ValueError("chunk start must precede end")
        if self.timeframe is not None:
            object.__setattr__(self, "timeframe", self.timeframe.strip().lower())
            timeframe_seconds(self.timeframe)
        if self.expected_uri is not None:
            object.__setattr__(
                self,
                "expected_uri",
                validate_source_uri(self.expected_uri, "expected_uri"),
            )


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    request_hash: str
    instrument: InstrumentReference
    chunks: tuple[DownloadChunk, ...]
    created_at: datetime
    limitations: tuple[str, ...] = ()
    plan_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_hash", _sha(self.request_hash, "request_hash"))
        object.__setattr__(self, "created_at", utc_datetime(self.created_at, "created_at"))
        ids = [item.chunk_id for item in self.chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("chunk ids must be unique")
        if any(not isinstance(item, str) or not item.strip() for item in self.limitations):
            raise ValueError("download plan limitations must be non-empty strings")


@dataclass(frozen=True, slots=True)
class DownloadCheckpoint:
    run_id: str
    request_hash: str
    state: RunState
    completed_chunks: tuple[str, ...]
    pending_chunks: tuple[str, ...]
    updated_at: datetime
    checkpoint_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "request_hash", _sha(self.request_hash, "request_hash"))
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at, "updated_at"))
        if set(self.completed_chunks) & set(self.pending_chunks):
            raise ValueError("completed and pending chunks must not overlap")


@dataclass(frozen=True, slots=True)
class RawArtifactDescriptor:
    artifact_id: str
    source: SourceKind
    source_uri: str
    retrieved_at: datetime
    checksum_sha256: str
    byte_size: int
    record_count: int | None
    start: datetime
    end: datetime
    adapter_version: str
    response_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        if not isinstance(self.source, SourceKind):
            raise TypeError("source must be SourceKind")
        object.__setattr__(self, "source_uri", validate_source_uri(self.source_uri))
        object.__setattr__(self, "retrieved_at", utc_datetime(self.retrieved_at))
        object.__setattr__(self, "checksum_sha256", _sha(self.checksum_sha256, "checksum"))
        object.__setattr__(self, "start", utc_datetime(self.start))
        object.__setattr__(self, "end", utc_datetime(self.end))
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter_version"))
        if not isinstance(self.response_metadata, dict):
            raise TypeError("response_metadata must be a dictionary")
        object.__setattr__(self, "response_metadata", dict(self.response_metadata))
        if not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool):
            raise TypeError("byte_size must be an integer")
        if self.record_count is not None and (
            not isinstance(self.record_count, int) or isinstance(self.record_count, bool)
        ):
            raise TypeError("record_count must be an integer or None")
        if self.byte_size < 0 or (self.record_count is not None and self.record_count < 0):
            raise ValueError("artifact sizes and counts must be non-negative")
        if self.start >= self.end:
            raise ValueError("raw artifact start must precede end")


@dataclass(frozen=True, slots=True)
class NormalizedPartitionDescriptor:
    partition_id: str
    data_type: DataType
    timeframe: str | None
    storage_uri: str
    checksum_sha256: str
    row_count: int
    start: datetime
    end: datetime
    schema_version: int
    source_artifact_ids: tuple[str, ...]

    @property
    def format(self) -> str:
        suffix = self.storage_uri.rsplit(".", 1)[-1].lower()
        if suffix not in {"csv", "parquet"}:
            raise ValueError("unsupported partition format")
        return suffix

    def __post_init__(self) -> None:
        object.__setattr__(self, "partition_id", _identifier(self.partition_id, "partition_id"))
        if not isinstance(self.data_type, DataType):
            raise TypeError("data_type must be DataType")
        interval_types = {
            DataType.KLINES,
            DataType.MARK_PRICE,
            DataType.INDEX_PRICE,
            DataType.PREMIUM_INDEX,
            DataType.OPEN_INTEREST_HISTORY,
        }
        point_types = {
            DataType.FUNDING_RATE,
            DataType.INSTRUMENT_METADATA,
            DataType.EXCHANGE_METADATA,
        }
        if self.timeframe is not None:
            normalized_timeframe = self.timeframe.strip().lower()
            timeframe_seconds(normalized_timeframe)
            object.__setattr__(self, "timeframe", normalized_timeframe)
        if self.data_type in interval_types and self.timeframe is None:
            raise ValueError("interval partition requires timeframe")
        if self.data_type in point_types and self.timeframe is not None:
            raise ValueError("point-event partition must not declare timeframe")
        object.__setattr__(
            self,
            "storage_uri",
            validate_relative_storage_uri(self.storage_uri),
        )
        object.__setattr__(self, "checksum_sha256", _sha(self.checksum_sha256, "checksum"))
        object.__setattr__(self, "start", utc_datetime(self.start))
        object.__setattr__(self, "end", utc_datetime(self.end))
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool):
            raise TypeError("row_count must be an integer")
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version,
            bool,
        ):
            raise TypeError("schema_version must be an integer")
        if self.row_count < 0 or self.schema_version <= 0:
            raise ValueError("row_count must be non-negative")
        if self.start >= self.end:
            raise ValueError("partition start must precede end")
        normalized_sources = tuple(
            _identifier(item, "source_artifact_id") for item in self.source_artifact_ids
        )
        if not normalized_sources:
            raise ValueError("partition must retain at least one lineage reference")
        if len(normalized_sources) != len(set(normalized_sources)):
            raise ValueError("partition lineage references must be unique")
        object.__setattr__(self, "source_artifact_ids", normalized_sources)


@dataclass(frozen=True, slots=True)
class GapDescriptor:
    start: datetime
    end: datetime
    timeframe: str
    missing_intervals: int
    reason: GapReason = GapReason.UNKNOWN
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", utc_datetime(self.start))
        object.__setattr__(self, "end", utc_datetime(self.end))
        object.__setattr__(self, "timeframe", self.timeframe.strip().lower())
        timeframe_seconds(self.timeframe)
        if not isinstance(self.reason, GapReason):
            raise TypeError("reason must be GapReason")
        if any(not isinstance(item, str) or not item.strip() for item in self.evidence):
            raise TypeError("gap evidence must contain non-empty strings")
        if self.start >= self.end or self.missing_intervals <= 0:
            raise ValueError("invalid gap range")


@dataclass(frozen=True, slots=True)
class DuplicateDescriptor:
    timestamp: datetime
    exact: bool
    source_partition_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))
        if not isinstance(self.exact, bool):
            raise TypeError("exact must be boolean")
        source_ids = tuple(
            _identifier(item, "source_partition_id") for item in self.source_partition_ids
        )
        evidence_hashes = tuple(_sha(item, "evidence_hash") for item in self.evidence_hashes)
        object.__setattr__(self, "source_partition_ids", source_ids)
        object.__setattr__(self, "evidence_hashes", evidence_hashes)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    severity: Severity
    message: str
    start: datetime | None = None
    end: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, "issue code"))
        object.__setattr__(self, "message", _text(self.message, "message"))
        if not isinstance(self.severity, Severity):
            raise TypeError("severity must be Severity")
        if not isinstance(self.evidence, dict):
            raise TypeError("evidence must be a dictionary")
        object.__setattr__(self, "evidence", dict(self.evidence))
        if self.start is not None:
            object.__setattr__(self, "start", utc_datetime(self.start))
        if self.end is not None:
            object.__setattr__(self, "end", utc_datetime(self.end))
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("issue start must not follow end")


@dataclass(frozen=True, slots=True)
class DataValidationReport:
    status: ValidationStatus
    issues: tuple[ValidationIssue, ...]
    gaps: tuple[GapDescriptor, ...]
    duplicates: tuple[DuplicateDescriptor, ...]
    rows_checked: int
    created_at: datetime
    report_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.status, ValidationStatus):
            raise TypeError("status must be ValidationStatus")
        if any(not isinstance(item, ValidationIssue) for item in self.issues):
            raise TypeError("issues must contain ValidationIssue")
        if any(not isinstance(item, GapDescriptor) for item in self.gaps):
            raise TypeError("gaps must contain GapDescriptor")
        if any(not isinstance(item, DuplicateDescriptor) for item in self.duplicates):
            raise TypeError("duplicates must contain DuplicateDescriptor")
        object.__setattr__(self, "created_at", utc_datetime(self.created_at))
        if not isinstance(self.rows_checked, int) or isinstance(self.rows_checked, bool):
            raise TypeError("rows_checked must be an integer")
        if self.rows_checked < 0:
            raise ValueError("rows_checked must be non-negative")
        if self.report_version != 1:
            raise ValueError("unsupported validation report version")


@dataclass(frozen=True, slots=True)
class DataTypeValidationReport:
    data_type: DataType
    timeframe: str | None
    report: DataValidationReport

    def __post_init__(self) -> None:
        if not isinstance(self.data_type, DataType):
            raise TypeError("data_type must be DataType")
        if self.timeframe is not None:
            normalized = self.timeframe.strip().lower()
            timeframe_seconds(normalized)
            object.__setattr__(self, "timeframe", normalized)
        if not isinstance(self.report, DataValidationReport):
            raise TypeError("report must be DataValidationReport")


@dataclass(frozen=True, slots=True)
class RepairPlan:
    dataset_id: str
    dataset_version: int
    gaps: tuple[GapDescriptor, ...]
    conflicting_partition_ids: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _identifier(self.dataset_id, "dataset_id"))
        object.__setattr__(self, "created_at", utc_datetime(self.created_at, "created_at"))
        if self.dataset_version <= 0:
            raise ValueError("dataset_version must be positive")


@dataclass(frozen=True, slots=True)
class RepairResult:
    status: RepairStatus
    repaired_ranges: tuple[tuple[datetime, datetime], ...]
    unresolved_gaps: tuple[GapDescriptor, ...]
    new_dataset_version: int | None
    audit_uri: str

    def __post_init__(self) -> None:
        normalized: list[tuple[datetime, datetime]] = []
        for start, end in self.repaired_ranges:
            start_utc = utc_datetime(start, "repair start")
            end_utc = utc_datetime(end, "repair end")
            if start_utc >= end_utc:
                raise ValueError("repair range start must precede end")
            normalized.append((start_utc, end_utc))
        object.__setattr__(self, "repaired_ranges", tuple(normalized))
        if self.new_dataset_version is not None and self.new_dataset_version <= 0:
            raise ValueError("new_dataset_version must be positive")
        object.__setattr__(self, "audit_uri", _text(self.audit_uri, "audit_uri"))


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_id: str
    version: int
    exchange: ExchangeReference
    market_type: MarketType
    instrument: InstrumentReference
    data_types: tuple[DataType, ...]
    timeframes: tuple[str, ...]
    start: datetime
    end: datetime
    partitions: tuple[NormalizedPartitionDescriptor, ...]
    source_artifacts: tuple[RawArtifactDescriptor, ...]
    validation: DataValidationReport
    normalization_version: str
    validation_policy_version: str
    schema_version: int
    lineage: tuple[str, ...]
    created_at: datetime
    data_type_reports: tuple[DataTypeValidationReport, ...] = ()
    manifest_version: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _identifier(self.dataset_id, "dataset_id"))
        if not isinstance(self.exchange, ExchangeReference):
            raise TypeError("exchange must be ExchangeReference")
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        if not isinstance(self.instrument, InstrumentReference):
            raise TypeError("instrument must be InstrumentReference")
        if self.instrument.exchange_id != self.exchange.id:
            raise ValueError("instrument exchange must match manifest exchange")
        if self.instrument.market_type is not self.market_type:
            raise ValueError("instrument market must match manifest market")
        if not isinstance(self.validation, DataValidationReport):
            raise TypeError("validation must be DataValidationReport")
        if any(not isinstance(item, DataType) for item in self.data_types):
            raise TypeError("data_types must contain DataType")
        if not self.data_types or len(self.data_types) != len(set(self.data_types)):
            raise ValueError("data_types must be non-empty and unique")
        normalized_timeframes = tuple(_text(item, "timeframe").lower() for item in self.timeframes)
        for item in normalized_timeframes:
            timeframe_seconds(item)
        if len(normalized_timeframes) != len(set(normalized_timeframes)):
            raise ValueError("timeframes must be unique")
        object.__setattr__(self, "timeframes", normalized_timeframes)
        if any(not isinstance(item, NormalizedPartitionDescriptor) for item in self.partitions):
            raise TypeError("partitions must contain NormalizedPartitionDescriptor")
        if any(not isinstance(item, RawArtifactDescriptor) for item in self.source_artifacts):
            raise TypeError("source_artifacts must contain RawArtifactDescriptor")
        if not self.partitions or not self.source_artifacts:
            raise ValueError("manifest must contain partitions and raw source artifacts")
        object.__setattr__(self, "start", utc_datetime(self.start))
        object.__setattr__(self, "end", utc_datetime(self.end))
        object.__setattr__(self, "created_at", utc_datetime(self.created_at))
        object.__setattr__(
            self,
            "normalization_version",
            _text(self.normalization_version, "normalization_version"),
        )
        object.__setattr__(
            self,
            "validation_policy_version",
            _text(self.validation_policy_version, "validation_policy_version"),
        )
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise TypeError("version must be an integer")
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version,
            bool,
        ):
            raise TypeError("schema_version must be an integer")
        if self.version <= 0 or self.schema_version <= 0:
            raise ValueError("dataset and schema versions must be positive")
        if self.start >= self.end:
            raise ValueError("dataset start must precede end")
        if self.manifest_version != 2:
            raise ValueError("unsupported manifest version")
        if any(not isinstance(item, str) or not item.strip() for item in self.lineage):
            raise TypeError("lineage must contain non-empty strings")
        partition_ids = tuple(item.partition_id for item in self.partitions)
        raw_ids = tuple(item.artifact_id for item in self.source_artifacts)
        if len(partition_ids) != len(set(partition_ids)):
            raise ValueError("partition IDs must be unique")
        if len(raw_ids) != len(set(raw_ids)):
            raise ValueError("raw artifact IDs must be unique")
        declared_types = set(self.data_types)
        partition_types = {item.data_type for item in self.partitions}
        if declared_types != partition_types:
            raise ValueError("manifest data_types must exactly match partition data types")
        partition_timeframes = {
            item.timeframe for item in self.partitions if item.timeframe is not None
        }
        if set(self.timeframes) != partition_timeframes:
            raise ValueError("manifest timeframes must exactly match partition timeframes")
        known_ids = set(partition_ids) | set(raw_ids)
        graph: dict[str, tuple[str, ...]] = {}
        for partition in self.partitions:
            unknown = set(partition.source_artifact_ids) - known_ids
            if unknown:
                raise ValueError("partition lineage contains unknown references")
            if partition.partition_id in partition.source_artifact_ids:
                raise ValueError("partition cannot reference itself")
            graph[partition.partition_id] = tuple(
                item for item in partition.source_artifact_ids if item in set(partition_ids)
            )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("partition lineage contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for parent in graph[node]:
                visit(parent)
            visiting.remove(node)
            visited.add(node)

        for partition_id in partition_ids:
            visit(partition_id)
        if any(not isinstance(item, DataTypeValidationReport) for item in self.data_type_reports):
            raise TypeError("data_type_reports must contain DataTypeValidationReport")
        keys = [(item.data_type, item.timeframe) for item in self.data_type_reports]
        if len(keys) != len(set(keys)):
            raise ValueError("data_type_reports must be unique by data type and timeframe")


@dataclass(frozen=True, slots=True)
class DatasetDescriptor:
    dataset_id: str
    version: int
    dataset_hash: str
    manifest_uri: str
    storage_uri: str
    exchange_id: str
    market_type: MarketType
    instrument_id: str
    data_types: tuple[DataType, ...]
    timeframes: tuple[str, ...]
    start: datetime
    end: datetime
    schema_version: int
    validation_status: ValidationStatus
    unresolved_issues: tuple[str, ...]
    lineage: tuple[str, ...]
    compatible_consumers: tuple[str, ...] = (
        "modular_backtest",
        "modular_research_runner",
        "modular_results_registry",
        "strategy_factory",
    )
    descriptor_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _identifier(self.dataset_id, "dataset_id"))
        object.__setattr__(self, "dataset_hash", _sha(self.dataset_hash, "dataset_hash"))
        object.__setattr__(
            self,
            "manifest_uri",
            validate_relative_storage_uri(self.manifest_uri, "manifest_uri"),
        )
        object.__setattr__(
            self,
            "storage_uri",
            validate_source_uri(self.storage_uri, "storage_uri"),
        )
        object.__setattr__(self, "exchange_id", _identifier(self.exchange_id, "exchange_id"))
        object.__setattr__(self, "instrument_id", _identifier(self.instrument_id, "instrument_id"))
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        for name, value in (
            ("version", self.version),
            ("schema_version", self.schema_version),
            ("descriptor_version", self.descriptor_version),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        object.__setattr__(self, "start", utc_datetime(self.start))
        object.__setattr__(self, "end", utc_datetime(self.end))
        if self.start >= self.end:
            raise ValueError("descriptor start must precede end")
        if not isinstance(self.validation_status, ValidationStatus):
            raise TypeError("validation_status must be ValidationStatus")
        if not isinstance(self.data_types, tuple) or not self.data_types:
            raise TypeError("data_types must be a non-empty tuple")
        if any(not isinstance(item, DataType) for item in self.data_types):
            raise TypeError("data_types must contain DataType")
        if len(self.data_types) != len(set(self.data_types)):
            raise ValueError("data_types must be unique")
        if not isinstance(self.timeframes, tuple):
            raise TypeError("timeframes must be a tuple")
        normalized_timeframes = tuple(_text(item, "timeframe").lower() for item in self.timeframes)
        for item in normalized_timeframes:
            timeframe_seconds(item)
        if len(normalized_timeframes) != len(set(normalized_timeframes)):
            raise ValueError("timeframes must be unique")
        interval_types = {
            DataType.KLINES,
            DataType.MARK_PRICE,
            DataType.INDEX_PRICE,
            DataType.PREMIUM_INDEX,
            DataType.OPEN_INTEREST_HISTORY,
        }
        if interval_types.intersection(self.data_types) and not normalized_timeframes:
            raise ValueError("interval data types require at least one timeframe")
        object.__setattr__(self, "timeframes", normalized_timeframes)
        if not isinstance(self.unresolved_issues, tuple):
            raise TypeError("unresolved_issues must be a tuple")
        normalized_issues = tuple(
            _text(item, "unresolved issue") for item in self.unresolved_issues
        )
        if len(normalized_issues) != len(set(normalized_issues)):
            raise ValueError("unresolved_issues must be unique")
        object.__setattr__(self, "unresolved_issues", normalized_issues)
        if not isinstance(self.lineage, tuple):
            raise TypeError("lineage must be a tuple")
        normalized_lineage = tuple(_text(item, "lineage item") for item in self.lineage)
        object.__setattr__(self, "lineage", normalized_lineage)
        if not isinstance(self.compatible_consumers, tuple):
            raise TypeError("compatible_consumers must be a tuple")
        normalized_consumers = tuple(
            _identifier(item, "compatible consumer") for item in self.compatible_consumers
        )
        if not normalized_consumers:
            raise ValueError("compatible_consumers must not be empty")
        if len(normalized_consumers) != len(set(normalized_consumers)):
            raise ValueError("compatible_consumers must be unique")
        object.__setattr__(self, "compatible_consumers", normalized_consumers)
        if self.descriptor_version != 1:
            raise ValueError("unsupported descriptor version")


@dataclass(frozen=True, slots=True)
class DatasetRegistration:
    descriptor: DatasetDescriptor
    registered_at: datetime
    registry_uri: str

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, DatasetDescriptor):
            raise TypeError("descriptor must be DatasetDescriptor")
        object.__setattr__(self, "registered_at", utc_datetime(self.registered_at, "registered_at"))
        object.__setattr__(self, "registry_uri", _text(self.registry_uri, "registry_uri"))


@dataclass(frozen=True, slots=True)
class IngestionResumeSpec:
    """Immutable execution envelope required for deterministic CLI resume."""

    run_id: str
    execution_hash: str
    request: MarketDataRequest
    adapter_version: str
    storage_format: str
    compression: str
    reject_unresolved_required_gaps: bool
    created_at: datetime
    concurrency: int = 1
    max_records_in_memory: int = 5_000_000
    base_dataset: DatasetDescriptor | None = None
    base_manifest_override: DatasetManifest | None = None
    resume_spec_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "execution_hash", _sha(self.execution_hash, "execution_hash"))
        if not isinstance(self.request, MarketDataRequest):
            raise TypeError("request must be MarketDataRequest")
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter_version"))
        if self.storage_format not in {"csv", "parquet"}:
            raise ValueError("storage_format must be csv or parquet")
        object.__setattr__(self, "compression", _text(self.compression, "compression"))
        if not isinstance(self.reject_unresolved_required_gaps, bool):
            raise TypeError("reject_unresolved_required_gaps must be bool")
        if (
            not isinstance(self.concurrency, int)
            or isinstance(self.concurrency, bool)
            or self.concurrency <= 0
        ):
            raise ValueError("concurrency must be a positive integer")
        if (
            not isinstance(self.max_records_in_memory, int)
            or isinstance(self.max_records_in_memory, bool)
            or self.max_records_in_memory <= 0
        ):
            raise ValueError("max_records_in_memory must be a positive integer")
        object.__setattr__(self, "created_at", utc_datetime(self.created_at, "created_at"))
        if self.base_dataset is not None and not isinstance(self.base_dataset, DatasetDescriptor):
            raise TypeError("base_dataset must be DatasetDescriptor or None")
        if self.base_manifest_override is not None:
            if self.base_dataset is None:
                raise ValueError("base_manifest_override requires base_dataset")
            if not isinstance(self.base_manifest_override, DatasetManifest):
                raise TypeError("base_manifest_override must be DatasetManifest or None")
        if self.resume_spec_version not in {1, 2, 3}:
            raise ValueError("unsupported resume spec version")
        if self.resume_spec_version == 1 and self.concurrency != 1:
            raise ValueError("resume spec v1 supports concurrency=1 only")
        if self.resume_spec_version < 3 and self.max_records_in_memory != 5_000_000:
            raise ValueError("resume spec versions before v3 use the fixed default memory limit")


@dataclass(frozen=True, slots=True)
class IngestionRunManifest:
    run_id: str
    request: MarketDataRequest
    request_hash: str
    state: RunState
    adapter_version: str
    started_at: datetime
    updated_at: datetime
    checkpoint_uri: str
    run_manifest_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        if not isinstance(self.request, MarketDataRequest):
            raise TypeError("request must be MarketDataRequest")
        object.__setattr__(self, "request_hash", _sha(self.request_hash, "request_hash"))
        if self.request_hash != self.request.canonical_hash:
            raise ValueError("request_hash does not match request")
        if not isinstance(self.state, RunState):
            raise TypeError("state must be RunState")
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter_version"))
        object.__setattr__(self, "started_at", utc_datetime(self.started_at, "started_at"))
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at, "updated_at"))
        if self.updated_at < self.started_at:
            raise ValueError("updated_at cannot precede started_at")
        object.__setattr__(self, "checkpoint_uri", _text(self.checkpoint_uri, "checkpoint_uri"))
        if self.run_manifest_version != 1:
            raise ValueError("unsupported run manifest version")


@dataclass(frozen=True, slots=True)
class IngestionRunResult:
    run_id: str
    final_state: RunState
    dataset: DatasetDescriptor | None
    validation: DataValidationReport | None
    warnings: tuple[str, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        if not isinstance(self.final_state, RunState):
            raise TypeError("final_state must be RunState")
        object.__setattr__(self, "completed_at", utc_datetime(self.completed_at, "completed_at"))
        terminal_states = {
            RunState.COMPLETED,
            RunState.COMPLETED_WITH_WARNINGS,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.DATASET_REJECTED,
        }
        if self.final_state not in terminal_states:
            raise ValueError("final_state must be terminal")
