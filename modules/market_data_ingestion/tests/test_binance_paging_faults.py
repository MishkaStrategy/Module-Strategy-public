from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from market_data_ingestion.adapters.binance import BinanceAdapter
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, SourceKind
from market_data_ingestion.errors import AdapterError
from market_data_ingestion.network import HttpClient, HttpResponse
from market_data_ingestion.validation import validate_records


class TinyPageBinance(BinanceAdapter):
    def _rest_contract(self, chunk, instrument):  # type: ignore[no-untyped-def]
        path, params, _, step = super()._rest_contract(chunk, instrument)
        return path, params, 1, step


def _instrument() -> InstrumentReference:
    return InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )


def _row(at: datetime) -> list[object]:
    close = at + timedelta(minutes=5) - timedelta(milliseconds=1)
    return [
        int(at.timestamp() * 1000),
        "100",
        "102",
        "99",
        "101",
        "10",
        int(close.timestamp() * 1000),
        "1000",
        1,
        "4",
        "400",
    ]


def _chunk(end_minutes: int = 15) -> DownloadChunk:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return DownloadChunk(
        chunk_id="paging",
        data_type=DataType.KLINES,
        start=start,
        end=start + timedelta(minutes=end_minutes),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )


def test_empty_intermediate_page_is_preserved_as_detectable_gap() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    pages = iter(([_row(start)], [], [_row(start + timedelta(minutes=10))]))

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        body = json.dumps(next(pages)).encode()
        return HttpResponse(200, {}, body, url)

    adapter = TinyPageBinance(
        HttpClient(transport=transport),
        now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    )
    acquired = adapter.acquire(_chunk(), _instrument())
    assert len(acquired.records) == 2
    _, report = validate_records(
        acquired.records,
        data_type=DataType.KLINES,
        instrument=_instrument(),
        timeframe="5m",
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert report.gaps and report.gaps[0].missing_intervals == 1
    restored = adapter.restore(_chunk(), _instrument(), acquired.raw_payload)
    assert restored.records == acquired.records


def test_repeated_page_fails_before_infinite_cursor_loop() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    body = json.dumps([_row(start)]).encode()

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        return HttpResponse(200, {}, body, url)

    adapter = TinyPageBinance(
        HttpClient(transport=transport),
        now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(AdapterError, match="repeated the same page"):
        adapter.acquire(_chunk(10), _instrument())


def test_out_of_order_page_fails_closed() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    body = json.dumps([_row(start + timedelta(minutes=5)), _row(start)]).encode()

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        return HttpResponse(200, {}, body, url)

    adapter = BinanceAdapter(
        HttpClient(transport=transport),
        now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(AdapterError, match="strictly increasing"):
        adapter.acquire(_chunk(10), _instrument())


def test_malformed_numeric_field_is_mapped_to_structured_schema_error() -> None:
    row = _row(datetime(2026, 1, 1, tzinfo=UTC))
    row[0] = "not-an-epoch"

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        del timeout, headers
        return HttpResponse(200, {}, json.dumps([row]).encode(), url)

    adapter = BinanceAdapter(
        HttpClient(transport=transport),
        now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(AdapterError) as captured:
        adapter.acquire(_chunk(5), _instrument())
    assert captured.value.code == "SOURCE_SCHEMA_CHANGED"
    assert captured.value.stage == "normalize"
