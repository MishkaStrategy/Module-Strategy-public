from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_data_ingestion.aggregation import (
    aggregate_klines,
    aggregate_klines_stream,
)
from market_data_ingestion.canonical import sha256_canonical
from market_data_ingestion.records import KlineRecord, normalized_record_from_row
from market_data_ingestion.timeframes import floor_timeframe


def _random_klines(seed: int) -> tuple[KlineRecord, ...]:
    generator = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    count = generator.randint(1, 120)
    records: list[KlineRecord] = []
    price = Decimal("100")
    for index in range(count):
        movement = Decimal(generator.randint(-20, 20)) / Decimal("10")
        close = max(Decimal("1"), price + movement)
        high = max(price, close) + Decimal(generator.randint(0, 10)) / Decimal("10")
        low = min(price, close) - Decimal(generator.randint(0, 9)) / Decimal("10")
        low = max(Decimal("0.1"), low)
        volume = Decimal(generator.randint(0, 1000)) / Decimal("10")
        open_time = start + timedelta(minutes=5 * index)
        records.append(
            KlineRecord(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=5, milliseconds=-1),
                open=format(price, "f"),
                high=format(high, "f"),
                low=format(low, "f"),
                close=format(close, "f"),
                volume=format(volume, "f"),
                quote_volume=format(volume * close, "f"),
                trade_count=generator.randint(0, 1000),
                taker_buy_base_volume=format(volume / Decimal("2"), "f"),
                taker_buy_quote_volume=format(
                    volume * close / Decimal("2"),
                    "f",
                ),
                symbol="BTCUSDT",
                timeframe="5m",
                exchange_id="fake",
                market_type="usdt_m_perpetual",
                source_kind="fake",
            )
        )
        price = close
    return tuple(record for record in records if generator.random() >= 0.08)


def test_randomized_streaming_aggregation_matches_reference() -> None:
    for seed in range(100):
        records = _random_klines(seed)
        for target in ("15m", "1h"):
            expected = aggregate_klines(records, target_timeframe=target)
            actual = tuple(aggregate_klines_stream(records, target_timeframe=target))
            assert actual == expected, (seed, target)


def test_randomized_kline_codec_round_trip() -> None:
    for seed in range(100, 200):
        for record in _random_klines(seed)[:10]:
            restored = normalized_record_from_row(
                record.data_type,
                record.to_row(),
            )
            assert restored == record


def test_canonical_hash_is_independent_of_mapping_insertion_order() -> None:
    generator = random.Random(20260727)
    items = [(f"key_{index}", index) for index in range(50)]
    expected = sha256_canonical(dict(items))
    for _ in range(100):
        generator.shuffle(items)
        assert sha256_canonical(dict(items)) == expected


def test_floor_timeframe_is_idempotent_and_never_moves_forward() -> None:
    generator = random.Random(7)
    origin = datetime(2020, 1, 1, tzinfo=UTC)
    for _ in range(1000):
        value = origin + timedelta(seconds=generator.randint(0, 500_000_000))
        for timeframe in ("5m", "15m", "1h", "4h", "1d"):
            floored = floor_timeframe(value, timeframe)
            assert floored <= value
            assert floor_timeframe(floored, timeframe) == floored
