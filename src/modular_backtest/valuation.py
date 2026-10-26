from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.data import timeframe_to_seconds
from modular_core.execution import BrokerSimulator, Portfolio
from modular_core.instruments import InstrumentSpec
from modular_core.models import Candle, Order, OrderRequest, PositionSnapshot, PositionUpdateRequest

from .funding import RegisteredFundingSource
from .futures_execution import FundingAwareBrokerSimulator

VALUATION_POLICY_LAST = "last"
VALUATION_POLICY_MARK = "mark"
MARK_VALUATION_MODEL_VERSION = "registered-mark-reference-v1"
VALUATION_APPLICATION_IDENTITY_VERSION = "1"
_ALLOWED_POLICIES = frozenset({VALUATION_POLICY_LAST, VALUATION_POLICY_MARK})


class MarkValuationError(ValueError):
    """Raised when a mark-valued Backtest run cannot be proven safely."""


@dataclass(frozen=True, slots=True)
class PriceLineageEntry:
    artifact_id: str
    source_kind: str
    checksum_sha256: str
    record_count: int
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise MarkValuationError("price lineage artifact_id must be non-empty")
        if not isinstance(self.source_kind, str) or not self.source_kind:
            raise MarkValuationError("price lineage source_kind must be non-empty")
        if (
            not isinstance(self.checksum_sha256, str)
            or len(self.checksum_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.checksum_sha256)
        ):
            raise MarkValuationError("price lineage checksum must be lowercase SHA-256")
        if (
            not isinstance(self.record_count, int)
            or isinstance(self.record_count, bool)
            or self.record_count <= 0
        ):
            raise MarkValuationError("price lineage record_count must be positive")
        start = _utc_datetime(self.start, "price lineage start")
        end = _utc_datetime(self.end, "price lineage end")
        if end <= start:
            raise MarkValuationError("price lineage interval must be non-empty")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def to_payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "source_kind": self.source_kind,
            "checksum_sha256": self.checksum_sha256,
            "record_count": self.record_count,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RegisteredMarkPriceSource:
    """Compact immutable mark-price window adapted from the Market Data owner."""

    dataset_id: str
    dataset_version: int
    dataset_hash: str
    symbol: str
    exchange_id: str
    market_type: str
    timeframe: str
    window_start_inclusive: datetime
    window_end_exclusive: datetime
    owner_row_count: int
    owner_source_record_count: int
    lineage: tuple[PriceLineageEntry, ...]
    mark_opens: tuple[float, ...]
    mark_closes: tuple[float, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, str) or not self.dataset_id:
            raise MarkValuationError("mark dataset_id must be non-empty")
        if (
            not isinstance(self.dataset_version, int)
            or isinstance(self.dataset_version, bool)
            or self.dataset_version <= 0
        ):
            raise MarkValuationError("mark dataset_version must be positive")
        if (
            not isinstance(self.dataset_hash, str)
            or len(self.dataset_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.dataset_hash)
        ):
            raise MarkValuationError("mark dataset_hash must be lowercase SHA-256")
        for name in ("symbol", "exchange_id", "market_type", "timeframe"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise MarkValuationError(f"mark {name} must be non-empty")
        if self.exchange_id != "binance" or self.market_type != "usdt_m_perpetual":
            raise MarkValuationError("mark source must be Binance USDT-M perpetual")
        seconds = timeframe_to_seconds(self.timeframe)
        if seconds is None or seconds <= 0:
            raise MarkValuationError("mark timeframe is unsupported")
        start = _utc_datetime(self.window_start_inclusive, "mark window start")
        end = _utc_datetime(self.window_end_exclusive, "mark window end")
        if end <= start:
            raise MarkValuationError("mark window must be non-empty")
        object.__setattr__(self, "window_start_inclusive", start)
        object.__setattr__(self, "window_end_exclusive", end)
        if self.schema_version != 1:
            raise MarkValuationError("unsupported registered mark source schema")
        for name in ("owner_row_count", "owner_source_record_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise MarkValuationError(f"mark {name} must be positive")
        if self.owner_row_count != self.owner_source_record_count:
            raise MarkValuationError("owner mark normalized/source counts differ")

        lineage = tuple(self.lineage)
        opens = tuple(float(value) for value in self.mark_opens)
        closes = tuple(float(value) for value in self.mark_closes)
        object.__setattr__(self, "lineage", lineage)
        object.__setattr__(self, "mark_opens", opens)
        object.__setattr__(self, "mark_closes", closes)
        if not lineage:
            raise MarkValuationError("mark source requires immutable raw lineage")
        if any(not isinstance(item, PriceLineageEntry) for item in lineage):
            raise MarkValuationError("mark lineage must contain PriceLineageEntry values")
        ordered = tuple(sorted(lineage, key=lambda item: (item.start, item.end, item.artifact_id)))
        if ordered != lineage:
            raise MarkValuationError("mark lineage must be canonically ordered")
        if sum(item.record_count for item in lineage) != self.owner_source_record_count:
            raise MarkValuationError("mark lineage counts differ from owner source count")
        covered_until = lineage[0].end
        for current in lineage[1:]:
            if current.start > covered_until:
                raise MarkValuationError("mark lineage contains an uncovered interval")
            if current.end > covered_until:
                covered_until = current.end
        if lineage[0].start > start or covered_until < end:
            raise MarkValuationError("mark lineage does not cover the selected run window")

        if not opens or len(opens) != len(closes):
            raise MarkValuationError("mark source requires equal non-empty open/close series")
        if len(opens) > self.owner_row_count:
            raise MarkValuationError("selected mark window exceeds owner dataset row evidence")
        if any(not math.isfinite(value) or value <= 0 for value in (*opens, *closes)):
            raise MarkValuationError("mark prices must be finite and positive")
        expected_end = start + timedelta(seconds=seconds * len(opens))
        if expected_end != end:
            raise MarkValuationError("mark row count does not exactly cover the selected window")

    @property
    def row_count(self) -> int:
        return len(self.mark_closes)

    @property
    def interval_seconds(self) -> int:
        value = timeframe_to_seconds(self.timeframe)
        if value is None:
            raise MarkValuationError("mark timeframe is unsupported")
        return value

    def identity(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "model_version": MARK_VALUATION_MODEL_VERSION,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_hash": self.dataset_hash,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "data_type": "mark_price",
            "timeframe": self.timeframe,
            "window_start_inclusive": self.window_start_inclusive.isoformat(),
            "window_end_exclusive": self.window_end_exclusive.isoformat(),
            "coverage_semantics": "[start,end)",
            "row_count": self.row_count,
            "owner_row_count": self.owner_row_count,
            "owner_source_record_count": self.owner_source_record_count,
            "lineage": [item.to_payload() for item in self.lineage],
        }
        return MappingProxyType(payload)

    def identity_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(dict(self.identity()))).hexdigest()

    def assert_matches_candles(
        self,
        first: Candle,
        last: Candle,
        total_bars: int,
    ) -> None:
        if not isinstance(total_bars, int) or isinstance(total_bars, bool) or total_bars <= 0:
            raise MarkValuationError("candle total_bars must be positive")
        if first.symbol != self.symbol or last.symbol != self.symbol:
            raise MarkValuationError("mark source symbol differs from candle source")
        if first.timeframe != self.timeframe or last.timeframe != self.timeframe:
            raise MarkValuationError("mark source timeframe differs from candle source")
        if total_bars != self.row_count:
            raise MarkValuationError("mark source row count differs from candle source")
        if first.timestamp != self.window_start_inclusive:
            raise MarkValuationError("mark source start differs from candle source")
        expected_last = self.window_end_exclusive - timedelta(seconds=self.interval_seconds)
        if last.timestamp != expected_last:
            raise MarkValuationError("mark source end differs from candle source")

    def mark_open(self, bar: Candle) -> float:
        return self.mark_opens[self._index_for_bar(bar)]

    def mark_close(self, bar: Candle) -> float:
        return self.mark_closes[self._index_for_bar(bar)]

    def mark_close_at(self, timestamp: datetime) -> float:
        return self.mark_closes[self._index_for_timestamp(timestamp)]

    def _index_for_bar(self, bar: Candle) -> int:
        if bar.symbol != self.symbol:
            raise MarkValuationError("processed candle symbol differs from mark source")
        if bar.timeframe != self.timeframe:
            raise MarkValuationError("processed candle timeframe differs from mark source")
        return self._index_for_timestamp(bar.timestamp)

    def _index_for_timestamp(self, timestamp: datetime) -> int:
        value = _utc_datetime(timestamp, "mark lookup timestamp")
        offset = value - self.window_start_inclusive
        if offset < timedelta(0):
            raise MarkValuationError("mark lookup precedes selected run window")
        step = timedelta(seconds=self.interval_seconds)
        index = offset // step
        remainder = offset % step
        if remainder != timedelta(0):
            raise MarkValuationError("mark lookup is not aligned to the registered timeframe")
        if index < 0 or index >= self.row_count or value >= self.window_end_exclusive:
            raise MarkValuationError("mark lookup exceeds selected run window")
        return int(index)


def normalize_valuation_policy(value: str) -> str:
    if not isinstance(value, str):
        raise MarkValuationError("valuation_policy must be a string")
    policy = value.strip().lower()
    if policy not in _ALLOWED_POLICIES:
        raise MarkValuationError("valuation_policy must be last or mark")
    return policy


def validate_valuation_inputs(
    policy: str,
    mark_source: RegisteredMarkPriceSource | None,
) -> str:
    normalized = normalize_valuation_policy(policy)
    if normalized == VALUATION_POLICY_LAST and mark_source is not None:
        raise MarkValuationError("last valuation policy must not receive a mark source")
    if normalized == VALUATION_POLICY_MARK and mark_source is None:
        raise MarkValuationError("mark valuation policy requires a registered mark source")
    return normalized


def valuation_application_run_id(
    base_application_id: str,
    policy: str,
    mark_source: RegisteredMarkPriceSource | None,
) -> str:
    normalized = validate_valuation_inputs(policy, mark_source)
    if not isinstance(base_application_id, str) or not base_application_id.strip():
        raise MarkValuationError("base application run_id must be non-empty")
    if normalized == VALUATION_POLICY_LAST:
        return base_application_id
    if mark_source is None:
        raise MarkValuationError("mark valuation source disappeared")
    payload = {
        "schema_version": VALUATION_APPLICATION_IDENTITY_VERSION,
        "base_application_run_id": base_application_id,
        "valuation_policy": normalized,
        "valuation_model": MARK_VALUATION_MODEL_VERSION,
        "mark_source_identity_sha256": mark_source.identity_sha256(),
    }
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return f"{base_application_id}-mark-{digest[:20]}"


def valuation_application_metadata(
    policy: str,
    mark_source: RegisteredMarkPriceSource | None,
) -> dict[str, object]:
    normalized = validate_valuation_inputs(policy, mark_source)
    if normalized == VALUATION_POLICY_LAST:
        return {
            "valuation_policy": VALUATION_POLICY_LAST,
            "valuation_model": "traded-last-baseline",
            "mark_source_identity_sha256": None,
            "mark_source": None,
            "execution_price_source": "traded_ohlc",
            "execution_risk_reference": "traded_open",
            "post_bar_reference": "traded_close",
        }
    if mark_source is None:
        raise MarkValuationError("mark valuation source disappeared")
    return {
        "valuation_policy": VALUATION_POLICY_MARK,
        "valuation_model": MARK_VALUATION_MODEL_VERSION,
        "mark_source_identity_sha256": mark_source.identity_sha256(),
        "mark_source": dict(mark_source.identity()),
        "execution_price_source": "traded_ohlc",
        "execution_risk_reference": "mark_open",
        "post_bar_reference": "mark_close",
    }


def valuation_runtime_evidence(
    application_id: str,
    policy: str,
    mark_source: RegisteredMarkPriceSource | None,
) -> Mapping[str, object]:
    if not isinstance(application_id, str) or not application_id:
        raise MarkValuationError("application_id must be non-empty")
    metadata = valuation_application_metadata(policy, mark_source)
    return MappingProxyType(
        {
            "schema_version": "1",
            "application_run_id": application_id,
            **metadata,
        }
    )


def open_registered_mark_price_source(
    storage_root: Path,
    dataset_id: str,
    dataset_version: int,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> RegisteredMarkPriceSource:
    """Adapt an exact registered MARK_PRICE window without moving Market Data ownership."""

    try:
        from market_data_ingestion.enums import DataType
        from market_data_ingestion.errors import StructuredIngestionError
        from market_data_ingestion.price_dataset_reader import (
            iter_registered_prices,
            resolve_registered_price_dataset,
        )
    except ImportError as exc:
        raise MarkValuationError(
            "registered mark support requires modular-market-data-ingestion with parquet support"
        ) from exc

    try:
        selection = resolve_registered_price_dataset(
            storage_root,
            dataset_id,
            dataset_version,
            price_type=DataType.MARK_PRICE,
            timeframe=timeframe,
            consumer="modular_backtest",
            deep=True,
        )
        records = tuple(iter_registered_prices(selection, start=start, end=end))
        return adapt_registered_mark_price_selection(
            selection,
            records,
            expected_symbol=symbol,
            start=start,
            end=end,
        )
    except MarkValuationError:
        raise
    except (StructuredIngestionError, OSError, TypeError, ValueError):
        pass
    raise MarkValuationError("registered mark validation failed")


def adapt_registered_mark_price_selection(
    selection: Any,
    records: Iterable[Any],
    *,
    expected_symbol: str,
    start: datetime,
    end: datetime,
) -> RegisteredMarkPriceSource:
    """Pure adapter used by the owner read path and focused contract tests."""

    try:
        from market_data_ingestion.enums import DataType
        from market_data_ingestion.records import PriceKlineRecord
    except ImportError as exc:
        raise MarkValuationError("registered mark adapter requires Market Data contracts") from exc

    if selection.price_type is not DataType.MARK_PRICE:
        raise MarkValuationError("registered price selection is not MARK_PRICE")
    if str(selection.manifest.instrument.symbol) != expected_symbol:
        raise MarkValuationError("registered mark symbol differs from backtest configuration")

    timeframe = str(selection.timeframe)
    seconds = timeframe_to_seconds(timeframe)
    if seconds is None:
        raise MarkValuationError("registered mark timeframe is unsupported")
    normalized_start = _utc_datetime(start, "requested mark start")
    normalized_end = _utc_datetime(end, "requested mark end")
    if normalized_end <= normalized_start:
        raise MarkValuationError("requested mark window must be non-empty")

    lineage = tuple(
        sorted(
            (
                PriceLineageEntry(
                    artifact_id=str(item.artifact_id),
                    source_kind=str(item.source.value),
                    checksum_sha256=str(item.checksum_sha256),
                    record_count=_required_record_count(item.record_count),
                    start=item.start,
                    end=item.end,
                )
                for item in selection.source_artifacts
            ),
            key=lambda item: (item.start, item.end, item.artifact_id),
        )
    )
    opens: list[float] = []
    closes: list[float] = []
    for index, record in enumerate(records):
        if not isinstance(record, PriceKlineRecord) or record.data_type is not DataType.MARK_PRICE:
            raise MarkValuationError("registered mark adapter received a non-mark record")
        if record.symbol != expected_symbol:
            raise MarkValuationError(
                "registered mark row symbol differs from backtest configuration"
            )
        if record.timeframe != timeframe:
            raise MarkValuationError("registered mark row timeframe differs from owner selection")
        expected_timestamp = normalized_start + timedelta(seconds=seconds * index)
        if record.open_time != expected_timestamp:
            raise MarkValuationError(
                "registered mark rows are not exactly aligned to the run window"
            )
        opens.append(_finite_positive_float(record.open, "mark open"))
        closes.append(_finite_positive_float(record.close, "mark close"))
    if normalized_start + timedelta(seconds=seconds * len(opens)) != normalized_end:
        raise MarkValuationError(
            "registered mark adapter did not consume the exact requested window"
        )

    return RegisteredMarkPriceSource(
        dataset_id=str(selection.descriptor.dataset_id),
        dataset_version=int(selection.descriptor.version),
        dataset_hash=str(selection.descriptor.dataset_hash),
        symbol=expected_symbol,
        exchange_id=str(selection.manifest.exchange.id),
        market_type=str(selection.manifest.market_type.value),
        timeframe=timeframe,
        window_start_inclusive=normalized_start,
        window_end_exclusive=normalized_end,
        owner_row_count=int(selection.row_count),
        owner_source_record_count=int(selection.source_record_count),
        lineage=lineage,
        mark_opens=tuple(opens),
        mark_closes=tuple(closes),
    )


@dataclass(slots=True)
class MarkAwarePortfolio(Portfolio):
    """Portfolio that projects equity from the current owner-backed mark reference."""

    reference_price: float | None = None

    def set_reference_price(self, value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise MarkValuationError("portfolio mark reference must be finite and positive")
        self.reference_price = value

    def _effective_price(self, supplied: float) -> float:
        if self.reference_price is None:
            return supplied
        return self.reference_price

    def unrealized_pnl(self, mark_price: float) -> float:
        return Portfolio.unrealized_pnl(self, self._effective_price(mark_price))

    def equity(self, mark_price: float) -> float:
        return Portfolio.equity(self, self._effective_price(mark_price))

    def position_snapshot(self, mark_price: float) -> PositionSnapshot | None:
        return Portfolio.position_snapshot(self, self._effective_price(mark_price))


class MarkAwareBrokerSimulator(BrokerSimulator):
    """Core broker with traded OHLC execution and owner-backed mark references."""

    def __init__(
        self,
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
        *,
        mark_source: RegisteredMarkPriceSource,
    ) -> None:
        super().__init__(config, journal, instrument)
        self.mark_source = mark_source
        self.portfolio = MarkAwarePortfolio(
            cash=self.portfolio.cash,
            position=self.portfolio.position,
            contract_size=self.portfolio.contract_size,
        )

    @property
    def _mark_portfolio(self) -> MarkAwarePortfolio:
        portfolio = self.portfolio
        if not isinstance(portfolio, MarkAwarePortfolio):
            raise MarkValuationError("mark-aware broker lost its portfolio contract")
        return portfolio

    def process_bar(self, bar: Candle, index: int) -> None:
        self._mark_portfolio.set_reference_price(self.mark_source.mark_open(bar))
        super().process_bar(bar, index)
        self._mark_portfolio.set_reference_price(self.mark_source.mark_close(bar))

    def submit(
        self,
        request: OrderRequest,
        *,
        source_module: str,
        created_at: datetime,
        current_index: int,
        mark_price: float,
    ) -> Order | None:
        return super().submit(
            request,
            source_module=source_module,
            created_at=created_at,
            current_index=current_index,
            mark_price=self.mark_source.mark_close_at(created_at),
        )

    def update_position(
        self,
        request: PositionUpdateRequest,
        *,
        timestamp: datetime,
        source_module: str,
        mark_price: float,
    ) -> bool:
        return super().update_position(
            request,
            timestamp=timestamp,
            source_module=source_module,
            mark_price=self.mark_source.mark_close_at(timestamp),
        )


class FundingMarkAwareBrokerSimulator(FundingAwareBrokerSimulator):
    """Funding V2 broker composed with the same deterministic mark-reference policy."""

    def __init__(
        self,
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
        *,
        funding_source: RegisteredFundingSource,
        mark_source: RegisteredMarkPriceSource,
    ) -> None:
        super().__init__(config, journal, instrument, funding_source=funding_source)
        self.mark_source = mark_source
        self.portfolio = MarkAwarePortfolio(
            cash=self.portfolio.cash,
            position=self.portfolio.position,
            contract_size=self.portfolio.contract_size,
        )

    @property
    def _mark_portfolio(self) -> MarkAwarePortfolio:
        portfolio = self.portfolio
        if not isinstance(portfolio, MarkAwarePortfolio):
            raise MarkValuationError("funding mark-aware broker lost its portfolio contract")
        return portfolio

    def process_bar(self, bar: Candle, index: int) -> None:
        self._mark_portfolio.set_reference_price(self.mark_source.mark_open(bar))
        super().process_bar(bar, index)
        self._mark_portfolio.set_reference_price(self.mark_source.mark_close(bar))

    def submit(
        self,
        request: OrderRequest,
        *,
        source_module: str,
        created_at: datetime,
        current_index: int,
        mark_price: float,
    ) -> Order | None:
        return super().submit(
            request,
            source_module=source_module,
            created_at=created_at,
            current_index=current_index,
            mark_price=self.mark_source.mark_close_at(created_at),
        )

    def update_position(
        self,
        request: PositionUpdateRequest,
        *,
        timestamp: datetime,
        source_module: str,
        mark_price: float,
    ) -> bool:
        return super().update_position(
            request,
            timestamp=timestamp,
            source_module=source_module,
            mark_price=self.mark_source.mark_close_at(timestamp),
        )


ExecutionFactory = Callable[[EngineConfig, Journal, InstrumentSpec], BrokerSimulator]


def valuation_execution_factory(
    mark_source: RegisteredMarkPriceSource,
    funding_source: RegisteredFundingSource | None = None,
) -> ExecutionFactory:
    if not isinstance(mark_source, RegisteredMarkPriceSource):
        raise TypeError("mark source must be RegisteredMarkPriceSource")

    def create(
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
    ) -> BrokerSimulator:
        if funding_source is None:
            return MarkAwareBrokerSimulator(
                config,
                journal,
                instrument,
                mark_source=mark_source,
            )
        return FundingMarkAwareBrokerSimulator(
            config,
            journal,
            instrument,
            funding_source=funding_source,
            mark_source=mark_source,
        )

    return create


def _required_record_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MarkValuationError("registered mark lineage record_count is missing or invalid")
    return value


def _finite_positive_float(value: str | float | int, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise MarkValuationError(f"{name} must be numeric") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise MarkValuationError(f"{name} must be finite and positive")
    return numeric


def _utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarkValuationError(f"{name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise MarkValuationError(f"{name} must use UTC offset")
    return value.astimezone(UTC)


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MarkValuationError("mark valuation identity is not canonical JSON") from exc
