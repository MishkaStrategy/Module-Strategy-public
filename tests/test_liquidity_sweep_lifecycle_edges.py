from __future__ import annotations

from datetime import datetime, timedelta, timezone

from modular_core import Candle, MarketView
from strategy_modules.liquidity_sweep import LiquiditySweepModule

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candle(index: int) -> Candle:
    return Candle(
        START + timedelta(minutes=5 * index),
        10.0,
        11.0,
        9.0,
        10.0,
        1.0,
        "BTCUSDT",
        "5m",
    )


def _view(history: list[Candle]) -> MarketView:
    return MarketView(
        base_bars=tuple(history),
        timeframe_bars={},
        index=len(history) - 1,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
    )


def test_direct_post_restore_continuation_consumes_startup_allowance() -> None:
    source = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    history = [_candle(0), _candle(1), _candle(2)]
    for index in range(len(history)):
        source.on_bar(_view(history[: index + 1]))

    restored = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    restored.restore_state(source.snapshot_state())
    restored.on_bar(_view([*history, _candle(3)]))
    assert restored.snapshot_state()["sequence"] == 4

    restored.on_start()
    assert restored.snapshot_state()["sequence"] == 0
    assert restored._runtime_restore_count == 0


def test_core_start_after_restore_still_preserves_state_once() -> None:
    source = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    source.on_bar(_view([_candle(0)]))

    restored = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    restored.restore_state(source.snapshot_state())
    before = restored.snapshot_state()
    restored.on_start()
    assert restored.snapshot_state() == before

    restored.on_start()
    assert restored.snapshot_state()["sequence"] == 0
