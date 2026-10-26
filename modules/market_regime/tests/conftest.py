from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

import pytest
from modular_core import Candle, MarketView
from market_regime import MarketRegimeModule


@pytest.fixture
def start() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def small_kwargs() -> dict[str, object]:
    return {
        "timeframe": "1h",
        "fast_ema_period": 2,
        "slow_ema_period": 4,
        "atr_period": 3,
        "volatility_baseline_period": 5,
        "efficiency_period": 3,
        "slope_lookback": 2,
        "regime_confirmation_bars": 1,
        "volatility_confirmation_bars": 1,
    }


@pytest.fixture
def module(small_kwargs: dict[str, object]) -> MarketRegimeModule:
    return MarketRegimeModule(**small_kwargs)


def candle(
    start: datetime,
    index: int,
    close: float,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    minutes: int | None = None,
    spread: float = 1.0,
    volume: float = 10.0,
) -> Candle:
    timestamp = start + (
        timedelta(minutes=minutes * index) if minutes is not None else timedelta(hours=index)
    )
    return Candle(
        timestamp=timestamp,
        open=max(1e-12, close - spread * 0.1),
        high=close + spread,
        low=max(1e-12, close - spread),
        close=close,
        volume=volume,
        symbol=symbol,
        timeframe=timeframe,
    )


def view_for(
    base: Candle,
    *,
    target: Candle | None = None,
    data_gap: bool = False,
    aliases: dict[str, tuple[Candle, ...]] | None = None,
) -> MarketView:
    timeframe_bars: dict[str, tuple[Candle, ...]] = {}
    if target is not None:
        timeframe_bars[target.timeframe] = (target,)
    elif base.timeframe.strip().lower() == "1h":
        timeframe_bars[base.timeframe] = (base,)
    if aliases:
        timeframe_bars.update(aliases)
    return MarketView(
        base_bars=(base,),
        timeframe_bars=timeframe_bars,
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
    )


def feed(module: MarketRegimeModule, bars: Iterable[Candle]) -> list:
    return [module.on_bar(view_for(bar)) for bar in bars]


def feature_trace(decisions: Iterable) -> list[tuple]:
    return [
        tuple(
            (
                feature.key,
                feature.feature_id,
                feature.created_at,
                feature.valid_until,
                feature.value,
            )
            for feature in decision.features
        )
        for decision in decisions
    ]
