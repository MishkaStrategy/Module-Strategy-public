from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from modular_core import Candle
from market_regime import MarketRegimeModule

from conftest import feature_trace, feed


def random_walk(count: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    price = 10_000.0
    bars: list[Candle] = []
    for i in range(count):
        move = rng.uniform(-25, 25)
        close = max(0.01, price + move)
        wick = rng.uniform(0.01, 10)
        bars.append(
            Candle(
                timestamp=start + timedelta(hours=i),
                open=price,
                high=max(price, close) + wick,
                low=max(0.001, min(price, close) - wick),
                close=close,
                volume=rng.uniform(0, 1_000_000),
                symbol="BTCUSDT",
                timeframe="1h",
            )
        )
        price = close
    return bars


def test_random_walk_deterministic(small_kwargs) -> None:
    bars = random_walk(2_000, 20260721)
    first = MarketRegimeModule(**small_kwargs)
    second = MarketRegimeModule(**small_kwargs)
    assert feature_trace(feed(first, bars)) == feature_trace(feed(second, bars))
    assert first.snapshot_state() == second.snapshot_state()


def test_random_walk_outputs_finite(small_kwargs) -> None:
    module = MarketRegimeModule(**small_kwargs)
    decisions = feed(module, random_walk(2_000, 7))
    count = 0
    for decision in decisions:
        for feature in decision.features:
            count += 1
            if feature.key == "market_regime.current":
                assert 0 <= feature.value["confidence"] <= 1
                assert all(math.isfinite(x) for x in feature.value["metrics"].values())
    assert count > 0


def test_extreme_finite_prices(small_kwargs) -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars: list[Candle] = []
    for i in range(100):
        exponent = -6 + i % 18
        close = 10.0**exponent
        bars.append(
            Candle(
                timestamp=start + timedelta(hours=i),
                open=close * 0.99,
                high=close * 1.01,
                low=max(close * 0.98, 1e-12),
                close=close,
                volume=10.0 ** min(exponent + 6, 18),
                symbol="SYNTHETIC",
                timeframe="1h",
            )
        )
    decisions = feed(MarketRegimeModule(**small_kwargs), bars)
    for decision in decisions:
        for feature in decision.features:
            if feature.key == "market_regime.current":
                assert all(math.isfinite(x) for x in feature.value["metrics"].values())


def test_restart_random_walk(small_kwargs) -> None:
    bars = random_walk(1_000, 101)
    expected = feature_trace(feed(MarketRegimeModule(**small_kwargs), bars))
    for cut in (7, 53, 211, 499, 877):
        before = MarketRegimeModule(**small_kwargs)
        prefix = feed(before, bars[:cut])
        after = MarketRegimeModule(**small_kwargs)
        after.restore_state(before.snapshot_state())
        suffix = feed(after, bars[cut:])
        assert feature_trace(prefix + suffix) == expected
