from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from .canonical import utc_datetime
from .enums import DataType
from .timeframes import timeframe_delta


def decimal_text(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return format(result, "f")


def parse_epoch(value: object, *, unit: str = "milliseconds") -> datetime:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("timestamp must be numeric")
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be an integer epoch") from exc
    divisors = {"seconds": 1, "milliseconds": 1_000, "microseconds": 1_000_000}
    if unit not in divisors:
        raise ValueError(f"unsupported timestamp unit: {unit}")
    try:
        return datetime.fromtimestamp(numeric / divisors[unit], tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp outside supported range") from exc


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def _validate_interval(open_time: datetime, close_time: datetime, timeframe: str) -> None:
    expected = timeframe_delta(timeframe)
    observed = close_time - open_time
    accepted = {
        expected,
        expected - timedelta(milliseconds=1),
        expected - timedelta(microseconds=1),
    }
    if observed not in accepted:
        raise ValueError("close_time does not match timeframe semantics")


def _validate_ohlc(open_value: str, high_value: str, low_value: str, close_value: str) -> None:
    open_price = Decimal(open_value)
    high = Decimal(high_value)
    low = Decimal(low_value)
    close = Decimal(close_value)
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        raise ValueError("invalid OHLC geometry")


@runtime_checkable
class MarketDataRecord(Protocol):
    symbol: str
    exchange_id: str
    market_type: str
    source_kind: str

    @property
    def data_type(self) -> DataType: ...

    @property
    def timestamp(self) -> datetime: ...

    @property
    def end_time(self) -> datetime: ...

    @property
    def record_timeframe(self) -> str | None: ...

    def to_row(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class KlineRecord:
    """Traded-price OHLCV candle.

    This record is intentionally distinct from mark/index price candles so volume
    fields can never be silently interpreted as traded volume for synthetic prices.
    """

    open_time: datetime
    close_time: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str
    quote_volume: str
    trade_count: int
    taker_buy_base_volume: str
    taker_buy_quote_volume: str
    symbol: str
    timeframe: str
    exchange_id: str
    market_type: str
    source_kind: str
    is_closed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_time", utc_datetime(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", utc_datetime(self.close_time, "close_time"))
        object.__setattr__(self, "open", decimal_text(self.open, "open", positive=True))
        object.__setattr__(self, "high", decimal_text(self.high, "high", positive=True))
        object.__setattr__(self, "low", decimal_text(self.low, "low", positive=True))
        object.__setattr__(self, "close", decimal_text(self.close, "close", positive=True))
        object.__setattr__(self, "volume", decimal_text(self.volume, "volume", nonnegative=True))
        object.__setattr__(
            self,
            "quote_volume",
            decimal_text(self.quote_volume, "quote_volume", nonnegative=True),
        )
        object.__setattr__(
            self,
            "taker_buy_base_volume",
            decimal_text(
                self.taker_buy_base_volume,
                "taker_buy_base_volume",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "taker_buy_quote_volume",
            decimal_text(
                self.taker_buy_quote_volume,
                "taker_buy_quote_volume",
                nonnegative=True,
            ),
        )
        if (
            not isinstance(self.trade_count, int)
            or isinstance(self.trade_count, bool)
            or self.trade_count < 0
        ):
            raise ValueError("trade_count must be a non-negative integer")
        for name in ("symbol", "timeframe", "exchange_id", "market_type", "source_kind"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        _validate_interval(self.open_time, self.close_time, self.timeframe)
        _validate_ohlc(self.open, self.high, self.low, self.close)

    @property
    def data_type(self) -> DataType:
        return DataType.KLINES

    @property
    def timestamp(self) -> datetime:
        return self.open_time

    @property
    def end_time(self) -> datetime:
        return self.close_time

    @property
    def record_timeframe(self) -> str:
        return self.timeframe

    def to_row(self) -> dict[str, Any]:
        return {
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trade_count": self.trade_count,
            "taker_buy_base_volume": self.taker_buy_base_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "source_kind": self.source_kind,
            "is_closed": self.is_closed,
        }

    def numeric_tuple(self) -> tuple[float, float, float, float, float]:
        values = (
            float(self.open),
            float(self.high),
            float(self.low),
            float(self.close),
            float(self.volume),
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("non-finite normalized record")
        return values


_PRICE_KLINE_TYPES = frozenset({DataType.MARK_PRICE, DataType.INDEX_PRICE, DataType.PREMIUM_INDEX})


@dataclass(frozen=True, slots=True)
class PriceKlineRecord:
    """OHLC candle for a non-traded price family such as mark or index price."""

    price_type: DataType
    open_time: datetime
    close_time: datetime
    open: str
    high: str
    low: str
    close: str
    symbol: str
    timeframe: str
    exchange_id: str
    market_type: str
    source_kind: str
    is_closed: bool = True

    def __post_init__(self) -> None:
        if self.price_type not in _PRICE_KLINE_TYPES:
            raise ValueError("price_type must be mark_price, index_price, or premium_index")
        object.__setattr__(self, "open_time", utc_datetime(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", utc_datetime(self.close_time, "close_time"))
        positive = self.price_type is not DataType.PREMIUM_INDEX
        object.__setattr__(self, "open", decimal_text(self.open, "open", positive=positive))
        object.__setattr__(self, "high", decimal_text(self.high, "high", positive=positive))
        object.__setattr__(self, "low", decimal_text(self.low, "low", positive=positive))
        object.__setattr__(self, "close", decimal_text(self.close, "close", positive=positive))
        for name in ("symbol", "timeframe", "exchange_id", "market_type", "source_kind"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        _validate_interval(self.open_time, self.close_time, self.timeframe)
        _validate_ohlc(self.open, self.high, self.low, self.close)

    @property
    def data_type(self) -> DataType:
        return self.price_type

    @property
    def timestamp(self) -> datetime:
        return self.open_time

    @property
    def end_time(self) -> datetime:
        return self.close_time

    @property
    def record_timeframe(self) -> str:
        return self.timeframe

    def to_row(self) -> dict[str, Any]:
        return {
            "data_type": self.price_type.value,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "source_kind": self.source_kind,
            "is_closed": self.is_closed,
        }


@dataclass(frozen=True, slots=True)
class FundingRateRecord:
    funding_time: datetime
    funding_rate: str
    mark_price: str | None
    symbol: str
    exchange_id: str
    market_type: str
    source_kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "funding_time", utc_datetime(self.funding_time, "funding_time"))
        object.__setattr__(self, "funding_rate", decimal_text(self.funding_rate, "funding_rate"))
        if self.mark_price is not None:
            object.__setattr__(
                self, "mark_price", decimal_text(self.mark_price, "mark_price", positive=True)
            )
        for name in ("symbol", "exchange_id", "market_type", "source_kind"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))

    @property
    def data_type(self) -> DataType:
        return DataType.FUNDING_RATE

    @property
    def timestamp(self) -> datetime:
        return self.funding_time

    @property
    def end_time(self) -> datetime:
        return self.funding_time

    @property
    def record_timeframe(self) -> None:
        return None

    def to_row(self) -> dict[str, Any]:
        return {
            "funding_time": self.funding_time,
            "funding_rate": self.funding_rate,
            "mark_price": self.mark_price,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class OpenInterestRecord:
    observed_at: datetime
    sum_open_interest: str
    sum_open_interest_value: str
    period: str
    symbol: str
    exchange_id: str
    market_type: str
    source_kind: str
    circulating_supply: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", utc_datetime(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "sum_open_interest",
            decimal_text(self.sum_open_interest, "sum_open_interest", nonnegative=True),
        )
        object.__setattr__(
            self,
            "sum_open_interest_value",
            decimal_text(
                self.sum_open_interest_value,
                "sum_open_interest_value",
                nonnegative=True,
            ),
        )
        if self.circulating_supply is not None:
            object.__setattr__(
                self,
                "circulating_supply",
                decimal_text(
                    self.circulating_supply,
                    "circulating_supply",
                    nonnegative=True,
                ),
            )
        timeframe_delta(self.period)
        for name in ("period", "symbol", "exchange_id", "market_type", "source_kind"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))

    @property
    def data_type(self) -> DataType:
        return DataType.OPEN_INTEREST_HISTORY

    @property
    def timestamp(self) -> datetime:
        return self.observed_at

    @property
    def end_time(self) -> datetime:
        return self.observed_at + timeframe_delta(self.period)

    @property
    def record_timeframe(self) -> str:
        return self.period

    def to_row(self) -> dict[str, Any]:
        return {
            "timestamp": self.observed_at,
            "sum_open_interest": self.sum_open_interest,
            "sum_open_interest_value": self.sum_open_interest_value,
            "circulating_supply": self.circulating_supply,
            "period": self.period,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class InstrumentMetadataRecord:
    effective_at: datetime
    symbol: str
    exchange_id: str
    market_type: str
    source_kind: str
    canonical_id: str
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_at", utc_datetime(self.effective_at, "effective_at"))
        for name in (
            "symbol",
            "exchange_id",
            "market_type",
            "source_kind",
            "canonical_id",
            "base_asset",
            "quote_asset",
            "contract_type",
            "trading_status",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.settlement_asset is not None:
            object.__setattr__(
                self, "settlement_asset", _identity(self.settlement_asset, "settlement_asset")
            )
        if self.listing_date is not None:
            object.__setattr__(
                self, "listing_date", utc_datetime(self.listing_date, "listing_date")
            )
        if self.delisting_date is not None:
            object.__setattr__(
                self, "delisting_date", utc_datetime(self.delisting_date, "delisting_date")
            )
        object.__setattr__(
            self,
            "tick_size",
            decimal_text(self.tick_size, "tick_size", nonnegative=True),
        )
        object.__setattr__(
            self,
            "quantity_step",
            decimal_text(self.quantity_step, "quantity_step", nonnegative=True),
        )
        object.__setattr__(
            self,
            "contract_multiplier",
            decimal_text(self.contract_multiplier, "contract_multiplier", positive=True),
        )

    @property
    def data_type(self) -> DataType:
        return DataType.INSTRUMENT_METADATA

    @property
    def timestamp(self) -> datetime:
        return self.effective_at

    @property
    def end_time(self) -> datetime:
        return self.effective_at

    @property
    def record_timeframe(self) -> None:
        return None

    def to_row(self) -> dict[str, Any]:
        return {
            "effective_at": self.effective_at,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "source_kind": self.source_kind,
            "canonical_id": self.canonical_id,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "settlement_asset": self.settlement_asset,
            "contract_type": self.contract_type,
            "listing_date": self.listing_date,
            "delisting_date": self.delisting_date,
            "tick_size": self.tick_size,
            "quantity_step": self.quantity_step,
            "contract_multiplier": self.contract_multiplier,
            "trading_status": self.trading_status,
        }


@dataclass(frozen=True, slots=True)
class ExchangeMetadataRecord:
    effective_at: datetime
    symbol: str
    exchange_id: str
    market_type: str
    source_kind: str
    adapter_version: str
    maturity: str
    supported_markets: tuple[str, ...]
    supported_data_types: tuple[str, ...]
    rate_limit_policy_json: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_at", utc_datetime(self.effective_at, "effective_at"))
        for name in (
            "symbol",
            "exchange_id",
            "market_type",
            "source_kind",
            "adapter_version",
            "maturity",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        markets = tuple(
            sorted(_identity(item, "supported_market") for item in self.supported_markets)
        )
        data_types = tuple(
            sorted(_identity(item, "supported_data_type") for item in self.supported_data_types)
        )
        if len(markets) != len(set(markets)) or len(data_types) != len(set(data_types)):
            raise ValueError("metadata capability lists must be unique")
        object.__setattr__(self, "supported_markets", markets)
        object.__setattr__(self, "supported_data_types", data_types)
        try:
            policy = json.loads(self.rate_limit_policy_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("rate_limit_policy_json must be valid JSON") from exc
        object.__setattr__(
            self,
            "rate_limit_policy_json",
            json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False),
        )

    @property
    def data_type(self) -> DataType:
        return DataType.EXCHANGE_METADATA

    @property
    def timestamp(self) -> datetime:
        return self.effective_at

    @property
    def end_time(self) -> datetime:
        return self.effective_at

    @property
    def record_timeframe(self) -> None:
        return None

    def to_row(self) -> dict[str, Any]:
        return {
            "effective_at": self.effective_at,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "source_kind": self.source_kind,
            "adapter_version": self.adapter_version,
            "maturity": self.maturity,
            "supported_markets": self.supported_markets,
            "supported_data_types": self.supported_data_types,
            "rate_limit_policy_json": self.rate_limit_policy_json,
        }


type NormalizedRecord = (
    KlineRecord
    | PriceKlineRecord
    | FundingRateRecord
    | OpenInterestRecord
    | InstrumentMetadataRecord
    | ExchangeMetadataRecord
)


_NORMALIZED_RECORD_FIELDS: dict[DataType, tuple[str, ...]] = {
    DataType.KLINES: (
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "symbol",
        "timeframe",
        "exchange_id",
        "market_type",
        "source_kind",
        "is_closed",
    ),
    DataType.MARK_PRICE: (
        "data_type",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "symbol",
        "timeframe",
        "exchange_id",
        "market_type",
        "source_kind",
        "is_closed",
    ),
    DataType.INDEX_PRICE: (
        "data_type",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "symbol",
        "timeframe",
        "exchange_id",
        "market_type",
        "source_kind",
        "is_closed",
    ),
    DataType.PREMIUM_INDEX: (
        "data_type",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "symbol",
        "timeframe",
        "exchange_id",
        "market_type",
        "source_kind",
        "is_closed",
    ),
    DataType.FUNDING_RATE: (
        "funding_time",
        "funding_rate",
        "mark_price",
        "symbol",
        "exchange_id",
        "market_type",
        "source_kind",
    ),
    DataType.OPEN_INTEREST_HISTORY: (
        "timestamp",
        "sum_open_interest",
        "sum_open_interest_value",
        "circulating_supply",
        "period",
        "symbol",
        "exchange_id",
        "market_type",
        "source_kind",
    ),
    DataType.INSTRUMENT_METADATA: (
        "effective_at",
        "symbol",
        "exchange_id",
        "market_type",
        "source_kind",
        "canonical_id",
        "base_asset",
        "quote_asset",
        "settlement_asset",
        "contract_type",
        "listing_date",
        "delisting_date",
        "tick_size",
        "quantity_step",
        "contract_multiplier",
        "trading_status",
    ),
    DataType.EXCHANGE_METADATA: (
        "effective_at",
        "symbol",
        "exchange_id",
        "market_type",
        "source_kind",
        "adapter_version",
        "maturity",
        "supported_markets",
        "supported_data_types",
        "rate_limit_policy_json",
    ),
}


def normalized_record_fieldnames(data_type: DataType) -> tuple[str, ...]:
    """Return the exact ordered normalized schema for a data family."""

    if not isinstance(data_type, DataType):
        raise TypeError("data_type must be DataType")
    try:
        return _NORMALIZED_RECORD_FIELDS[data_type]
    except KeyError as exc:
        raise ValueError(f"unsupported normalized record data type: {data_type.value}") from exc


def _row_datetime(row: dict[str, Any], name: str) -> datetime:
    value = row.get(name)
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc


def _optional_row_datetime(row: dict[str, Any], name: str) -> datetime | None:
    value = row.get(name)
    if value in {None, ""}:
        return None
    return _row_datetime(row, name)


def _row_string(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_row_string(row: dict[str, Any], name: str) -> str | None:
    value = row.get(name)
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _row_bool(row: dict[str, Any], name: str) -> bool:
    value = row.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _row_string_tuple(row: dict[str, Any], name: str) -> tuple[str, ...]:
    value = row.get(name)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} must be a JSON array of strings") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return tuple(value)


def normalized_record_from_row(data_type: DataType, row: dict[str, Any]) -> NormalizedRecord:
    """Restore a normalized record from the strict JSON row representation.

    This codec is intentionally explicit and never uses pickle or dynamic class
    loading, because staging files can outlive a crashed process.
    """

    if not isinstance(data_type, DataType):
        raise TypeError("data_type must be DataType")
    if not isinstance(row, dict):
        raise TypeError("row must be a mapping")
    common = {
        "symbol": _row_string(row, "symbol"),
        "exchange_id": _row_string(row, "exchange_id"),
        "market_type": _row_string(row, "market_type"),
        "source_kind": _row_string(row, "source_kind"),
    }
    if data_type is DataType.KLINES:
        trade_count_value = row.get("trade_count")
        if trade_count_value is None or isinstance(trade_count_value, bool):
            raise ValueError("trade_count must be an integer")
        try:
            trade_count = int(trade_count_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("trade_count must be an integer") from exc
        if str(trade_count) != str(trade_count_value).strip():
            raise ValueError("trade_count must be an integer")
        return KlineRecord(
            open_time=_row_datetime(row, "open_time"),
            close_time=_row_datetime(row, "close_time"),
            open=_row_string(row, "open"),
            high=_row_string(row, "high"),
            low=_row_string(row, "low"),
            close=_row_string(row, "close"),
            volume=_row_string(row, "volume"),
            quote_volume=_row_string(row, "quote_volume"),
            trade_count=trade_count,
            taker_buy_base_volume=_row_string(row, "taker_buy_base_volume"),
            taker_buy_quote_volume=_row_string(row, "taker_buy_quote_volume"),
            timeframe=_row_string(row, "timeframe"),
            is_closed=_row_bool(row, "is_closed"),
            **common,
        )
    if data_type in _PRICE_KLINE_TYPES:
        encoded_type = row.get("data_type")
        if encoded_type != data_type.value:
            raise ValueError("price row data_type does not match the requested codec")
        return PriceKlineRecord(
            price_type=data_type,
            open_time=_row_datetime(row, "open_time"),
            close_time=_row_datetime(row, "close_time"),
            open=_row_string(row, "open"),
            high=_row_string(row, "high"),
            low=_row_string(row, "low"),
            close=_row_string(row, "close"),
            timeframe=_row_string(row, "timeframe"),
            is_closed=_row_bool(row, "is_closed"),
            **common,
        )
    if data_type is DataType.FUNDING_RATE:
        return FundingRateRecord(
            funding_time=_row_datetime(row, "funding_time"),
            funding_rate=_row_string(row, "funding_rate"),
            mark_price=_optional_row_string(row, "mark_price"),
            **common,
        )
    if data_type is DataType.OPEN_INTEREST_HISTORY:
        return OpenInterestRecord(
            observed_at=_row_datetime(row, "timestamp"),
            sum_open_interest=_row_string(row, "sum_open_interest"),
            sum_open_interest_value=_row_string(row, "sum_open_interest_value"),
            circulating_supply=_optional_row_string(row, "circulating_supply"),
            period=_row_string(row, "period"),
            **common,
        )
    if data_type is DataType.INSTRUMENT_METADATA:
        return InstrumentMetadataRecord(
            effective_at=_row_datetime(row, "effective_at"),
            canonical_id=_row_string(row, "canonical_id"),
            base_asset=_row_string(row, "base_asset"),
            quote_asset=_row_string(row, "quote_asset"),
            settlement_asset=_optional_row_string(row, "settlement_asset"),
            contract_type=_row_string(row, "contract_type"),
            listing_date=_optional_row_datetime(row, "listing_date"),
            delisting_date=_optional_row_datetime(row, "delisting_date"),
            tick_size=_row_string(row, "tick_size"),
            quantity_step=_row_string(row, "quantity_step"),
            contract_multiplier=_row_string(row, "contract_multiplier"),
            trading_status=_row_string(row, "trading_status"),
            **common,
        )
    if data_type is DataType.EXCHANGE_METADATA:
        return ExchangeMetadataRecord(
            effective_at=_row_datetime(row, "effective_at"),
            adapter_version=_row_string(row, "adapter_version"),
            maturity=_row_string(row, "maturity"),
            supported_markets=_row_string_tuple(row, "supported_markets"),
            supported_data_types=_row_string_tuple(row, "supported_data_types"),
            rate_limit_policy_json=_row_string(row, "rate_limit_policy_json"),
            **common,
        )
    raise ValueError(f"unsupported normalized record data type: {data_type.value}")
