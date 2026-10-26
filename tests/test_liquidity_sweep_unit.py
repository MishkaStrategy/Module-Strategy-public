from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from typing import Iterable

import pytest

from modular_core import Candle, MarketView, Side, SetupStatus
from strategy_modules.liquidity_sweep import LiquiditySweepConfig, LiquiditySweepModule

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def candle(
    i: int,
    o: float,
    h: float,
    low: float,
    c: float,
    *,
    timeframe: str = "5m",
    symbol: str = "BTCUSDT",
) -> Candle:
    step = 5 if timeframe == "5m" else 15
    return Candle(START + timedelta(minutes=i * step), o, h, low, c, 1.0, symbol, timeframe)


def view(
    history: list[Candle],
    *,
    gap: bool = False,
    tf_history: dict[str, list[Candle]] | None = None,
    features: object | None = None,
) -> MarketView:
    return MarketView(
        base_bars=tuple(history),
        timeframe_bars={key: tuple(value) for key, value in (tf_history or {}).items()},
        index=len(history) - 1,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        data_gap=gap,
        features=features,
    )


def feed(module: LiquiditySweepModule, bars: Iterable[Candle], *, gap_at: set[int] | None = None):
    history: list[Candle] = []
    decisions = []
    for index, item in enumerate(bars):
        history.append(item)
        decisions.append(module.on_bar(view(history, gap=index in (gap_at or set()))))
    return decisions


def high_level_bars() -> list[Candle]:
    return [
        candle(0, 9.0, 10.0, 8.0, 9.0),
        candle(1, 10.0, 12.0, 9.0, 11.0),
        candle(2, 10.5, 11.0, 9.5, 10.0),
    ]


def low_level_bars() -> list[Candle]:
    return [
        candle(0, 10.0, 11.0, 9.0, 10.0),
        candle(1, 9.0, 10.0, 7.0, 8.0),
        candle(2, 8.5, 10.0, 8.0, 9.0),
    ]


def make_module(**kwargs) -> LiquiditySweepModule:
    parameters = {
        "pivot_left_bars": 1,
        "pivot_right_bars": 1,
        "min_wick_to_body_ratio": 0.5,
        "min_wick_fraction": 0.2,
    }
    parameters.update(kwargs)
    return LiquiditySweepModule(**parameters)


def created_level(decision):
    return next(feature for feature in decision.features if feature.key == "liquidity_sweep.level")


def sweep_event(decision):
    return next(feature for feature in decision.features if feature.key == "liquidity_sweep.event")


@pytest.mark.parametrize(
    "field,value", [("pivot_left_bars", 0), ("pivot_right_bars", 0), ("max_active_levels", 0)]
)
def test_positive_integer_config_validation(field, value):
    with pytest.raises(ValueError):
        LiquiditySweepConfig(**{field: value})


def test_integer_config_rejects_bool():
    with pytest.raises(TypeError):
        LiquiditySweepConfig(pivot_left_bars=True)


def test_blank_timeframe_rejected():
    with pytest.raises(ValueError):
        LiquiditySweepConfig(timeframe="  ")


def test_age_range_validation():
    with pytest.raises(ValueError):
        LiquiditySweepConfig(min_level_age_bars=3, max_level_age_bars=2)


def test_penetration_range_validation():
    with pytest.raises(ValueError):
        LiquiditySweepConfig(min_penetration_bps=2, max_penetration_bps=1)


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_wick_fraction_validation(value):
    with pytest.raises(ValueError):
        LiquiditySweepConfig(min_wick_fraction=value)


def test_non_finite_config_rejected():
    with pytest.raises(ValueError):
        LiquiditySweepConfig(max_penetration_bps=float("inf"))


def test_manifest_contract_and_no_core_dependencies():
    module = make_module()
    assert module.manifest.name == "liquidity_sweep"
    assert module.manifest.api_version == "1.0"
    assert module.manifest.required_features == ()
    assert set(module.manifest.produced_features) == {
        "liquidity_sweep.level",
        "liquidity_sweep.event",
    }
    assert module.manifest.skip_until_ready is False


def test_explicit_timeframe_declares_requirement_and_warmup():
    module = make_module(timeframe="15m")
    assert module.manifest.required_timeframes == ("15m",)
    assert module.manifest.warmup_bars == {"15m": 3}


def test_no_level_before_full_pivot_window():
    decisions = feed(make_module(), high_level_bars()[:2])
    assert all(not decision.features for decision in decisions)
    assert decisions[-1].diagnostics["state"] == "warming_up"


def test_confirms_high_only_after_right_bar_closes():
    decisions = feed(make_module(), high_level_bars())
    level = created_level(decisions[-1])
    assert level.value["liquidity_side"] == "high"
    assert level.value["price"] == 12.0
    assert level.created_at == high_level_bars()[-1].close_time


def test_confirms_low_only_after_right_bar_closes():
    decisions = feed(make_module(), low_level_bars())
    level = created_level(decisions[-1])
    assert level.value["liquidity_side"] == "low"
    assert level.value["price"] == 7.0


def test_equal_highs_are_not_strict_pivot():
    bars = [candle(0, 9, 12, 8, 10), candle(1, 10, 12, 9, 11), candle(2, 10, 11, 9, 10)]
    decisions = feed(make_module(), bars)
    assert not any(feature.key == "liquidity_sweep.level" for feature in decisions[-1].features)


def test_high_sweep_emits_bearish_feature_setup_and_optional_signal():
    module = make_module(emit_signals=True)
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    event = sweep_event(decision)
    assert event.value["direction"] == "bearish"
    assert event.value["liquidity_side"] == "high"
    assert [item.status for item in decision.setups] == [SetupStatus.CREATED, SetupStatus.CONFIRMED]
    assert decision.setups[-1].side is Side.SELL
    assert decision.signals[0].side is Side.SELL
    assert decision.orders == ()
    assert decision.closes == ()


def test_low_sweep_emits_bullish_feature_and_setup():
    module = make_module()
    bars = low_level_bars() + [candle(3, 8.2, 9.2, 6.95, 8.8)]
    decision = feed(module, bars)[-1]
    event = sweep_event(decision)
    assert event.value["direction"] == "bullish"
    assert decision.setups[-1].side is Side.BUY
    assert decision.signals == ()


def test_minimum_penetration_filter():
    module = make_module(min_penetration_bps=100.0, max_penetration_bps=200.0)
    bars = high_level_bars() + [candle(3, 11.8, 12.05, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_maximum_penetration_filter():
    module = make_module(max_penetration_bps=100.0)
    bars = high_level_bars() + [candle(3, 11.8, 13.5, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_close_reclaim_buffer_filter():
    module = make_module(close_reclaim_bps=100.0)
    bars = high_level_bars() + [candle(3, 11.8, 12.5, 10.8, 11.95)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_wick_to_body_ratio_filter():
    module = make_module(min_wick_to_body_ratio=10.0)
    bars = high_level_bars() + [candle(3, 10.0, 12.5, 9.5, 11.8)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_wick_fraction_filter():
    module = make_module(min_wick_fraction=0.9)
    bars = high_level_bars() + [candle(3, 11.7, 12.5, 8.0, 11.1)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_directional_close_filter():
    module = make_module(require_directional_close=True)
    bars = high_level_bars() + [candle(3, 10.8, 12.5, 10.5, 11.2)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_accepted_breakout_invalidates_level():
    module = make_module(invalidation_close_bps=0)
    bars = high_level_bars() + [candle(3, 11.8, 12.8, 11.5, 12.4)]
    decision = feed(module, bars)[-1]
    assert any(item.reason == "accepted_breakout" for item in decision.feature_deletes)
    assert decision.diagnostics["active_levels"] == 0


def test_level_expiry_deletes_feature():
    module = make_module(max_level_age_bars=1, min_level_age_bars=0)
    bars = high_level_bars() + [candle(3, 10.5, 11.0, 9.5, 10.0), candle(4, 10.5, 11.0, 9.5, 10.0)]
    decision = feed(module, bars)[-1]
    assert any(item.reason == "level_expired" for item in decision.feature_deletes)


def test_min_level_age_delays_sweep():
    module = make_module(min_level_age_bars=2)
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    assert not any(feature.key == "liquidity_sweep.event" for feature in decision.features)


def test_duplicate_event_is_idempotent():
    module = make_module()
    bars = high_level_bars()
    feed(module, bars)
    before = deepcopy(dict(module.snapshot_state()))
    duplicate = module.on_bar(view(bars))
    after = dict(module.snapshot_state())
    assert duplicate.skips == ("liquidity_sweep.duplicate_bar",)
    assert after == before


def test_conflicting_duplicate_is_rejected_without_mutation():
    module = make_module()
    bars = high_level_bars()
    feed(module, bars)
    before = deepcopy(dict(module.snapshot_state()))
    conflicting = bars[:-1] + [candle(2, 10.5, 11.2, 9.5, 10.0)]
    decision = module.on_bar(view(conflicting))
    assert decision.skips == ("liquidity_sweep.conflicting_duplicate_bar",)
    assert dict(module.snapshot_state()) == before


def test_out_of_order_event_is_rejected_without_mutation():
    module = make_module()
    bars = high_level_bars()
    feed(module, bars)
    before = deepcopy(dict(module.snapshot_state()))
    decision = module.on_bar(view([bars[0]]))
    assert decision.skips == ("liquidity_sweep.out_of_order_bar",)
    assert dict(module.snapshot_state()) == before


def test_gap_resets_levels_and_buffer():
    module = make_module()
    bars = high_level_bars()
    feed(module, bars)
    next_bar = candle(3, 10, 11, 9, 10)
    decision = module.on_bar(view(bars + [next_bar], gap=True))
    assert any(item.reason == "data_gap" for item in decision.feature_deletes)
    assert decision.skips == ("liquidity_sweep.data_gap_reset",)
    assert decision.diagnostics["buffered_bars"] == 1
    assert decision.diagnostics["active_levels"] == 0


def test_snapshot_is_json_serializable():
    module = make_module()
    feed(module, high_level_bars())
    json.dumps(module.snapshot_state())


def test_state_roundtrip_preserves_continuation():
    original = make_module(emit_signals=True)
    bars = high_level_bars()
    feed(original, bars)
    restored = make_module(emit_signals=True)
    restored.restore_state(original.snapshot_state())
    sweep = candle(3, 11.6, 12.1, 10.8, 11.2)
    left = original.on_bar(view(bars + [sweep]))
    right = restored.on_bar(view(bars + [sweep]))
    left_events = tuple(
        feature for feature in left.features if feature.key == "liquidity_sweep.event"
    )
    right_events = tuple(
        feature for feature in right.features if feature.key == "liquidity_sweep.event"
    )
    assert left_events == right_events
    assert [feature.key for feature in right.features[:2]] == [
        "liquidity_sweep.level",
        "liquidity_sweep.event",
    ]
    assert right.diagnostics["levels_republished"] == 1
    assert left.feature_deletes == right.feature_deletes
    assert left.setups == right.setups
    assert left.signals == right.signals
    assert original.snapshot_state() == restored.snapshot_state()


def test_restore_rejects_wrong_schema_atomically():
    module = make_module()
    feed(module, high_level_bars())
    before = deepcopy(dict(module.snapshot_state()))
    broken = deepcopy(before)
    broken["schema_version"] = "999"
    with pytest.raises(ValueError):
        module.restore_state(broken)
    assert dict(module.snapshot_state()) == before


def test_restore_rejects_future_level_sequence():
    module = make_module()
    feed(module, high_level_bars())
    state = deepcopy(dict(module.snapshot_state()))
    state["levels"][0]["confirmed_sequence"] = state["sequence"] + 1
    with pytest.raises(ValueError):
        make_module().restore_state(state)


def test_parallel_instances_are_isolated():
    first = make_module()
    second = make_module()
    feed(first, high_level_bars())
    assert first.snapshot_state()["levels"]
    assert second.snapshot_state()["levels"] == []


def test_deterministic_ids_for_identical_input():
    first = feed(make_module(), high_level_bars())[-1]
    second = feed(make_module(), high_level_bars())[-1]
    assert created_level(first).feature_id == created_level(second).feature_id


def test_explicit_timeframe_uses_only_completed_timeframe_series():
    module = make_module(timeframe="15m")
    base = [candle(0, 9, 10, 8, 9)]
    tf_bars = [
        candle(0, 9, 10, 8, 9, timeframe="15m"),
        candle(1, 10, 12, 9, 11, timeframe="15m"),
        candle(2, 10.5, 11, 9.5, 10, timeframe="15m"),
    ]
    decisions = []
    for index in range(3):
        decisions.append(module.on_bar(view(base, tf_history={"15m": tf_bars[: index + 1]})))
    assert created_level(decisions[-1]).timeframe == "15m"


def test_missing_explicit_timeframe_fails_safe():
    module = make_module(timeframe="15m")
    decision = module.on_bar(view([candle(0, 9, 10, 8, 9)]))
    assert decision.skips == ("liquidity_sweep.timeframe_unavailable",)


def test_capacity_limit_evicts_oldest_level_deterministically():
    module = make_module(max_active_levels=1, max_level_age_bars=1000)
    bars = [
        candle(0, 9, 10, 8, 9),
        candle(1, 10, 12, 9, 11),
        candle(2, 10, 11, 9, 10),
        candle(3, 10, 13, 9.5, 12),
        candle(4, 11, 12, 10, 11),
    ]
    decisions = feed(module, bars)
    assert any(item.reason == "capacity_eviction" for item in decisions[-1].feature_deletes)
    assert len(module.snapshot_state()["levels"]) == 1


def test_event_feature_is_same_bar_only():
    module = make_module()
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    event = sweep_event(feed(module, bars)[-1])
    assert event.valid_until == event.created_at


def test_swept_level_is_deleted_once():
    module = make_module()
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    swept_deletes = [item for item in decision.feature_deletes if item.reason == "level_swept"]
    assert len(swept_deletes) == 1
    next_decision = module.on_bar(view(bars + [candle(4, 11, 12.4, 10, 11)]))
    assert not any(item.reason == "level_swept" for item in next_decision.feature_deletes)


def test_invalid_explicit_timeframe_rejected():
    with pytest.raises(ValueError):
        LiquiditySweepConfig(timeframe="15minutes")


@pytest.mark.parametrize("field", ["close_reclaim_bps", "invalidation_close_bps"])
def test_percentage_threshold_cannot_exceed_one_hundred_percent(field):
    with pytest.raises(ValueError):
        LiquiditySweepConfig(**{field: 10_001})


@pytest.mark.parametrize(
    "module_kwargs,sweep_bar,reason",
    [
        (
            {"min_penetration_bps": 100.0},
            candle(3, 11.6, 12.05, 10.8, 11.2),
            "penetration_below_min",
        ),
        ({"max_penetration_bps": 50.0}, candle(3, 11.6, 12.1, 10.8, 11.2), "penetration_above_max"),
        ({"close_reclaim_bps": 100.0}, candle(3, 11.6, 12.1, 10.8, 11.95), "close_not_reclaimed"),
        (
            {"require_directional_close": True},
            candle(3, 10.9, 12.1, 10.8, 11.2),
            "directional_close_required",
        ),
        (
            {"min_wick_to_body_ratio": 10.0},
            candle(3, 10.0, 12.1, 9.5, 11.8),
            "wick_ratio_below_min",
        ),
        ({"min_wick_fraction": 0.9}, candle(3, 11.6, 12.1, 8.0, 11.2), "wick_fraction_below_min"),
    ],
)
def test_filter_rejection_is_explainable(module_kwargs, sweep_bar, reason):
    decision = feed(make_module(**module_kwargs), high_level_bars() + [sweep_bar])[-1]
    assert decision.diagnostics["rejection_counts"].get(reason) == 1


def test_explicit_timeframe_gap_resets_on_repeated_htf_bar():
    module = make_module(timeframe="15m")
    tf_bars = [
        candle(0, 9, 10, 8, 9, timeframe="15m"),
        candle(1, 10, 12, 9, 11, timeframe="15m"),
        candle(2, 10.5, 11, 9.5, 10, timeframe="15m"),
    ]
    base = [candle(0, 9, 10, 8, 9)]
    for index in range(3):
        module.on_bar(view(base, tf_history={"15m": tf_bars[: index + 1]}))
    assert module.snapshot_state()["levels"]
    decision = module.on_bar(view(base, gap=True, tf_history={"15m": tf_bars}))
    assert decision.skips == ("liquidity_sweep.data_gap_reset",)
    assert decision.feature_deletes[0].reason == "data_gap"
    assert module.snapshot_state()["levels"] == []
    assert module.snapshot_state()["bars"] == []


def test_restore_rejects_inconsistent_ohlc_atomically():
    module = make_module()
    feed(module, high_level_bars())
    before = deepcopy(dict(module.snapshot_state()))
    broken = deepcopy(before)
    broken["bars"][-1]["high"] = "1"
    broken["last_bar"]["high"] = "1"
    with pytest.raises(ValueError):
        module.restore_state(broken)
    assert dict(module.snapshot_state()) == before


def test_restore_rejects_last_bar_mismatch():
    module = make_module()
    feed(module, high_level_bars())
    broken = deepcopy(dict(module.snapshot_state()))
    broken["last_bar"]["close"] = "9.9"
    with pytest.raises(ValueError, match="last_bar"):
        make_module().restore_state(broken)
