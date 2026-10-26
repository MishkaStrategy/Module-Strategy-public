from datetime import datetime, timedelta, timezone

import pytest

from modular_core import Candle
from modular_core.timeframes import TimeframeSpec, TimeframeStore


def _bar(
    minute: int,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
) -> Candle:
    return Candle(
        datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
        100,
        101,
        99,
        100,
        1,
        symbol,
        timeframe,
    )


def test_store_preflights_every_aggregator_before_first_mutation() -> None:
    store = TimeframeStore(
        (
            TimeframeSpec(2, "10m"),
            TimeframeSpec(3, "15m"),
        ),
        expected_base_interval=timedelta(minutes=5),
    )
    later = store._aggregators["15m"]
    later.push(_bar(0, symbol="ETHUSDT"))

    with pytest.raises(ValueError, match="mix symbols"):
        store.push(_bar(0))

    later.reset()
    assert store.push(_bar(0)) is False
    assert len(store.snapshot()["10m"]) == 0
    assert store.push(_bar(5)) is False
    assert len(store.snapshot()["10m"]) == 1


def test_store_preflight_preserves_all_histories_on_rejected_event() -> None:
    store = TimeframeStore(
        (
            TimeframeSpec(1, "5m-copy"),
            TimeframeSpec(2, "10m"),
        ),
        expected_base_interval=timedelta(minutes=5),
    )
    store.push(_bar(0))
    before = {name: tuple(series) for name, series in store.snapshot().items()}

    with pytest.raises(ValueError, match="duplicate"):
        store.push(_bar(0))

    after = {name: tuple(series) for name, series in store.snapshot().items()}
    assert after == before
