from modular_core import SetupStatus, Side

from strategy_modules.breakout_return import BreakoutReturnModule, BreakoutReturnPhase


def feed(module, bars, make_view, *, feature_sets=None, gaps=None):
    decisions = []
    feature_sets = feature_sets or [()] * len(bars)
    gaps = gaps or [False] * len(bars)
    visible = []
    for i, bar in enumerate(bars):
        visible.append(bar)
        decisions.append(
            module.on_bar(
                make_view(
                    visible,
                    features=feature_sets[i],
                    data_gap=gaps[i],
                )
            )
        )
    return decisions


def rolling_seed(make_bar):
    return [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
    ]


def test_above_body_breakout_arms_contrarian_sell(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = rolling_seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)]
    decision = feed(module, bars, make_view)[-1]
    assert decision.setups[0].status is SetupStatus.ARMED
    assert decision.setups[0].side is Side.SELL
    assert decision.setups[0].metadata["range_high"] == 100.0
    assert decision.features[0].key == module.ACTIVE_FEATURE
    assert not decision.orders
    assert module.phase is BreakoutReturnPhase.WAITING_RETURN


def test_below_body_breakout_arms_contrarian_buy(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = rolling_seed(make_bar) + [make_bar(3, 89, 89.5, 86, 87)]
    decision = feed(module, bars, make_view)[-1]
    assert decision.setups[0].side is Side.BUY
    assert decision.setups[0].metadata["breakout_direction"] == "below"


def test_wick_or_partial_body_does_not_arm_in_body_mode(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = rolling_seed(make_bar) + [make_bar(3, 99, 104, 98, 103)]
    decision = feed(module, bars, make_view)[-1]
    assert decision.setups == ()
    assert module.phase is BreakoutReturnPhase.SCANNING


def test_close_mode_accepts_close_only_breakout(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, breakout_confirmation="close")
    bars = rolling_seed(make_bar) + [make_bar(3, 99, 104, 98, 103)]
    assert feed(module, bars, make_view)[-1].setups


def test_above_return_confirms_sell_and_transient_feature(make_bar, make_view):
    module = BreakoutReturnModule(
        lookback_bars=3,
        min_return_depth=0.02,
        max_return_depth=0.20,
    )
    bars = rolling_seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 99.5, 102, 98.5, 99),
    ]
    decisions = feed(module, bars, make_view)
    confirmed = decisions[-1]
    assert confirmed.setups[0].status is SetupStatus.CONFIRMED
    assert confirmed.signals[0].side is Side.SELL
    assert confirmed.signals[0].metadata["return_depth"] == 0.1
    assert confirmed.features[0].key == module.CONFIRMED_FEATURE
    assert confirmed.features[0].valid_until == bars[-1].close_time
    assert confirmed.feature_deletes[0].key == module.ACTIVE_FEATURE
    assert module.phase is BreakoutReturnPhase.SCANNING


def test_below_return_confirms_buy(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = rolling_seed(make_bar) + [
        make_bar(3, 89, 89.5, 86, 87),
        make_bar(4, 90.5, 92.5, 88, 91),
    ]
    confirmed = feed(module, bars, make_view)[-1]
    assert confirmed.signals[0].side is Side.BUY
    assert confirmed.signals[0].metadata["return_depth"] == 0.1


def test_shallow_return_waits_and_then_confirms(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, min_return_depth=0.05)
    bars = rolling_seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 99.9, 101, 99.7, 99.8),
        make_bar(5, 99.5, 100, 98.8, 99),
    ]
    decisions = feed(module, bars, make_view)
    assert decisions[-2].signals == ()
    assert decisions[-2].diagnostics["return_depth"] == 0.02
    assert decisions[-1].signals


def test_return_too_deep_is_rejected(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, max_return_depth=0.2)
    bars = rolling_seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 97, 99, 96, 97),
    ]
    decision = feed(module, bars, make_view)[-1]
    assert decision.setups[0].status is SetupStatus.REJECTED
    assert decision.skips == ("breakout_return.return_too_deep",)


def test_body_return_requires_open_and_close_inside(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, return_confirmation="body")
    bars = rolling_seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 101, 102, 98.5, 99),
    ]
    decision = feed(module, bars, make_view)[-1]
    assert not decision.signals
    assert module.phase is BreakoutReturnPhase.WAITING_RETURN


def test_close_return_mode_accepts_open_outside(make_bar, make_view):
    module = BreakoutReturnModule(
        lookback_bars=3,
        return_confirmation="close",
    )
    bars = rolling_seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 101, 102, 98.5, 99),
    ]
    assert feed(module, bars, make_view)[-1].signals


def test_direction_filter_blocks_above(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3, direction="below")
    bars = rolling_seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)]
    assert feed(module, bars, make_view)[-1].setups == ()


def test_breakout_buffer_is_strict(make_bar, make_view):
    module = BreakoutReturnModule(
        lookback_bars=3,
        breakout_confirmation="close",
        breakout_buffer_bps=100,
    )
    bars = rolling_seed(make_bar) + [make_bar(3, 100, 102, 99, 100.9)]
    assert feed(module, bars, make_view)[-1].setups == ()
