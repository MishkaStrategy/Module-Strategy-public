from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from modular_core import Candle, MarketView
from strategy_modules.liquidity_sweep import LiquiditySweepModule

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _state_with_bar() -> dict[str, object]:
    module = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    item = Candle(
        NOW,
        10,
        11,
        9,
        10,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    module.on_bar(
        MarketView(
            base_bars=(item,),
            timeframe_bars={},
            index=0,
            cash=10_000,
            equity=10_000,
            position=None,
            pending_orders=(),
        )
    )
    return deepcopy(dict(module.snapshot_state()))


def test_oversized_bar_container_is_rejected_before_element_parsing() -> None:
    module = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    state = deepcopy(dict(module.snapshot_state()))
    state["sequence"] = 4
    state["stats"]["processed_bars"] = 4  # type: ignore[index]
    state["bars"] = (object(), object(), object(), object())
    with pytest.raises(ValueError, match="more buffered bars"):
        module.restore_state(state)


def test_oversized_level_container_is_rejected_before_element_parsing() -> None:
    module = LiquiditySweepModule(max_active_levels=1)
    state = deepcopy(dict(module.snapshot_state()))
    state["levels"] = (object(), object())
    with pytest.raises(ValueError, match="more active levels"):
        module.restore_state(state)


def test_non_string_root_state_key_is_rejected_atomically() -> None:
    module = LiquiditySweepModule()
    before = module.snapshot_state()
    state = deepcopy(dict(before))
    state[1] = "unexpected"  # type: ignore[index]
    with pytest.raises(ValueError, match="keys must be strings"):
        module.restore_state(state)
    assert module.snapshot_state() == before


def test_non_string_nested_bar_key_is_rejected_atomically() -> None:
    state = _state_with_bar()
    state["bars"][0][1] = "unexpected"  # type: ignore[index]
    target = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="keys must be strings"):
        target.restore_state(state)
    assert target.snapshot_state() == before


def test_non_string_stats_key_is_rejected_atomically() -> None:
    module = LiquiditySweepModule()
    before = module.snapshot_state()
    state = deepcopy(dict(before))
    state["stats"][1] = 0  # type: ignore[index]
    with pytest.raises(ValueError, match="keys must be strings"):
        module.restore_state(state)
    assert module.snapshot_state() == before
