from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta

from ..canonical import sha256_canonical
from ..contracts import (
    DataTypeCapability,
    DownloadChunk,
    DownloadPlan,
    ExchangeCapabilities,
    ExchangeReference,
    InstrumentMetadata,
    InstrumentReference,
    MarketDataRequest,
)
from ..enums import AdapterMaturity, DataType, MarketType, SourceKind
from ..errors import AdapterError
from ..records import (
    FundingRateRecord,
    KlineRecord,
    NormalizedRecord,
    OpenInterestRecord,
    PriceKlineRecord,
)
from ..timeframes import timeframe_delta
from .base import AcquiredData, ExchangeAdapter, assert_non_overlapping

_CHUNKED_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)


class FakeExchangeAdapter(ExchangeAdapter):
    exchange_id = "fake"
    adapter_version = "1.2.0"

    def __init__(
        self,
        *,
        fail_chunks: frozenset[str] = frozenset(),
        duplicate_page: bool = False,
        chunk_bars: int | None = None,
    ) -> None:
        if chunk_bars is not None and chunk_bars <= 0:
            raise ValueError("chunk_bars must be positive")
        self._fail_chunks = fail_chunks
        self._duplicate_page = duplicate_page
        self._chunk_bars = chunk_bars
        self.acquired_chunks: list[str] = []
        self.restored_chunks: list[str] = []

    def get_capabilities(self) -> ExchangeCapabilities:
        futures_only = (MarketType.USDT_M_PERPETUAL,)
        return ExchangeCapabilities(
            exchange=ExchangeReference(self.exchange_id, self.adapter_version),
            markets=(MarketType.SPOT, MarketType.USDT_M_PERPETUAL),
            data=(
                DataTypeCapability(
                    data_type=DataType.KLINES,
                    timeframes=("1m", "5m", "15m", "1h", "4h", "1d"),
                    sources=(SourceKind.OFFICIAL_REST,),
                ),
                DataTypeCapability(
                    data_type=DataType.FUNDING_RATE,
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                ),
                DataTypeCapability(
                    data_type=DataType.MARK_PRICE,
                    timeframes=("1m", "5m", "15m", "1h", "4h", "1d"),
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                ),
                DataTypeCapability(
                    data_type=DataType.INDEX_PRICE,
                    timeframes=("1m", "5m", "15m", "1h", "4h", "1d"),
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                ),
                DataTypeCapability(
                    data_type=DataType.PREMIUM_INDEX,
                    timeframes=("1m", "5m", "15m", "1h", "4h", "1d"),
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                ),
                DataTypeCapability(
                    data_type=DataType.OPEN_INTEREST_HISTORY,
                    timeframes=("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"),
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                ),
                DataTypeCapability(
                    data_type=DataType.INSTRUMENT_METADATA,
                    sources=(SourceKind.OFFICIAL_REST,),
                ),
                DataTypeCapability(
                    data_type=DataType.EXCHANGE_METADATA,
                    sources=(SourceKind.OFFICIAL_REST,),
                ),
            ),
            maximum_page_size=1000,
            timestamp_unit="milliseconds",
            maturity=AdapterMaturity.TESTED,
            rate_limit_policy={"requests_per_second": 100},
        )

    def resolve_instrument(self, market_type: object, symbol: str) -> InstrumentMetadata:
        if not isinstance(market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        normalized = symbol.replace("/", "").upper()
        if normalized not in {"BTCUSDT", "ETHUSDT"}:
            raise AdapterError(
                "INSTRUMENT_NOT_FOUND",
                "fake instrument does not exist",
                False,
                "instrument_resolution",
            )
        reference = InstrumentReference(
            exchange_id=self.exchange_id,
            market_type=market_type,
            symbol=normalized,
            canonical_id=f"fake:{market_type.value}:{normalized.lower()}",
        )
        return InstrumentMetadata(
            reference=reference,
            base_asset=normalized[:-4],
            quote_asset="USDT",
            settlement_asset="USDT" if market_type is not MarketType.SPOT else None,
            contract_type="perpetual" if market_type is not MarketType.SPOT else "spot",
            listing_date=datetime(2020, 1, 1, tzinfo=UTC),
            delisting_date=None,
            tick_size="0.1",
            quantity_step="0.001",
            contract_multiplier="1",
            trading_status="TRADING",
        )

    def plan_download(
        self, request: MarketDataRequest, instrument: InstrumentReference
    ) -> DownloadPlan:
        start = request.start or datetime(2020, 1, 1, tzinfo=UTC)
        chunks: list[DownloadChunk] = []
        requested = tuple(dict.fromkeys((*request.required_data, *request.optional_data)))
        for data_type in requested:
            if data_type not in _CHUNKED_TYPES:
                continue
            cursor = start
            index = 0
            if data_type is DataType.FUNDING_RATE:
                maximum = timedelta(days=30)
                timeframe = None
            else:
                maximum = (
                    timeframe_delta(request.base_timeframe) * self._chunk_bars
                    if self._chunk_bars is not None
                    else request.end - start
                )
                timeframe = request.base_timeframe
            while cursor < request.end:
                index += 1
                boundary = min(request.end, cursor + maximum)
                chunks.append(
                    DownloadChunk(
                        chunk_id=(f"{request.request_id}:{data_type.value}:{index:06d}"),
                        data_type=data_type,
                        start=cursor,
                        end=boundary,
                        source=SourceKind.OFFICIAL_REST,
                        timeframe=timeframe,
                        expected_uri=f"fake://{data_type.value}",
                    )
                )
                cursor = boundary
        assert_non_overlapping(chunks)
        return DownloadPlan(
            request_hash=request.canonical_hash,
            instrument=instrument,
            chunks=tuple(chunks),
            created_at=datetime.now(UTC),
        )

    def restore(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> AcquiredData:
        self.restored_chunks.append(chunk.chunk_id)
        records: tuple[NormalizedRecord, ...]
        if chunk.data_type is DataType.KLINES:
            records = self._restore_klines(chunk, instrument, raw_payload)
        else:
            records = self._restore_json(chunk, instrument, raw_payload)
        return AcquiredData(
            chunk=chunk,
            source_uri=chunk.expected_uri or f"fake://{chunk.data_type.value}",
            raw_payload=raw_payload,
            records=records,
            response_metadata={"records": len(records), "restored": True},
        )

    def acquire(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        self.acquired_chunks.append(chunk.chunk_id)
        if chunk.chunk_id in self._fail_chunks:
            raise AdapterError("FAKE_FAILURE", "injected adapter failure", True, "download")
        records: tuple[NormalizedRecord, ...]
        if chunk.data_type is DataType.KLINES:
            kline_records = self._generate_klines(chunk, instrument)
            records = kline_records
            payload = self._kline_csv(kline_records)
        elif chunk.data_type in {DataType.MARK_PRICE, DataType.INDEX_PRICE, DataType.PREMIUM_INDEX}:
            records = self._generate_price_klines(chunk, instrument)
            payload = self._json_payload(records)
        elif chunk.data_type is DataType.FUNDING_RATE:
            records = self._generate_funding(chunk, instrument)
            payload = self._json_payload(records)
        elif chunk.data_type is DataType.OPEN_INTEREST_HISTORY:
            records = self._generate_open_interest(chunk, instrument)
            payload = self._json_payload(records)
        else:
            raise AdapterError(
                "DATA_TYPE_UNSUPPORTED",
                "fake adapter data type is not implemented",
                False,
                "download",
            )
        return AcquiredData(
            chunk=chunk,
            source_uri=f"fake://{chunk.data_type.value}",
            raw_payload=payload,
            records=records,
            response_metadata={
                "records": len(records),
                "identity": sha256_canonical([record.to_row() for record in records]),
            },
        )

    def _generate_klines(
        self, chunk: DownloadChunk, instrument: InstrumentReference
    ) -> tuple[KlineRecord, ...]:
        if chunk.timeframe is None:
            raise AdapterError("TIMEFRAME_REQUIRED", "klines require timeframe", False, "download")
        step = timeframe_delta(chunk.timeframe)
        records: list[KlineRecord] = []
        current = chunk.start
        index = 0
        while current < chunk.end:
            price = 100 + index
            records.append(
                KlineRecord(
                    open_time=current,
                    close_time=current + step - timedelta(milliseconds=1),
                    open=str(price),
                    high=str(price + 2),
                    low=str(price - 1),
                    close=str(price + 1),
                    volume=str(10 + index),
                    quote_volume=str((10 + index) * (price + 1)),
                    trade_count=3,
                    taker_buy_base_volume="5",
                    taker_buy_quote_volume=str(5 * (price + 1)),
                    symbol=instrument.symbol,
                    timeframe=chunk.timeframe,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                )
            )
            current += step
            index += 1
        if self._duplicate_page and records:
            records.append(records[-1])
        return tuple(records)

    @staticmethod
    def _generate_price_klines(
        chunk: DownloadChunk, instrument: InstrumentReference
    ) -> tuple[PriceKlineRecord, ...]:
        if chunk.timeframe is None:
            raise AdapterError(
                "TIMEFRAME_REQUIRED",
                "price klines require timeframe",
                False,
                "download",
            )
        step = timeframe_delta(chunk.timeframe)
        records: list[PriceKlineRecord] = []
        current = chunk.start
        index = 0
        while current < chunk.end:
            price = 200 + index
            records.append(
                PriceKlineRecord(
                    price_type=chunk.data_type,
                    open_time=current,
                    close_time=current + step - timedelta(milliseconds=1),
                    open=str(price),
                    high=str(price + 1),
                    low=str(price - 1),
                    close=str(price + 0.5),
                    symbol=instrument.symbol,
                    timeframe=chunk.timeframe,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                )
            )
            current += step
            index += 1
        return tuple(records)

    @staticmethod
    def _generate_funding(
        chunk: DownloadChunk, instrument: InstrumentReference
    ) -> tuple[FundingRateRecord, ...]:
        records: list[FundingRateRecord] = []
        current = chunk.start
        while current < chunk.end:
            records.append(
                FundingRateRecord(
                    funding_time=current,
                    funding_rate="0.0001",
                    mark_price="200",
                    symbol=instrument.symbol,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                )
            )
            current += timedelta(hours=8)
        return tuple(records)

    @staticmethod
    def _generate_open_interest(
        chunk: DownloadChunk, instrument: InstrumentReference
    ) -> tuple[OpenInterestRecord, ...]:
        if chunk.timeframe is None:
            raise AdapterError(
                "TIMEFRAME_REQUIRED",
                "open interest history requires period",
                False,
                "download",
            )
        step = timeframe_delta(chunk.timeframe)
        records: list[OpenInterestRecord] = []
        current = chunk.start
        index = 0
        while current < chunk.end:
            records.append(
                OpenInterestRecord(
                    observed_at=current,
                    sum_open_interest=str(1000 + index),
                    sum_open_interest_value=str(200_000 + index * 100),
                    period=chunk.timeframe,
                    symbol=instrument.symbol,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                )
            )
            current += step
            index += 1
        return tuple(records)

    @staticmethod
    def _kline_csv(records: tuple[KlineRecord, ...]) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
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
                "is_closed",
            ]
        )
        for record in records:
            writer.writerow(
                [
                    record.open_time.isoformat(),
                    record.close_time.isoformat(),
                    record.open,
                    record.high,
                    record.low,
                    record.close,
                    record.volume,
                    record.quote_volume,
                    record.trade_count,
                    record.taker_buy_base_volume,
                    record.taker_buy_quote_volume,
                    record.is_closed,
                ]
            )
        return output.getvalue().encode("utf-8")

    @staticmethod
    def _json_payload(records: tuple[NormalizedRecord, ...]) -> bytes:
        rows = []
        for record in records:
            row = record.to_row()
            rows.append(
                {
                    name: value.isoformat() if isinstance(value, datetime) else value
                    for name, value in row.items()
                }
            )
        return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _restore_klines(
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> tuple[KlineRecord, ...]:
        if chunk.timeframe is None:
            raise AdapterError("TIMEFRAME_REQUIRED", "klines require timeframe", False, "restore")
        try:
            text = io.StringIO(raw_payload.decode("utf-8"), newline="")
            reader = csv.DictReader(text)
            return tuple(
                KlineRecord(
                    open_time=datetime.fromisoformat(row["open_time"]),
                    close_time=datetime.fromisoformat(row["close_time"]),
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    quote_volume=row["quote_volume"],
                    trade_count=int(row["trade_count"]),
                    taker_buy_base_volume=row["taker_buy_base_volume"],
                    taker_buy_quote_volume=row["taker_buy_quote_volume"],
                    symbol=instrument.symbol,
                    timeframe=chunk.timeframe or "",
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                    is_closed=row["is_closed"].strip().lower() == "true",
                )
                for row in reader
            )
        except (UnicodeDecodeError, KeyError, ValueError, csv.Error) as exc:
            raise AdapterError(
                "RAW_RESTORE_FAILED",
                "fake raw artifact cannot be restored",
                False,
                "restore",
            ) from exc

    @staticmethod
    def _restore_json(
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> tuple[NormalizedRecord, ...]:
        try:
            rows = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AdapterError(
                "RAW_RESTORE_FAILED",
                "fake JSON raw artifact cannot be restored",
                False,
                "restore",
            ) from exc
        if not isinstance(rows, list):
            raise AdapterError(
                "RAW_RESTORE_FAILED", "fake raw root must be a list", False, "restore"
            )
        if chunk.data_type in {DataType.MARK_PRICE, DataType.INDEX_PRICE, DataType.PREMIUM_INDEX}:
            if chunk.timeframe is None:
                raise AdapterError(
                    "TIMEFRAME_REQUIRED", "price klines require timeframe", False, "restore"
                )
            return tuple(
                PriceKlineRecord(
                    price_type=chunk.data_type,
                    open_time=datetime.fromisoformat(str(row["open_time"])),
                    close_time=datetime.fromisoformat(str(row["close_time"])),
                    open=str(row["open"]),
                    high=str(row["high"]),
                    low=str(row["low"]),
                    close=str(row["close"]),
                    symbol=instrument.symbol,
                    timeframe=chunk.timeframe,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                    is_closed=bool(row["is_closed"]),
                )
                for row in rows
                if isinstance(row, dict)
            )
        if chunk.data_type is DataType.FUNDING_RATE:
            return tuple(
                FundingRateRecord(
                    funding_time=datetime.fromisoformat(str(row["funding_time"])),
                    funding_rate=str(row["funding_rate"]),
                    mark_price=(
                        str(row["mark_price"]) if row.get("mark_price") is not None else None
                    ),
                    symbol=instrument.symbol,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                )
                for row in rows
                if isinstance(row, dict)
            )
        if chunk.data_type is DataType.OPEN_INTEREST_HISTORY:
            if chunk.timeframe is None:
                raise AdapterError(
                    "TIMEFRAME_REQUIRED",
                    "open interest history requires period",
                    False,
                    "restore",
                )
            return tuple(
                OpenInterestRecord(
                    observed_at=datetime.fromisoformat(str(row["timestamp"])),
                    sum_open_interest=str(row["sum_open_interest"]),
                    sum_open_interest_value=str(row["sum_open_interest_value"]),
                    circulating_supply=(
                        str(row["circulating_supply"])
                        if row.get("circulating_supply") is not None
                        else None
                    ),
                    period=chunk.timeframe,
                    symbol=instrument.symbol,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=chunk.source.value,
                )
                for row in rows
                if isinstance(row, dict)
            )
        raise AdapterError(
            "DATA_TYPE_UNSUPPORTED",
            "fake restore data type is not implemented",
            False,
            "restore",
        )
