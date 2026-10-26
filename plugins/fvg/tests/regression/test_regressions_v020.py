from __future__ import annotations

from datetime import timedelta

import pytest
from modular_core import Candle

from conftest import bullish_triplet, monotonic_bars, view_for
from modular_strategy_fvg import FvgDataConflictError, FvgModule


def test_regression_conflicting_duplicate_no_longer_silent() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))
    c = bars[-1]
    bars[-1] = Candle(c.timestamp, c.open, c.high, c.low, c.close + 0.1, c.volume, c.symbol, c.timeframe)
    with pytest.raises(FvgDataConflictError):
        module.on_bar(view_for(bars))


def test_regression_gap_does_not_resurrect_old_history() -> None:
    bars = monotonic_bars(8)
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars[:5]))
    module.on_bar(view_for(bars[:5], data_gap=True))
    assert not module.on_bar(view_for(bars[:5])).features
    assert not module.on_bar(view_for(bars[:6])).features
    assert not module.on_bar(view_for(bars[:7])).features
    assert module.on_bar(view_for(bars[:8])).features


def test_regression_timezone_offset_does_not_change_identity() -> None:
    bars = list(bullish_triplet())
    first = FvgModule(timeframes=("15m",)).on_bar(view_for(bars))
    shifted = [
        Candle(
            bar.timestamp.astimezone(bar.timestamp.tzinfo),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.symbol,
            bar.timeframe,
        )
        for bar in bars
    ]
    second = FvgModule(timeframes=("15m",)).on_bar(view_for(shifted))
    assert first.features[0].feature_id == second.features[0].feature_id


def test_regression_restore_is_atomic_on_late_zone_error() -> None:
    target = FvgModule(timeframes=("15m",))
    target.on_bar(view_for(bullish_triplet()))
    before = target.snapshot_state()
    bad = dict(before)
    bad["zones"] = [
        dict(before["zones"][0]),
        {
            **dict(before["zones"][0]),
            "zone_id": "bad",
            "created_at": (bullish_triplet()[-1].close_time + timedelta(days=1)).isoformat(),
        },
    ]
    with pytest.raises(Exception):
        target.restore_state(bad)
    assert target.snapshot_state() == before


def test_regression_historical_source_mutation_is_rejected_atomically() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars[:2]))
    before = module.snapshot_state()
    a = bars[0]
    mutated_a = Candle(
        a.timestamp,
        a.open,
        106,
        a.low,
        a.close,
        a.volume,
        a.symbol,
        a.timeframe,
    )
    with pytest.raises(FvgDataConflictError, match="historical candle"):
        module.on_bar(view_for((mutated_a, bars[1], bars[2])))
    assert module.snapshot_state() == before


def test_regression_gap_with_missing_timeframe_blocks_old_history_recreation() -> None:
    bars = list(bullish_triplet(timeframe="4h"))
    module = FvgModule(timeframes=("15m", "4h"))
    first = module.on_bar(view_for(bars, timeframe="4h"))
    assert first.features

    regular = view_for(bars, timeframe="4h")
    gap_view = regular.__class__(
        base_bars=regular.base_bars,
        timeframe_bars={"15m": ()},
        index=regular.index,
        cash=regular.cash,
        equity=regular.equity,
        position=regular.position,
        pending_orders=regular.pending_orders,
        data_gap=True,
    )
    module.on_bar(gap_view)

    replay = module.on_bar(view_for(bars, timeframe="4h"))
    assert not replay.features
    assert not module.active_zones


def test_regression_open_or_volume_change_is_a_conflicting_duplicate() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))
    before = module.snapshot_state()
    c = bars[-1]
    bars[-1] = Candle(
        c.timestamp,
        c.open + 0.5,
        c.high,
        c.low,
        c.close,
        c.volume + 1,
        c.symbol,
        c.timeframe,
    )
    with pytest.raises(FvgDataConflictError):
        module.on_bar(view_for(bars))
    assert module.snapshot_state() == before
