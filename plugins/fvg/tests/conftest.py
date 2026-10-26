from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import pytest
from modular_core import Candle, MarketView

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def candle(
    index: int,
    *,
    timeframe: str = "15m",
    symbol: str = "BTCUSDT",
    open_: float = 95.0,
    high: float = 100.0,
    low: float = 90.0,
    close: float = 95.0,
) -> Candle:
    minutes = 15 if timeframe == "15m" else 5 if timeframe == "5m" else 240
    return Candle(
        timestamp=START + timedelta(minutes=minutes * index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        symbol=symbol,
        timeframe=timeframe,
    )


def bullish_triplet(*, symbol: str = "BTCUSDT", timeframe: str = "15m") -> tuple[Candle, Candle, Candle]:
    return (
        candle(0, timeframe=timeframe, symbol=symbol, open_=95, high=100, low=90, close=95),
        candle(1, timeframe=timeframe, symbol=symbol, open_=96, high=102, low=91, close=97),
        candle(2, timeframe=timeframe, symbol=symbol, open_=106, high=110, low=105, close=107),
    )


def bearish_triplet(*, symbol: str = "BTCUSDT", timeframe: str = "15m") -> tuple[Candle, Candle, Candle]:
    return (
        candle(0, timeframe=timeframe, symbol=symbol, open_=105, high=110, low=100, close=105),
        candle(1, timeframe=timeframe, symbol=symbol, open_=104, high=109, low=98, close=103),
        candle(2, timeframe=timeframe, symbol=symbol, open_=94, high=95, low=90, close=92),
    )


def monotonic_bars(count: int, *, symbol: str = "BTCUSDT", timeframe: str = "15m") -> list[Candle]:
    bars: list[Candle] = []
    for index in range(count):
        base = 100.0 + 3.0 * index
        bars.append(
            candle(
                index,
                timeframe=timeframe,
                symbol=symbol,
                open_=base + 1,
                high=base + 2,
                low=base,
                close=base + 1,
            )
        )
    return bars


def view_for(
    bars: Sequence[Candle],
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
    data_gap: bool = False,
    base_close_offset_minutes: int = 0,
) -> MarketView:
    if bars:
        visible_close = bars[-1].close_time + timedelta(minutes=base_close_offset_minutes)
    else:
        visible_close = START + timedelta(minutes=5)
    base = Candle(
        timestamp=visible_close - timedelta(minutes=5),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
        symbol=symbol,
        timeframe="5m",
    )
    return MarketView(
        base_bars=(base,),
        timeframe_bars={timeframe: tuple(bars)},
        index=max(0, len(bars) - 1),
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
    )


@dataclass(frozen=True)
class FakeCandle:
    timestamp: datetime
    close_time: datetime
    high: float
    low: float
    close: float
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"


@pytest.fixture
def bull() -> tuple[Candle, Candle, Candle]:
    return bullish_triplet()
