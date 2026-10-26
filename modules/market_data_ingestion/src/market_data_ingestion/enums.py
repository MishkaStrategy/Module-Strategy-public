from __future__ import annotations

from enum import StrEnum


class MarketType(StrEnum):
    SPOT = "spot"
    MARGIN = "margin"
    USDT_M_PERPETUAL = "usdt_m_perpetual"
    COIN_M_PERPETUAL = "coin_m_perpetual"
    DELIVERY_FUTURES = "delivery_futures"
    OPTIONS = "options"


class DataType(StrEnum):
    KLINES = "klines"
    INSTRUMENT_METADATA = "instrument_metadata"
    EXCHANGE_METADATA = "exchange_metadata"
    FUNDING_RATE = "funding_rate"
    MARK_PRICE = "mark_price"
    INDEX_PRICE = "index_price"
    PREMIUM_INDEX = "premium_index"
    OPEN_INTEREST = "open_interest"
    OPEN_INTEREST_HISTORY = "open_interest_history"
    TRADES = "trades"
    AGGREGATE_TRADES = "aggregate_trades"
    LIQUIDATIONS = "liquidations"


class SourceKind(StrEnum):
    OFFICIAL_ARCHIVE = "official_archive"
    OFFICIAL_REST = "official_rest"
    OFFICIAL_EXPORT = "official_export"


class AdapterMaturity(StrEnum):
    EXPERIMENTAL = "experimental"
    TESTED = "tested"
    PRODUCTION = "production"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"
    REJECTED = "REJECTED"


class RepairStatus(StrEnum):
    REPAIRED = "REPAIRED"
    PARTIALLY_REPAIRED = "PARTIALLY_REPAIRED"
    UNRESOLVED = "UNRESOLVED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"


class GapReason(StrEnum):
    NOT_YET_LISTED = "instrument_not_yet_listed"
    DELISTED = "instrument_delisted"
    TRADING_HALT = "official_trading_halt"
    MAINTENANCE = "technical_maintenance"
    SOURCE_NOT_PUBLISHED = "source_not_published"
    API_FAILURE = "api_temporary_failure"
    LOCAL_DATA_LOSS = "local_data_loss"
    UNKNOWN = "unknown"


class RunState(StrEnum):
    CREATED = "CREATED"
    VALIDATING_REQUEST = "VALIDATING_REQUEST"
    DISCOVERING_CAPABILITIES = "DISCOVERING_CAPABILITIES"
    RESOLVING_INSTRUMENT = "RESOLVING_INSTRUMENT"
    PLANNING = "PLANNING"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING_RAW = "VERIFYING_RAW"
    NORMALIZING = "NORMALIZING"
    VALIDATING_DATA = "VALIDATING_DATA"
    REPAIRING = "REPAIRING"
    BUILDING_DERIVED = "BUILDING_DERIVED"
    HASHING = "HASHING"
    REGISTERING = "REGISTERING"
    EXPORTING = "EXPORTING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    DATASET_REJECTED = "DATASET_REJECTED"
