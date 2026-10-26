from copy import deepcopy

import pytest
from modular_core import SetupStatus

from strategy_modules.breakout_return import (
    BreakoutReturnDataError,
    BreakoutReturnModule,
    BreakoutReturnPhase,
    BreakoutReturnStateError,
)


def process(module, bars, make_view, *, features=(), gaps=None):
    decisions = []
    visible = []
    gaps = gaps or [False] * len(bars)
    for bar, gap in zip(bars, gaps):
        visible.append(bar)
        decisions.append(module.on_bar(make_view(visible, features=features, data_gap=gap)))
    return decisions


def seed_and_breakout(make_bar):
    return [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
        make_bar(3, 101, 104, 100.5, 103),
    ]


def test_duplicate_event_is_idempotent(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = seed_and_breakout(make_bar)
    process(module, bars, make_view)
    before = module.snapshot_state()
    duplicate = module.on_bar(make_view(bars))
    assert duplicate.skips == ("breakout_return.duplicate_event_ignored",)
    assert module.snapshot_state() == before


def test_contradictory_duplicate_raises(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=2)
    first = make_bar(0, 95, 100, 90, 96)
    module.on_bar(make_view([first]))
    changed = make_bar(0, 95, 101, 90, 96)
    with pytest.raises(BreakoutReturnDataError, match="contradictory duplicate"):
        module.on_bar(make_view([changed]))


def test_out_of_order_raises(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=2)
    later = make_bar(2, 95, 100, 90, 96)
    earlier = make_bar(1, 95, 100, 90, 96)
    module.on_bar(make_view([later]))
    with pytest.raises(BreakoutReturnDataError, match="out-of-order"):
        module.on_bar(make_view([earlier]))


def test_symbol_or_timeframe_switch_raises(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=2)
    first = make_bar(0, 95, 100, 90, 96)
    module.on_bar(make_view([first]))
    other = make_bar(1, 95, 100, 90, 96, symbol="ETHUSDT")
    with pytest.raises(BreakoutReturnDataError, match="cannot switch"):
        module.on_bar(make_view([other]))


def test_gap_invalidates_active_and_resets_warmup(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = seed_and_breakout(make_bar) + [make_bar(4, 103, 105, 102, 104)]
    decisions = process(module, bars, make_view, gaps=[False, False, False, False, True])
    gap = decisions[-1]
    assert gap.setups[0].status is SetupStatus.INVALIDATED
    assert gap.skips == ("breakout_return.data_gap_reset",)
    next_bar = make_bar(5, 104, 106, 103, 105)
    after = module.on_bar(make_view(bars + [next_bar]))
    assert after.skips == ("breakout_return.insufficient_reference",)


def test_expiry_allows_exact_ttl_then_expires_next_bar(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, max_return_bars=2)
    bars = seed_and_breakout(make_bar) + [
        make_bar(4, 103, 105, 102, 104),
        make_bar(5, 104, 106, 103, 105),
        make_bar(6, 105, 107, 104, 106),
    ]
    decisions = process(module, bars, make_view)
    assert decisions[-2].setups == ()
    assert decisions[-1].setups[0].status is SetupStatus.EXPIRED


def test_extreme_update_versions_active_feature(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = seed_and_breakout(make_bar)
    decisions = process(module, bars, make_view)
    active_feature = decisions[-1].features[0]
    next_bar = make_bar(4, 103, 108, 102, 104)
    view = make_view(bars + [next_bar], features=(active_feature,))
    decision = module.on_bar(view)
    assert decision.features[0].feature_id.endswith(":active:1")
    assert decision.features[0].value["breakout_extreme"] == 108.0
    assert decision.feature_deletes[0].feature_id.endswith(":active:0")


def test_snapshot_restore_continues_active_setup(make_bar, make_view):
    original = BreakoutReturnModule(lookback_bars=3)
    bars = seed_and_breakout(make_bar)
    decisions = process(original, bars, make_view)
    state = original.snapshot_state()

    restored = BreakoutReturnModule(lookback_bars=3)
    restored.restore_state(state)
    restored.on_start()
    return_bar = make_bar(4, 99.5, 101, 98.5, 99)
    decision = restored.on_bar(make_view(bars + [return_bar]))
    assert decision.signals
    assert decision.signals[0].metadata["setup_id"] == decisions[-1].setups[0].setup_id
    assert restored.phase is BreakoutReturnPhase.SCANNING


@pytest.mark.parametrize(
    "mutator",
    [
        lambda state: state.update(payload_version=999),
        lambda state: state.update(sequence=-1),
        lambda state: state.update(sequence=1.5),
        lambda state: state.update(last_bar_signature=["bad"]),
        lambda state: state.update(maxima=[[2, "100"], [1, "90"]]),
    ],
)
def test_corrupt_state_is_rejected(make_bar, make_view, mutator):
    module = BreakoutReturnModule(lookback_bars=3)
    process(module, seed_and_breakout(make_bar), make_view)
    state = deepcopy(dict(module.snapshot_state()))
    mutator(state)
    with pytest.raises(BreakoutReturnStateError):
        BreakoutReturnModule(lookback_bars=3).restore_state(state)


def test_parallel_instances_are_independent(make_bar, make_view):
    btc = BreakoutReturnModule(lookback_bars=2)
    eth = BreakoutReturnModule(lookback_bars=2)
    btc_bars = [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 101, 104, 100.5, 103),
    ]
    eth_bars = [
        make_bar(0, 50, 55, 45, 51, symbol="ETHUSDT"),
        make_bar(1, 51, 54, 46, 50, symbol="ETHUSDT"),
        make_bar(2, 44, 44.5, 41, 42, symbol="ETHUSDT"),
    ]
    assert process(btc, btc_bars, make_view)[-1].setups[0].side.value == "sell"
    assert process(eth, eth_bars, make_view)[-1].setups[0].side.value == "buy"
    assert btc.snapshot_state()["bound_symbol"] == "BTCUSDT"
    assert eth.snapshot_state()["bound_symbol"] == "ETHUSDT"
