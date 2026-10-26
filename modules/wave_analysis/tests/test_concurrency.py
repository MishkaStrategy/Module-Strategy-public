from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from modular_core import Candle

from tests.support import market_view
from wave_analysis import WaveAnalysisModule

START = datetime(2026, 5, 1, tzinfo=UTC)


def candle(index: int, price: float = 100.0) -> Candle:
    return Candle(
        START + timedelta(minutes=index),
        price,
        price + 1,
        price - 1,
        price,
        100,
        "BTCUSDT",
        "1m",
    )


def test_concurrent_duplicate_delivery_is_atomic() -> None:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m")
    item = candle(0)
    view = market_view((item,), {"1m": (item,)}, 0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _: module.on_bar(view), range(16)))

    state = module.snapshot_state()
    assert state["processed_base_events"] == 1
    assert state["duplicate_base_events"] == 15
    assert sum(bool(decision.features) for decision in decisions) <= 1
    assert sum("wave_analysis.duplicate_bar" in decision.skips for decision in decisions) == 15


def test_concurrent_checkpoint_reads_remain_restoreable() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.03",
    )
    items = [candle(index, 100 + (8 if index % 2 else -8)) for index in range(60)]
    snapshots: list[dict[str, object]] = []

    def feed() -> None:
        history: list[Candle] = []
        for index, item in enumerate(items):
            history.append(item)
            module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))

    def read() -> None:
        for _ in range(100):
            snapshots.append(dict(module.snapshot_state()))
            assert len(module.state_digest) == 64

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(feed), *(pool.submit(read) for _ in range(3))]
        for future in futures:
            future.result()

    assert snapshots
    restored = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.03",
    )
    restored.restore_state(module.snapshot_state())
    assert restored.state_digest == module.state_digest
