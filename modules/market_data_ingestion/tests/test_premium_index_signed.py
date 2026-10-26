from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_ingestion.adapters.binance import BinanceAdapter
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, SourceKind
from market_data_ingestion.records import PriceKlineRecord


def _record(price_type: DataType, value: str) -> PriceKlineRecord:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return PriceKlineRecord(
        price_type=price_type,
        open_time=start,
        close_time=start + timedelta(minutes=5) - timedelta(milliseconds=1),
        open=value,
        high=value,
        low=value,
        close=value,
        symbol="BTCUSDT",
        timeframe="5m",
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL.value,
        source_kind=SourceKind.OFFICIAL_REST.value,
    )


def test_premium_index_accepts_signed_and_zero_ohlc() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    record = PriceKlineRecord(
        price_type=DataType.PREMIUM_INDEX,
        open_time=start,
        close_time=start + timedelta(minutes=5) - timedelta(milliseconds=1),
        open="-0.00010",
        high="0.00020",
        low="-0.00030",
        close="0",
        symbol="BTCUSDT",
        timeframe="5m",
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL.value,
        source_kind=SourceKind.OFFICIAL_REST.value,
    )

    assert Decimal(record.open) == Decimal("-0.0001")
    assert Decimal(record.high) == Decimal("0.0002")
    assert Decimal(record.low) == Decimal("-0.0003")
    assert record.close == "0"


@pytest.mark.parametrize(
    "price_type",
    [DataType.MARK_PRICE, DataType.INDEX_PRICE],
)
def test_mark_and_index_prices_remain_strictly_positive(
    price_type: DataType,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _record(price_type, "-0.0001")


def test_binance_parser_accepts_signed_premium_index_kline() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    close = start + timedelta(minutes=5) - timedelta(milliseconds=1)
    chunk = DownloadChunk(
        chunk_id="premium-signed",
        data_type=DataType.PREMIUM_INDEX,
        start=start,
        end=start + timedelta(minutes=5),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    row = [
        int(start.timestamp() * 1000),
        "-0.00010",
        "0.00020",
        "-0.00030",
        "0",
        "0",
        int(close.timestamp() * 1000),
    ]

    record = BinanceAdapter(now=lambda: datetime(2026, 1, 2, tzinfo=UTC))._parse_price_kline(
        row, chunk, instrument
    )

    assert record.price_type is DataType.PREMIUM_INDEX
    assert Decimal(record.open) == Decimal("-0.0001")
    assert record.close == "0"
