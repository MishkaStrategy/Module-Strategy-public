from __future__ import annotations

import pytest
from modular_core import Candle

from conftest import bullish_triplet, view_for
from modular_strategy_fvg import FvgDataConflictError, FvgModule


def _change_source_b_volume(bars: list[Candle]) -> None:
    b = bars[-2]
    bars[-2] = Candle(
        b.timestamp,
        b.open,
        b.high,
        b.low,
        b.close,
        b.volume + 1,
        b.symbol,
        b.timeframe,
    )


def test_duplicate_noop_detects_changed_retained_history_immediately() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))
    before = module.snapshot_state()
    _change_source_b_volume(bars)

    with pytest.raises(
        FvgDataConflictError,
        match="historical candle changed",
    ):
        module.on_bar(view_for(bars))

    assert module.snapshot_state() == before


def test_pre_fvg_duplicate_path_detects_changed_retained_history() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(
        timeframes=("15m",),
        pre_fvg_enabled=True,
    )
    module.on_bar(view_for(bars))
    before = module.snapshot_state()
    _change_source_b_volume(bars)

    with pytest.raises(
        FvgDataConflictError,
        match="historical candle changed",
    ):
        module.on_bar(view_for(bars))

    assert module.snapshot_state() == before


def test_first_replay_after_restore_detects_changed_retained_history() -> None:
    original_bars = list(bullish_triplet())
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(original_bars))

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(source.snapshot_state())
    before = restored.snapshot_state()

    changed_bars = list(original_bars)
    _change_source_b_volume(changed_bars)
    with pytest.raises(
        FvgDataConflictError,
        match="historical candle changed",
    ):
        restored.on_bar(view_for(changed_bars))

    assert restored.snapshot_state() == before


def test_duplicate_noop_with_unchanged_retained_history_is_idempotent() -> None:
    bars = bullish_triplet()
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))

    decision = module.on_bar(view_for(bars))

    assert decision.features == ()
    assert decision.feature_deletes == ()
    assert decision.diagnostics["duplicates"] == 1
