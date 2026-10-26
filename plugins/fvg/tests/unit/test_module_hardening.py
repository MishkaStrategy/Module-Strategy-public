from __future__ import annotations

import pytest
from modular_core import Candle, MarketView

from conftest import bullish_triplet, candle, monotonic_bars, view_for
from modular_strategy_fvg import (
    ACTIVE_ZONE_KEY,
    CREATED_EVENT_KEY,
    INVALIDATED_EVENT_KEY,
    ZONE_STATE_KEY,
    FvgDataConflictError,
    FvgDataOrderError,
    FvgModule,
    FvgSymbolScopeError,
)


def test_empty_base_bars_is_safe_skip() -> None:
    view = MarketView((), {}, 0, 1, 1, None, ())
    decision = FvgModule(timeframes=("15m",)).on_bar(view)
    assert decision.skips == ("fvg.empty_base_bars",)


def test_missing_timeframe_is_typed_skip() -> None:
    decision = FvgModule(timeframes=("15m",)).on_bar(view_for([], timeframe="1h"))
    assert decision.skips == ("fvg.no_data:15m",)


def test_zone_created_only_after_c_is_visible() -> None:
    bars = bullish_triplet()
    module = FvgModule(timeframes=("15m",))
    assert not module.on_bar(view_for(bars[:2])).features
    decision = module.on_bar(view_for(bars))
    assert [feature.key for feature in decision.features] == [
        ACTIVE_ZONE_KEY,
        ZONE_STATE_KEY,
        CREATED_EVENT_KEY,
    ]


def test_exact_duplicate_is_idempotent() -> None:
    bars = bullish_triplet()
    module = FvgModule(timeframes=("15m",))
    first = module.on_bar(view_for(bars))
    second = module.on_bar(view_for(bars))
    assert first.features
    assert second.features == ()
    assert second.feature_deletes == ()
    assert second.diagnostics["duplicates"] == 1


def test_conflicting_duplicate_fails_closed_and_preserves_state() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))
    before = module.snapshot_state()
    c = bars[-1]
    bars[-1] = Candle(
        c.timestamp,
        c.open,
        c.high + 1,
        c.low,
        c.close,
        c.volume,
        c.symbol,
        c.timeframe,
    )
    with pytest.raises(FvgDataConflictError):
        module.on_bar(view_for(bars))
    assert module.snapshot_state() == before


def test_future_htf_bar_rejected_without_mutation() -> None:
    bars = bullish_triplet()
    base = candle(0, timeframe="5m")
    view = MarketView((base,), {"15m": bars}, 0, 1, 1, None, ())
    module = FvgModule(timeframes=("15m",))
    with pytest.raises(ValueError, match="future/incomplete"):
        module.on_bar(view)
    assert module.bound_symbol is None
    assert module.active_zones == ()


def test_wrong_series_symbol_rejected() -> None:
    bars = bullish_triplet(symbol="ETHUSDT")
    with pytest.raises(ValueError, match="symbol"):
        FvgModule(timeframes=("15m",)).on_bar(view_for(bars, symbol="BTCUSDT"))


def test_wrong_series_timeframe_rejected() -> None:
    bars = bullish_triplet(timeframe="4h")
    with pytest.raises(ValueError, match="timeframe"):
        FvgModule(timeframes=("15m",)).on_bar(view_for(bars, timeframe="15m"))


def test_instance_binds_to_one_symbol() -> None:
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bullish_triplet()))
    with pytest.raises(FvgSymbolScopeError):
        module.on_bar(view_for(bullish_triplet(symbol="ETHUSDT"), symbol="ETHUSDT"))


@pytest.mark.parametrize(
    ("level", "basis", "price", "invalidated"),
    [
        ("first_touch", "wick", 104.9, True),
        ("midpoint", "wick", 102.5, True),
        ("full_fill", "wick", 100.0, True),
        ("full_fill", "wick", 100.1, False),
        ("first_touch", "close", 104.9, True),
        ("midpoint", "close", 102.6, False),
    ],
)
def test_bullish_invalidation_modes(level: str, basis: str, price: float, invalidated: bool) -> None:
    bars = list(bullish_triplet())
    module = FvgModule(
        timeframes=("15m",),
        invalidation_level=level,
        invalidation_basis=basis,
    )
    module.on_bar(view_for(bars))
    next_bar = candle(3, open_=106, high=108, low=min(price, 105), close=price)
    decision = module.on_bar(view_for([*bars, next_bar]))
    assert bool(decision.feature_deletes) is invalidated
    if invalidated:
        assert decision.feature_deletes[0].reason == "fvg_mitigated"
        assert any(feature.key == INVALIDATED_EVENT_KEY for feature in decision.features)


def test_source_c_cannot_invalidate_its_own_zone() -> None:
    decision = FvgModule(timeframes=("15m",), invalidation_level="first_touch").on_bar(view_for(bullish_triplet()))
    assert not decision.feature_deletes
    assert len(decision.features) == 3


def test_gap_reset_emits_exact_delete_and_barrier() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))
    gap = module.on_bar(view_for(bars, data_gap=True))
    assert gap.skips == ("fvg.data_gap_reset",)
    assert len(gap.feature_deletes) == 1
    assert gap.feature_deletes[0].reason == "fvg_data_gap_reset"
    assert module.active_zones == ()
    assert not module.on_bar(view_for(bars)).features


def test_three_new_bars_required_after_gap() -> None:
    bars = monotonic_bars(6)
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars[:3]))
    module.on_bar(view_for(bars[:3], data_gap=True))
    assert not module.on_bar(view_for(bars[:4])).features
    assert not module.on_bar(view_for(bars[:5])).features
    assert any(feature.key == ACTIVE_ZONE_KEY for feature in module.on_bar(view_for(bars[:6])).features)


def test_order_regression_raises_by_default() -> None:
    bars = monotonic_bars(5)
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bars))
    with pytest.raises(FvgDataOrderError):
        module.on_bar(view_for(bars[:3]))


def test_order_regression_reset_policy_clears_zones() -> None:
    bars = monotonic_bars(5)
    module = FvgModule(timeframes=("15m",), order_violation_policy="reset_and_skip")
    module.on_bar(view_for(bars))
    decision = module.on_bar(view_for(bars[:3]))
    assert decision.skips == ("fvg.order_violation_reset:15m",)
    assert decision.feature_deletes
    assert module.active_zones == ()


def test_lifecycle_events_can_be_disabled() -> None:
    decision = FvgModule(timeframes=("15m",), publish_lifecycle_events=False).on_bar(view_for(bullish_triplet()))
    assert [feature.key for feature in decision.features] == [
        ACTIVE_ZONE_KEY,
        ZONE_STATE_KEY,
    ]


def test_capacity_is_bounded_and_oldest_zone_is_evicted() -> None:
    module = FvgModule(timeframes=("15m",), max_active_zones_per_timeframe=2)
    decision = module.on_bar(view_for(monotonic_bars(6)))
    assert len(module.active_zones) == 2
    assert decision.diagnostics["capacity_evicted"] == 2
    assert all(delete.reason == "fvg_capacity_evicted" for delete in decision.feature_deletes)


def test_multiple_instances_are_isolated() -> None:
    first = FvgModule(timeframes=("15m",))
    second = FvgModule(timeframes=("15m",))
    first.on_bar(view_for(bullish_triplet()))
    assert first.active_zones
    assert second.active_zones == ()


def test_future_extension_does_not_change_current_decision() -> None:
    bars = list(bullish_triplet())
    current = FvgModule(timeframes=("15m",)).on_bar(view_for(bars))
    extended = FvgModule(timeframes=("15m",)).on_bar(
        view_for(
            [
                *bars,
                candle(3, open_=107, high=111, low=106, close=108),
            ]
        )
    )
    current_ids = [feature.feature_id for feature in current.features if feature.key == ACTIVE_ZONE_KEY]
    extended_ids = [
        feature.feature_id
        for feature in extended.features
        if feature.created_at <= bars[-1].close_time and feature.key == ACTIVE_ZONE_KEY
    ]
    assert current_ids == extended_ids
