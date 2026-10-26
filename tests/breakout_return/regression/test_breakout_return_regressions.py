from __future__ import annotations

import json
import random
from copy import deepcopy

import pytest
from modular_core import SetupStatus

from strategy_modules.breakout_return import BreakoutReturnModule


def run(module, bars, make_view, *, features_by_bar=None, gaps=None):
    decisions = []
    visible = []
    features_by_bar = features_by_bar or [()] * len(bars)
    gaps = gaps or [False] * len(bars)
    for index, bar in enumerate(bars):
        visible.append(bar)
        decisions.append(
            module.on_bar(
                make_view(
                    visible,
                    features=features_by_bar[index],
                    data_gap=gaps[index],
                )
            )
        )
    return decisions


def seed(make_bar):
    return [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
    ]


def test_regression_current_bar_excluded_from_reference(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, breakout_confirmation="close")
    bars = seed(make_bar) + [make_bar(3, 99, 150, 98, 101)]
    decision = run(module, bars, make_view)[-1]
    assert decision.setups, "current high must not raise the reference boundary before detection"
    assert decision.setups[0].metadata["range_high"] == 100.0


def test_regression_equal_boundary_is_not_breakout(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, breakout_confirmation="close")
    bars = seed(make_bar) + [make_bar(3, 99, 101, 98, 100)]
    assert run(module, bars, make_view)[-1].setups == ()


def test_regression_return_cannot_confirm_on_breakout_bar(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, breakout_confirmation="close")
    bars = seed(make_bar) + [make_bar(3, 99, 104, 98, 103)]
    decision = run(module, bars, make_view)[-1]
    assert decision.setups[0].status is SetupStatus.ARMED
    assert decision.signals == ()


@pytest.mark.parametrize("depth", [0.02, 0.20])
def test_regression_return_depth_boundaries_are_inclusive(depth, make_bar, make_view):
    module = BreakoutReturnModule(
        lookback_bars=3,
        min_return_depth=0.02,
        max_return_depth=0.20,
        return_confirmation="close",
    )
    close = 100 - depth * 10
    bars = seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, close + 0.1, close + 0.5, close - 0.5, close),
    ]
    decision = run(module, bars, make_view)[-1]
    assert decision.setups[0].status is SetupStatus.CONFIRMED


def test_regression_gap_bar_cannot_arm_new_setup(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    breakout = make_bar(3, 101, 104, 100.5, 103)
    decisions = run(
        module,
        seed(make_bar) + [breakout],
        make_view,
        gaps=[False, False, False, True],
    )
    assert decisions[-1].setups == ()
    assert decisions[-1].skips == ("breakout_return.data_gap_reset",)


def test_regression_state_is_json_round_trip_safe(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    run(module, seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)], make_view)
    payload = json.loads(json.dumps(module.snapshot_state()))
    restored = BreakoutReturnModule(lookback_bars=3)
    restored.restore_state(payload)
    assert restored.snapshot_state() == module.snapshot_state()


def test_regression_duplicate_after_restore_is_idempotent(make_bar, make_view):
    bars = seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)]
    first = BreakoutReturnModule(lookback_bars=3)
    run(first, bars, make_view)
    restored = BreakoutReturnModule(lookback_bars=3)
    restored.restore_state(first.snapshot_state())
    before = restored.snapshot_state()
    decision = restored.on_bar(make_view(bars))
    assert decision.skips == ("breakout_return.duplicate_event_ignored",)
    assert restored.snapshot_state() == before


def test_regression_active_delete_targets_only_previous_revision(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)]
    armed = run(module, bars, make_view)[-1]
    active = armed.features[0]
    extension = make_bar(4, 103, 108, 102, 104)
    decision = module.on_bar(make_view(bars + [extension], features=(active,)))
    assert decision.feature_deletes[0].feature_id != decision.features[0].feature_id
    assert decision.feature_deletes[0].feature_id.endswith(":active:0")
    assert decision.features[0].feature_id.endswith(":active:1")


def test_regression_rolling_extrema_match_naive_window(make_bar, make_view):
    rng = random.Random(17)
    lookback = 25
    module = BreakoutReturnModule(
        lookback_bars=lookback,
        direction="above",
        breakout_buffer_bps=5000,
    )
    bars = []
    price = 100.0
    for index in range(400):
        price += rng.uniform(-0.2, 0.2)
        high = price + rng.uniform(0.1, 1.0)
        low = price - rng.uniform(0.1, 1.0)
        open_ = max(low, min(high, price + rng.uniform(-0.1, 0.1)))
        close = max(low, min(high, price + rng.uniform(-0.1, 0.1)))
        bar = make_bar(index, open_, high, low, close)
        bars.append(bar)
        decision = module.on_bar(make_view(bars))
        if index >= lookback:
            expected_high = max(item.high for item in bars[index - lookback : index])
            expected_low = min(item.low for item in bars[index - lookback : index])
            assert decision.diagnostics["range_high"] == pytest.approx(expected_high)
            assert decision.diagnostics["range_low"] == pytest.approx(expected_low)


def test_regression_corrupt_active_side_is_rejected(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    run(module, seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)], make_view)
    state = deepcopy(dict(module.snapshot_state()))
    state["active"] = dict(state["active"])
    state["active"]["signal_side"] = "buy"
    with pytest.raises(Exception):
        BreakoutReturnModule(lookback_bars=3).restore_state(state)
