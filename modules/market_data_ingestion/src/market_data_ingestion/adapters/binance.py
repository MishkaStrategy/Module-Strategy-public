from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

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
from ..network import HttpClient
from ..records import (
    FundingRateRecord,
    KlineRecord,
    NormalizedRecord,
    OpenInterestRecord,
    PriceKlineRecord,
    parse_epoch,
)
from ..timeframes import timeframe_delta
from .base import (
    AcquiredData,
    ExchangeAdapter,
    InstrumentResolution,
    assert_non_overlapping,
)

_ARCHIVE = "https://data.binance.vision"
_SPOT_API = "https://data-api.binance.vision"
_USDTM_API = "https://fapi.binance.com"
_SAFE_ZIP_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.csv$")
_RAW_MARKER = b"MDI-RAW-HTTP-PAGES-V1\n"
_PRICE_TYPES = frozenset({DataType.MARK_PRICE, DataType.INDEX_PRICE, DataType.PREMIUM_INDEX})
_OI_PERIODS = frozenset({"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"})
_FUNDING_MAX_POSITIVE_JITTER_MS = 47
_FUNDING_TIMESTAMP_POLICY = "binance-usdtm-hour-boundary-jitter-v2"
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


class BinanceAdapter(ExchangeAdapter):
    """Official Binance Spot and USD-M historical market-data adapter.

    The adapter advertises only implemented public-data families. Mark/index price,
    funding, and open-interest history are isolated from traded OHLCV by record type,
    storage path, and manifest data type.
    """

    exchange_id = "binance"
    adapter_version = "0.5.0"

    def __init__(
        self,
        http: HttpClient | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http or HttpClient(timeout_seconds=30, retries=6)
        self._now = now or (lambda: datetime.now(UTC))

    def get_capabilities(self) -> ExchangeCapabilities:
        kline_timeframes = (
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "8h",
            "12h",
            "1d",
            "3d",
            "1w",
        )
        futures_only = (MarketType.USDT_M_PERPETUAL,)
        return ExchangeCapabilities(
            exchange=ExchangeReference(self.exchange_id, self.adapter_version),
            markets=(MarketType.SPOT, MarketType.USDT_M_PERPETUAL),
            data=(
                DataTypeCapability(
                    data_type=DataType.KLINES,
                    timeframes=kline_timeframes,
                    sources=(SourceKind.OFFICIAL_ARCHIVE, SourceKind.OFFICIAL_REST),
                    requires_api_key=False,
                    notes=(
                        "monthly archive preferred for complete months; "
                        "REST used for partial periods",
                    ),
                ),
                DataTypeCapability(
                    data_type=DataType.MARK_PRICE,
                    timeframes=kline_timeframes,
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                    requires_api_key=False,
                ),
                DataTypeCapability(
                    data_type=DataType.INDEX_PRICE,
                    timeframes=kline_timeframes,
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                    requires_api_key=False,
                ),
                DataTypeCapability(
                    data_type=DataType.PREMIUM_INDEX,
                    timeframes=kline_timeframes,
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                    requires_api_key=False,
                ),
                DataTypeCapability(
                    data_type=DataType.FUNDING_RATE,
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                    requires_api_key=False,
                    notes=(
                        "event interval may vary by symbol and historical funding configuration",
                        "only +1..47 ms provider jitter at an unambiguously mapped "
                        "canonical UTC-hour funding boundary is normalized; other non-aligned "
                        "timestamps fail closed",
                    ),
                ),
                DataTypeCapability(
                    data_type=DataType.OPEN_INTEREST_HISTORY,
                    timeframes=tuple(sorted(_OI_PERIODS)),
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=futures_only,
                    requires_api_key=False,
                    notes=("official REST endpoint retains only the latest 30 days",),
                ),
                DataTypeCapability(
                    data_type=DataType.INSTRUMENT_METADATA,
                    sources=(SourceKind.OFFICIAL_REST,),
                    requires_api_key=False,
                ),
                DataTypeCapability(
                    data_type=DataType.EXCHANGE_METADATA,
                    sources=(SourceKind.OFFICIAL_REST,),
                    requires_api_key=False,
                ),
            ),
            maximum_page_size=1000,
            timestamp_unit=("milliseconds (Spot archive may use microseconds from 2025-01-01)"),
            maturity=AdapterMaturity.TESTED,
            rate_limit_policy={
                "bounded_retries": 6,
                "http_429": "Retry-After then exponential backoff",
                "open_interest_history_limit": 500,
                "other_history_limit": 1000,
            },
        )

    def resolve_instrument(self, market_type: object, symbol: str) -> InstrumentMetadata:
        return self.resolve_instrument_with_evidence(market_type, symbol).metadata

    def resolve_instrument_with_evidence(
        self, market_type: object, symbol: str
    ) -> InstrumentResolution:
        if not isinstance(market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        if market_type not in {MarketType.SPOT, MarketType.USDT_M_PERPETUAL}:
            raise AdapterError(
                "MARKET_UNSUPPORTED",
                "Binance adapter market is not implemented",
                False,
                "instrument_resolution",
            )
        normalized = self._normalize_symbol(symbol)
        base, path = (
            (_SPOT_API, "/api/v3/exchangeInfo")
            if market_type is MarketType.SPOT
            else (_USDTM_API, "/fapi/v1/exchangeInfo")
        )
        response = self._http.get(base, path, {"symbol": normalized})
        metadata = self._metadata_from_exchange_info(market_type, normalized, response.body)
        return InstrumentResolution(
            metadata=metadata,
            source=SourceKind.OFFICIAL_REST,
            source_uri=f"{base}{path}",
            raw_payload=response.body,
            response_metadata={
                "records": 1,
                "http_status": response.status,
                "endpoint": path,
                "symbol": normalized,
            },
        )

    def restore_instrument_resolution(
        self, market_type: object, symbol: str, raw_payload: bytes
    ) -> InstrumentResolution:
        if not isinstance(market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        normalized = self._normalize_symbol(symbol)
        metadata = self._metadata_from_exchange_info(market_type, normalized, raw_payload)
        base, path = (
            (_SPOT_API, "/api/v3/exchangeInfo")
            if market_type is MarketType.SPOT
            else (_USDTM_API, "/fapi/v1/exchangeInfo")
        )
        return InstrumentResolution(
            metadata=metadata,
            source=SourceKind.OFFICIAL_REST,
            source_uri=f"{base}{path}",
            raw_payload=raw_payload,
            response_metadata={
                "records": 1,
                "endpoint": path,
                "symbol": normalized,
                "restored_from_raw": True,
            },
        )

    def _metadata_from_exchange_info(
        self, market_type: MarketType, normalized: str, raw_payload: bytes
    ) -> InstrumentMetadata:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "Binance exchangeInfo evidence is not valid JSON",
                False,
                "instrument_resolution",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "unexpected Binance exchangeInfo response",
                False,
                "instrument_resolution",
            )
        candidates = [
            item
            for item in payload["symbols"]
            if isinstance(item, dict) and item.get("symbol") == normalized
        ]
        if len(candidates) != 1:
            raise AdapterError(
                "INSTRUMENT_NOT_FOUND" if not candidates else "INSTRUMENT_AMBIGUOUS",
                "exact Binance instrument resolution failed",
                False,
                "instrument_resolution",
                {"symbol": normalized, "matches": len(candidates)},
            )
        item = candidates[0]
        if market_type is MarketType.USDT_M_PERPETUAL and item.get("contractType") != "PERPETUAL":
            raise AdapterError(
                "MARKET_MISMATCH",
                "requested instrument is not a USD-M perpetual",
                False,
                "instrument_resolution",
            )
        filters = {
            entry.get("filterType"): entry
            for entry in item.get("filters", [])
            if isinstance(entry, dict)
        }
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        onboard = item.get("onboardDate")
        delivery = item.get("deliveryDate")
        listing = parse_epoch(onboard) if isinstance(onboard, int) and onboard > 0 else None
        delisting = None
        if isinstance(delivery, int) and 0 < delivery < 4_102_444_800_000:
            delisting = parse_epoch(delivery)
        reference = InstrumentReference(
            exchange_id=self.exchange_id,
            market_type=market_type,
            symbol=normalized,
            canonical_id=f"binance:{market_type.value}:{normalized.lower()}",
        )
        return InstrumentMetadata(
            reference=reference,
            base_asset=str(item.get("baseAsset", "")),
            quote_asset=str(item.get("quoteAsset", "")),
            settlement_asset=(str(item.get("marginAsset", "")) or None),
            contract_type="spot" if market_type is MarketType.SPOT else "perpetual",
            listing_date=listing,
            delisting_date=delisting,
            tick_size=str(price_filter.get("tickSize", "0")),
            quantity_step=str(lot_filter.get("stepSize", "0")),
            contract_multiplier="1",
            trading_status=str(item.get("status", "UNKNOWN")),
        )

    def plan_download(
        self, request: MarketDataRequest, instrument: InstrumentReference
    ) -> DownloadPlan:
        start = request.start
        if start is None:
            raise AdapterError(
                "EARLIEST_DISCOVERY_REQUIRED",
                "full history planning requires resolved listing date in orchestration",
                False,
                "planning",
            )
        requested = tuple(dict.fromkeys((*request.required_data, *request.optional_data)))
        chunks: list[DownloadChunk] = []
        limitations: list[str] = []
        for data_type in requested:
            if data_type not in _CHUNKED_TYPES:
                continue
            if instrument.market_type is MarketType.SPOT and data_type is not DataType.KLINES:
                continue
            if data_type is DataType.KLINES:
                chunks.extend(self._plan_traded_klines(request, instrument, start))
            elif data_type in _PRICE_TYPES:
                chunks.extend(
                    self._plan_fixed_interval_rest(
                        request,
                        data_type=data_type,
                        start=start,
                        page_size=1000,
                    )
                )
            elif data_type is DataType.FUNDING_RATE:
                chunks.extend(self._plan_funding(request, start))
            elif data_type is DataType.OPEN_INTEREST_HISTORY:
                oi_chunks, limitation = self._plan_open_interest(request, start)
                chunks.extend(oi_chunks)
                if limitation is not None:
                    limitations.append(limitation)
        assert_non_overlapping(chunks)
        return DownloadPlan(
            request_hash=request.canonical_hash,
            instrument=instrument,
            chunks=tuple(chunks),
            created_at=self._utc_now(),
            limitations=tuple(limitations),
        )

    def acquire(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        if chunk.source is SourceKind.OFFICIAL_ARCHIVE:
            if chunk.data_type is not DataType.KLINES:
                raise AdapterError(
                    "CHUNK_CONTRACT",
                    "only traded klines currently use Binance archive acquisition",
                    False,
                    "download",
                )
            try:
                return self._acquire_archive(chunk, instrument)
            except AdapterError as exc:
                if exc.code != "HTTP_ERROR" or exc.details.get("status") != 404:
                    raise
                fallback = replace(
                    chunk,
                    source=SourceKind.OFFICIAL_REST,
                    expected_uri=self._rest_base(instrument.market_type),
                )
                return self._acquire_rest(fallback, instrument)
        if chunk.source is SourceKind.OFFICIAL_REST:
            return self._acquire_rest(chunk, instrument)
        raise AdapterError(
            "SOURCE_UNSUPPORTED", "unsupported Binance source kind", False, "download"
        )

    def restore(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> AcquiredData:
        pages_payload: list[list[object]] = []
        if chunk.source is SourceKind.OFFICIAL_ARCHIVE:
            if chunk.data_type is not DataType.KLINES:
                raise AdapterError(
                    "CHUNK_CONTRACT",
                    "archive restore supports traded klines only",
                    False,
                    "restore",
                )
            rows = self._safe_archive_rows(raw_payload)
            records: tuple[NormalizedRecord, ...] = tuple(
                self._parse_archive_rows(rows, chunk, instrument)
            )
            pages = 1
        elif chunk.source is SourceKind.OFFICIAL_REST:
            pages_payload = self._decode_pages(raw_payload)
            parsed: list[NormalizedRecord] = []
            for page in pages_payload:
                parsed.extend(self._parse_page(page, chunk, instrument))
            records = tuple(
                record for record in parsed if chunk.start <= record.timestamp < chunk.end
            )
            pages = len(pages_payload)
        else:
            raise AdapterError("SOURCE_UNSUPPORTED", "unsupported raw source", False, "restore")
        response_metadata: dict[str, object] = {
            "records": len(records),
            "pages": pages,
            "restored": True,
        }
        if chunk.data_type is DataType.FUNDING_RATE:
            response_metadata["funding_timestamp_normalization"] = (
                self._funding_normalization_summary(pages_payload)
            )
        return AcquiredData(
            chunk=chunk,
            source_uri=chunk.expected_uri or self._rest_base(instrument.market_type),
            raw_payload=raw_payload,
            records=records,
            response_metadata=response_metadata,
        )

    def _plan_traded_klines(
        self,
        request: MarketDataRequest,
        instrument: InstrumentReference,
        start: datetime,
    ) -> list[DownloadChunk]:
        chunks: list[DownloadChunk] = []
        cursor = start
        index = 0
        while cursor < request.end:
            next_month = self._next_month(cursor)
            boundary = min(request.end, next_month)
            full_month = cursor.day == 1 and cursor.time() == time.min and boundary == next_month
            if full_month:
                source = SourceKind.OFFICIAL_ARCHIVE
                uri = self._monthly_archive_uri(
                    instrument, request.base_timeframe, cursor.year, cursor.month
                )
            else:
                source = SourceKind.OFFICIAL_REST
                uri = self._rest_base(instrument.market_type)
                boundary = min(
                    boundary,
                    cursor + timeframe_delta(request.base_timeframe) * 1000,
                )
            index += 1
            chunks.append(
                DownloadChunk(
                    chunk_id=f"{request.request_id}:klines:{index:06d}",
                    data_type=DataType.KLINES,
                    start=cursor,
                    end=boundary,
                    source=source,
                    timeframe=request.base_timeframe,
                    expected_uri=uri,
                )
            )
            cursor = boundary
        return chunks

    @staticmethod
    def _plan_fixed_interval_rest(
        request: MarketDataRequest,
        *,
        data_type: DataType,
        start: datetime,
        page_size: int,
    ) -> list[DownloadChunk]:
        chunks: list[DownloadChunk] = []
        cursor = start
        index = 0
        maximum = timeframe_delta(request.base_timeframe) * page_size
        while cursor < request.end:
            index += 1
            boundary = min(request.end, cursor + maximum)
            chunks.append(
                DownloadChunk(
                    chunk_id=f"{request.request_id}:{data_type.value}:{index:06d}",
                    data_type=data_type,
                    start=cursor,
                    end=boundary,
                    source=SourceKind.OFFICIAL_REST,
                    timeframe=request.base_timeframe,
                    expected_uri=_USDTM_API,
                )
            )
            cursor = boundary
        return chunks

    @staticmethod
    def _plan_funding(request: MarketDataRequest, start: datetime) -> list[DownloadChunk]:
        chunks: list[DownloadChunk] = []
        cursor = start
        index = 0
        # Thirty days is at most 720 one-hour funding observations.
        maximum = timedelta(days=30)
        while cursor < request.end:
            index += 1
            boundary = min(request.end, cursor + maximum)
            chunks.append(
                DownloadChunk(
                    chunk_id=f"{request.request_id}:funding_rate:{index:06d}",
                    data_type=DataType.FUNDING_RATE,
                    start=cursor,
                    end=boundary,
                    source=SourceKind.OFFICIAL_REST,
                    expected_uri=_USDTM_API,
                )
            )
            cursor = boundary
        return chunks

    def _plan_open_interest(
        self, request: MarketDataRequest, start: datetime
    ) -> tuple[list[DownloadChunk], str | None]:
        if request.base_timeframe not in _OI_PERIODS:
            if DataType.OPEN_INTEREST_HISTORY in request.required_data:
                raise AdapterError(
                    "TIMEFRAME_UNSUPPORTED",
                    "Binance open-interest history does not support the requested period",
                    False,
                    "planning",
                    {"timeframe": request.base_timeframe},
                )
            return [], (
                "optional open_interest_history omitted because Binance does not "
                f"support period {request.base_timeframe}"
            )
        retention_start = self._utc_now() - timedelta(days=30)
        if start < retention_start:
            if DataType.OPEN_INTEREST_HISTORY in request.required_data:
                raise AdapterError(
                    "SOURCE_RETENTION_LIMIT",
                    "required Binance open-interest history exceeds the "
                    "official 30-day retention window",
                    False,
                    "planning",
                    {
                        "requested_start": start.isoformat(),
                        "earliest_available": retention_start.isoformat(),
                    },
                )
            limitation = (
                "optional open_interest_history was clipped to Binance's latest "
                "30-day official retention window"
            )
        else:
            limitation = None
        effective_start = max(start, retention_start)
        if request.end <= effective_start:
            if DataType.OPEN_INTEREST_HISTORY in request.required_data:
                raise AdapterError(
                    "SOURCE_RETENTION_LIMIT",
                    "Binance open-interest history is outside the official 30-day retention window",
                    False,
                    "planning",
                )
            return [], limitation or (
                "optional open_interest_history is outside Binance's official retention window"
            )
        chunks: list[DownloadChunk] = []
        cursor = effective_start
        index = 0
        maximum = timeframe_delta(request.base_timeframe) * 500
        while cursor < request.end:
            index += 1
            boundary = min(request.end, cursor + maximum)
            chunks.append(
                DownloadChunk(
                    chunk_id=(f"{request.request_id}:open_interest_history:{index:06d}"),
                    data_type=DataType.OPEN_INTEREST_HISTORY,
                    start=cursor,
                    end=boundary,
                    source=SourceKind.OFFICIAL_REST,
                    timeframe=request.base_timeframe,
                    expected_uri=_USDTM_API,
                )
            )
            cursor = boundary
        return chunks, limitation

    def _acquire_archive(
        self, chunk: DownloadChunk, instrument: InstrumentReference
    ) -> AcquiredData:
        if chunk.expected_uri is None:
            raise AdapterError("CHUNK_CONTRACT", "archive URI missing", False, "download")
        checksum_response = self._http.get(chunk.expected_uri + ".CHECKSUM")
        try:
            checksum_text = checksum_response.body.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise AdapterError(
                "CHECKSUM_FORMAT",
                "official archive checksum is not ASCII",
                False,
                "verify_raw",
            ) from exc
        expected = checksum_text.split()[0].lower() if checksum_text else ""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise AdapterError(
                "CHECKSUM_FORMAT",
                "invalid official archive checksum",
                False,
                "verify_raw",
            )
        response = self._http.get(chunk.expected_uri)
        actual = hashlib.sha256(response.body).hexdigest()
        if actual != expected:
            raise AdapterError(
                "CHECKSUM_MISMATCH",
                "official Binance archive checksum mismatch",
                False,
                "verify_raw",
                {"expected": expected, "actual": actual},
            )
        rows = self._safe_archive_rows(response.body)
        records = tuple(self._parse_archive_rows(rows, chunk, instrument))
        return AcquiredData(
            chunk=chunk,
            source_uri=chunk.expected_uri,
            raw_payload=response.body,
            records=records,
            response_metadata={
                "records": len(records),
                "http_status": response.status,
                "pages": 1,
            },
            upstream_checksum=expected,
        )

    def _acquire_rest(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        path, static_params, limit, cursor_step = self._rest_contract(chunk, instrument)
        cursor = chunk.start
        page_bodies: list[bytes] = []
        funding_pages: list[list[object]] = []
        records: list[NormalizedRecord] = []
        previous_signature: str | None = None
        consecutive_empty_pages = 0
        while cursor < chunk.end:
            params = {
                **static_params,
                "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(chunk.end.timestamp() * 1000) - 1,
                "limit": limit,
            }
            base_url = _USDTM_API if instrument.market_type is not MarketType.SPOT else _SPOT_API
            response = self._http.get(base_url, path, params)
            page = response.json()
            if not isinstance(page, list):
                raise AdapterError(
                    "SOURCE_SCHEMA_CHANGED",
                    "Binance historical page is not a list",
                    False,
                    "download",
                )
            if not page:
                page_bodies.append(response.body)
                if chunk.data_type is DataType.FUNDING_RATE:
                    break
                consecutive_empty_pages += 1
                if consecutive_empty_pages > 2:
                    break
                cursor += cursor_step
                continue
            consecutive_empty_pages = 0
            signature = sha256_canonical(page)
            if signature == previous_signature:
                raise AdapterError(
                    "REPEATED_PAGE",
                    "Binance API repeated the same page",
                    True,
                    "download",
                )
            previous_signature = signature
            parsed = self._parse_page(page, chunk, instrument)
            if not parsed:
                raise AdapterError(
                    "EMPTY_PARSED_PAGE",
                    "Binance page contained no usable records",
                    False,
                    "normalize",
                )
            timestamps = [item.timestamp for item in parsed]
            if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
                raise AdapterError(
                    "PAGE_ORDER_INVALID",
                    "Binance page timestamps are not strictly increasing",
                    False,
                    "download",
                )
            cursor_timestamps = (
                [parse_epoch(row["fundingTime"]) for row in page if isinstance(row, dict)]
                if chunk.data_type is DataType.FUNDING_RATE
                else timestamps
            )
            if len(cursor_timestamps) != len(parsed):
                raise AdapterError(
                    "SOURCE_SCHEMA_CHANGED",
                    "Binance funding page lacks a raw timestamp for pagination",
                    False,
                    "download",
                )
            if any(timestamp < cursor for timestamp in cursor_timestamps):
                raise AdapterError(
                    "CURSOR_REGRESSION",
                    "Binance page regressed before cursor",
                    False,
                    "download",
                )
            last_timestamp = cursor_timestamps[-1]
            new_cursor = last_timestamp + cursor_step
            if new_cursor <= cursor:
                raise AdapterError(
                    "CURSOR_NOT_ADVANCING",
                    "Binance cursor did not advance",
                    True,
                    "download",
                )
            records.extend(item for item in parsed if chunk.start <= item.timestamp < chunk.end)
            page_bodies.append(response.body)
            if chunk.data_type is DataType.FUNDING_RATE:
                funding_pages.append(page)
            cursor = new_cursor
            if len(page) < limit:
                break
        raw_payload = self._encode_pages(page_bodies)
        response_metadata: dict[str, object] = {
            "pages": len(page_bodies),
            "records": len(records),
            "framing": "MDI-RAW-HTTP-PAGES-V1",
            "endpoint": path,
        }
        if chunk.data_type is DataType.FUNDING_RATE:
            response_metadata["funding_timestamp_normalization"] = (
                self._funding_normalization_summary(funding_pages)
            )
        return AcquiredData(
            chunk=chunk,
            source_uri=f"{self._rest_base(instrument.market_type)}{path}",
            raw_payload=raw_payload,
            records=tuple(records),
            response_metadata=response_metadata,
        )

    def _rest_contract(
        self, chunk: DownloadChunk, instrument: InstrumentReference
    ) -> tuple[str, dict[str, object], int, timedelta]:
        if chunk.data_type is DataType.KLINES:
            if chunk.timeframe is None:
                raise AdapterError(
                    "CHUNK_CONTRACT", "kline chunk requires timeframe", False, "download"
                )
            path = (
                "/api/v3/klines" if instrument.market_type is MarketType.SPOT else "/fapi/v1/klines"
            )
            return (
                path,
                {"symbol": instrument.symbol, "interval": chunk.timeframe},
                1000,
                timeframe_delta(chunk.timeframe),
            )
        if chunk.data_type is DataType.MARK_PRICE:
            if chunk.timeframe is None:
                raise AdapterError(
                    "CHUNK_CONTRACT",
                    "mark-price chunk requires timeframe",
                    False,
                    "download",
                )
            return (
                "/fapi/v1/markPriceKlines",
                {"symbol": instrument.symbol, "interval": chunk.timeframe},
                1000,
                timeframe_delta(chunk.timeframe),
            )
        if chunk.data_type is DataType.INDEX_PRICE:
            if chunk.timeframe is None:
                raise AdapterError(
                    "CHUNK_CONTRACT",
                    "index-price chunk requires timeframe",
                    False,
                    "download",
                )
            return (
                "/fapi/v1/indexPriceKlines",
                {"pair": instrument.symbol, "interval": chunk.timeframe},
                1000,
                timeframe_delta(chunk.timeframe),
            )
        if chunk.data_type is DataType.PREMIUM_INDEX:
            if chunk.timeframe is None:
                raise AdapterError(
                    "CHUNK_CONTRACT",
                    "premium-index chunk requires timeframe",
                    False,
                    "download",
                )
            return (
                "/fapi/v1/premiumIndexKlines",
                {"symbol": instrument.symbol, "interval": chunk.timeframe},
                1000,
                timeframe_delta(chunk.timeframe),
            )
        if chunk.data_type is DataType.FUNDING_RATE:
            return (
                "/fapi/v1/fundingRate",
                {"symbol": instrument.symbol},
                1000,
                timedelta(milliseconds=1),
            )
        if chunk.data_type is DataType.OPEN_INTEREST_HISTORY:
            if chunk.timeframe is None:
                raise AdapterError(
                    "CHUNK_CONTRACT",
                    "open-interest chunk requires period",
                    False,
                    "download",
                )
            return (
                "/futures/data/openInterestHist",
                {"symbol": instrument.symbol, "period": chunk.timeframe},
                500,
                timeframe_delta(chunk.timeframe),
            )
        raise AdapterError(
            "DATA_TYPE_UNSUPPORTED",
            "Binance REST acquisition data type is not implemented",
            False,
            "download",
        )

    def _parse_page(
        self,
        page: list[object],
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> list[NormalizedRecord]:
        try:
            if chunk.data_type is DataType.KLINES:
                return [self._parse_traded_kline(row, chunk, instrument) for row in page]
            if chunk.data_type in _PRICE_TYPES:
                return [self._parse_price_kline(row, chunk, instrument) for row in page]
            if chunk.data_type is DataType.FUNDING_RATE:
                return [self._parse_funding(row, chunk, instrument) for row in page]
            if chunk.data_type is DataType.OPEN_INTEREST_HISTORY:
                return [self._parse_open_interest(row, chunk, instrument) for row in page]
            raise AdapterError(
                "DATA_TYPE_UNSUPPORTED",
                "Binance response parser data type is not implemented",
                False,
                "normalize",
            )
        except AdapterError:
            raise
        except (ArithmeticError, TypeError, ValueError, OverflowError) as exc:
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "Binance historical response contains an invalid field representation",
                False,
                "normalize",
                {"data_type": chunk.data_type.value},
            ) from exc

    def _parse_archive_rows(
        self,
        rows: Iterable[list[str]],
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> Iterable[KlineRecord]:
        for row in rows:
            if not row or row[0].lower() in {"open_time", "open time"}:
                continue
            try:
                yield self._parse_traded_kline(row, chunk, instrument)
            except AdapterError:
                raise
            except (ArithmeticError, TypeError, ValueError, OverflowError) as exc:
                raise AdapterError(
                    "SOURCE_SCHEMA_CHANGED",
                    "Binance archive row contains an invalid field representation",
                    False,
                    "normalize",
                    {"data_type": chunk.data_type.value},
                ) from exc

    def _parse_traded_kline(
        self,
        row: object,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> KlineRecord:
        if not isinstance(row, list) or len(row) < 11 or chunk.timeframe is None:
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "unexpected Binance traded-kline row",
                False,
                "normalize",
            )
        unit = (
            "microseconds"
            if instrument.market_type is MarketType.SPOT and int(row[0]) > 10**14
            else "milliseconds"
        )
        open_time = parse_epoch(row[0], unit=unit)
        close_time = parse_epoch(row[6], unit=unit)
        return KlineRecord(
            open_time=open_time,
            close_time=close_time,
            open=str(row[1]),
            high=str(row[2]),
            low=str(row[3]),
            close=str(row[4]),
            volume=str(row[5]),
            quote_volume=str(row[7]),
            trade_count=int(row[8]),
            taker_buy_base_volume=str(row[9]),
            taker_buy_quote_volume=str(row[10]),
            symbol=instrument.symbol,
            timeframe=chunk.timeframe,
            exchange_id=self.exchange_id,
            market_type=instrument.market_type.value,
            source_kind=chunk.source.value,
            is_closed=close_time < self._utc_now(),
        )

    def _parse_price_kline(
        self,
        row: object,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> PriceKlineRecord:
        if not isinstance(row, list) or len(row) < 7 or chunk.timeframe is None:
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "unexpected Binance price-kline row",
                False,
                "normalize",
            )
        open_time = parse_epoch(row[0])
        close_time = parse_epoch(row[6])
        return PriceKlineRecord(
            price_type=chunk.data_type,
            open_time=open_time,
            close_time=close_time,
            open=str(row[1]),
            high=str(row[2]),
            low=str(row[3]),
            close=str(row[4]),
            symbol=instrument.symbol,
            timeframe=chunk.timeframe,
            exchange_id=self.exchange_id,
            market_type=instrument.market_type.value,
            source_kind=chunk.source.value,
            is_closed=close_time < self._utc_now(),
        )

    @staticmethod
    def _canonical_funding_time(value: object) -> tuple[datetime, int]:
        raw_timestamp = parse_epoch(value)
        if raw_timestamp.minute != 0 or raw_timestamp.second != 0:
            raise AdapterError(
                "FUNDING_BOUNDARY_UNMAPPABLE",
                "Binance funding timestamp is not on a canonical UTC hour boundary",
                False,
                "normalize",
                {
                    "policy": _FUNDING_TIMESTAMP_POLICY,
                    "raw_timestamp": raw_timestamp.isoformat(),
                    "max_positive_jitter_ms": _FUNDING_MAX_POSITIVE_JITTER_MS,
                },
            )
        jitter_microseconds = raw_timestamp.microsecond
        if jitter_microseconds == 0:
            return raw_timestamp, 0
        if jitter_microseconds % 1000 != 0:
            raise AdapterError(
                "FUNDING_BOUNDARY_UNMAPPABLE",
                "Binance funding timestamp precision cannot be mapped deterministically",
                False,
                "normalize",
                {
                    "policy": _FUNDING_TIMESTAMP_POLICY,
                    "raw_timestamp": raw_timestamp.isoformat(),
                    "max_positive_jitter_ms": _FUNDING_MAX_POSITIVE_JITTER_MS,
                },
            )
        jitter_ms = jitter_microseconds // 1000
        if jitter_ms > _FUNDING_MAX_POSITIVE_JITTER_MS:
            raise AdapterError(
                "FUNDING_BOUNDARY_UNMAPPABLE",
                "Binance funding timestamp exceeds the approved positive jitter window",
                False,
                "normalize",
                {
                    "policy": _FUNDING_TIMESTAMP_POLICY,
                    "raw_timestamp": raw_timestamp.isoformat(),
                    "observed_positive_jitter_ms": jitter_ms,
                    "max_positive_jitter_ms": _FUNDING_MAX_POSITIVE_JITTER_MS,
                },
            )
        return raw_timestamp.replace(microsecond=0), jitter_ms

    @classmethod
    def _funding_normalization_summary(
        cls,
        pages: Iterable[list[object]],
    ) -> dict[str, object]:
        exact_boundary_events = 0
        normalized_jitter_events = 0
        max_observed_positive_jitter_ms = 0
        for page in pages:
            for row in page:
                if not isinstance(row, dict) or "fundingTime" not in row:
                    raise AdapterError(
                        "SOURCE_SCHEMA_CHANGED",
                        "Binance funding row is missing raw timestamp evidence",
                        False,
                        "normalize",
                    )
                _, jitter_ms = cls._canonical_funding_time(row["fundingTime"])
                if jitter_ms == 0:
                    exact_boundary_events += 1
                else:
                    normalized_jitter_events += 1
                    max_observed_positive_jitter_ms = max(
                        max_observed_positive_jitter_ms,
                        jitter_ms,
                    )
        return {
            "policy": _FUNDING_TIMESTAMP_POLICY,
            "canonical_boundary": "utc-hour",
            "approved_positive_jitter_ms_min": 1,
            "approved_positive_jitter_ms_max": _FUNDING_MAX_POSITIVE_JITTER_MS,
            "raw_timestamp_field": "fundingTime",
            "raw_payload_preserved": True,
            "non_mappable_behavior": "fail_closed",
            "exact_boundary_events": exact_boundary_events,
            "normalized_jitter_events": normalized_jitter_events,
            "max_observed_positive_jitter_ms": max_observed_positive_jitter_ms,
        }

    def _parse_funding(
        self,
        row: object,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> FundingRateRecord:
        if not isinstance(row, dict):
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "unexpected Binance funding-rate row",
                False,
                "normalize",
            )
        if row.get("symbol") != instrument.symbol:
            raise AdapterError(
                "SOURCE_IDENTITY_MISMATCH",
                "funding row symbol differs from resolved instrument",
                False,
                "normalize",
            )
        required = {"fundingTime", "fundingRate"}
        if not required.issubset(row):
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "Binance funding row is missing required fields",
                False,
                "normalize",
            )
        funding_time, _jitter_ms = self._canonical_funding_time(row["fundingTime"])
        return FundingRateRecord(
            funding_time=funding_time,
            funding_rate=str(row["fundingRate"]),
            mark_price=(str(row["markPrice"]) if row.get("markPrice") not in {None, ""} else None),
            symbol=instrument.symbol,
            exchange_id=self.exchange_id,
            market_type=instrument.market_type.value,
            source_kind=chunk.source.value,
        )

    def _parse_open_interest(
        self,
        row: object,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> OpenInterestRecord:
        if not isinstance(row, dict) or chunk.timeframe is None:
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "unexpected Binance open-interest row",
                False,
                "normalize",
            )
        if row.get("symbol") != instrument.symbol:
            raise AdapterError(
                "SOURCE_IDENTITY_MISMATCH",
                "open-interest row symbol differs from resolved instrument",
                False,
                "normalize",
            )
        required = {"timestamp", "sumOpenInterest", "sumOpenInterestValue"}
        if not required.issubset(row):
            raise AdapterError(
                "SOURCE_SCHEMA_CHANGED",
                "Binance open-interest row is missing required fields",
                False,
                "normalize",
            )
        return OpenInterestRecord(
            observed_at=parse_epoch(row["timestamp"]),
            sum_open_interest=str(row["sumOpenInterest"]),
            sum_open_interest_value=str(row["sumOpenInterestValue"]),
            circulating_supply=(
                str(row["CMCCirculatingSupply"])
                if row.get("CMCCirculatingSupply") not in {None, ""}
                else None
            ),
            period=chunk.timeframe,
            symbol=instrument.symbol,
            exchange_id=self.exchange_id,
            market_type=instrument.market_type.value,
            source_kind=chunk.source.value,
        )

    @staticmethod
    def _encode_pages(page_bodies: Iterable[bytes]) -> bytes:
        raw = io.BytesIO()
        raw.write(_RAW_MARKER)
        for body in page_bodies:
            raw.write(len(body).to_bytes(8, "big"))
            raw.write(body)
        return raw.getvalue()

    @staticmethod
    def _decode_pages(raw_payload: bytes) -> list[list[object]]:
        if not raw_payload.startswith(_RAW_MARKER):
            raise AdapterError(
                "RAW_FRAME_INVALID",
                "restored REST raw artifact has an unknown frame",
                False,
                "restore",
            )
        cursor = len(_RAW_MARKER)
        pages: list[list[object]] = []
        while cursor < len(raw_payload):
            if cursor + 8 > len(raw_payload):
                raise AdapterError(
                    "RAW_FRAME_TRUNCATED",
                    "restored REST raw frame is truncated",
                    False,
                    "restore",
                )
            size = int.from_bytes(raw_payload[cursor : cursor + 8], "big")
            cursor += 8
            if size < 0 or cursor + size > len(raw_payload):
                raise AdapterError(
                    "RAW_FRAME_TRUNCATED",
                    "restored REST raw page is truncated",
                    False,
                    "restore",
                )
            body = raw_payload[cursor : cursor + size]
            cursor += size
            try:
                page = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AdapterError(
                    "MALFORMED_JSON",
                    "restored REST page is malformed",
                    False,
                    "restore",
                ) from exc
            if not isinstance(page, list):
                raise AdapterError(
                    "SOURCE_SCHEMA_CHANGED",
                    "restored historical page is not a list",
                    False,
                    "restore",
                )
            pages.append(page)
        return pages

    @staticmethod
    def _safe_archive_rows(payload: bytes) -> list[list[str]]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise AdapterError(
                "TRUNCATED_ARCHIVE",
                "official archive is not a valid ZIP",
                False,
                "verify_raw",
            ) from exc
        names = archive.namelist()
        if len(names) != 1 or _SAFE_ZIP_NAME.fullmatch(names[0]) is None:
            raise AdapterError(
                "UNSAFE_ARCHIVE",
                "archive contains unsafe or unexpected members",
                False,
                "verify_raw",
            )
        info = archive.getinfo(names[0])
        if info.file_size > 2_000_000_000 or info.compress_size <= 0:
            raise AdapterError(
                "ARCHIVE_SIZE_LIMIT",
                "archive exceeds safety limits",
                False,
                "verify_raw",
            )
        if info.file_size / info.compress_size > 200:
            raise AdapterError(
                "ARCHIVE_RATIO_LIMIT",
                "archive compression ratio exceeds safety limit",
                False,
                "verify_raw",
            )
        with archive.open(info) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")
            return [row for row in csv.reader(text)]

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Binance adapter clock must return timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be non-empty")
        normalized = symbol.strip().upper().replace("/", "").replace("-", "")
        if not re.fullmatch(r"[A-Z0-9]{5,30}", normalized):
            raise ValueError("symbol contains unsupported characters")
        return normalized

    @staticmethod
    def _next_month(value: datetime) -> datetime:
        if value.month == 12:
            return datetime(value.year + 1, 1, 1, tzinfo=UTC)
        return datetime(value.year, value.month + 1, 1, tzinfo=UTC)

    @staticmethod
    def _rest_base(market_type: MarketType) -> str:
        return _SPOT_API if market_type is MarketType.SPOT else _USDTM_API

    @staticmethod
    def _monthly_archive_uri(
        instrument: InstrumentReference,
        timeframe: str,
        year: int,
        month: int,
    ) -> str:
        market_path = "spot" if instrument.market_type is MarketType.SPOT else "futures/um"
        filename = f"{instrument.symbol}-{timeframe}-{year:04d}-{month:02d}.zip"
        return (
            f"{_ARCHIVE}/data/{market_path}/monthly/klines/"
            f"{instrument.symbol}/{timeframe}/{filename}"
        )
