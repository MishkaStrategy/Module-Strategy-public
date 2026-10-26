from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core import Candle, Feature, FeatureView, MarketView


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def make_bar():
    def factory(
        index: int,
        open_: float,
        high: float,
        low: float,
        close: float,
        *,
        symbol: str = "BTCUSDT",
        timeframe: str = "5m",
    ) -> Candle:
        return Candle(
            START + timedelta(minutes=5 * index),
            open_,
            high,
            low,
            close,
            1.0,
            symbol,
            timeframe,
        )

    return factory


@pytest.fixture
def make_view():
    def factory(bars, *, features=(), data_gap=False):
        current = bars[-1]
        return MarketView(
            base_bars=tuple(bars),
            timeframe_bars={},
            index=len(bars) - 1,
            cash=10_000.0,
            equity=10_000.0,
            position=None,
            pending_orders=(),
            data_gap=data_gap,
            features=FeatureView(tuple(features), current.close_time),
        )

    return factory


@pytest.fixture
def range_feature(make_bar):
    bar = make_bar(0, 95, 100, 90, 95)
    return Feature(
        key="range.current",
        source_module="range_builder",
        created_at=bar.close_time,
        value={"high": 100.0, "low": 90.0, "range_id": "range-1"},
        symbol="BTCUSDT",
        timeframe="5m",
        feature_id="range-1",
    )
