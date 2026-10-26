from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Iterable

import pytest

from modular_core import Candle, MarketView, SetupStatus
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


def test_pivot_with_two_right_bars_is_not_published_early():
    module = LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=2)
    bars = [
        candle(0, 9, 10, 8, 9),
        candle(1, 10, 12, 9, 11),
        candle(2, 10, 11, 9, 10),
        candle(3, 10, 10.5, 9, 10),
    ]
    decisions = feed(module, bars)
    assert not any(feature.key == "liquidity_sweep.level" for feature in decisions[2].features)
    assert created_level(decisions[3]).value["price"] == 12.0


def test_newly_confirmed_level_cannot_be_swept_on_confirmation_bar():
    module = make_module(min_level_age_bars=0)
    decisions = feed(module, high_level_bars())
    assert any(feature.key == "liquidity_sweep.level" for feature in decisions[-1].features)
    assert not any(feature.key == "liquidity_sweep.event" for feature in decisions[-1].features)


def test_max_penetration_boundary_is_inclusive():
    module = make_module(max_penetration_bps=100.0)
    bars = high_level_bars() + [candle(3, 11.6, 12.12, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    assert sweep_event(decision).value["penetration_bps"] == pytest.approx(100.0)


def test_close_reclaim_boundary_is_inclusive():
    module = make_module(close_reclaim_bps=100.0)
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 11.1, 11.88)]
    decision = feed(module, bars)[-1]
    assert sweep_event(decision).value["direction"] == "bearish"


def test_stream_change_resets_old_symbol_levels_before_processing():
    module = make_module()
    bars = high_level_bars()
    feed(module, bars)
    assert module.snapshot_state()["levels"]
    other = candle(3, 100, 101, 99, 100, symbol="ETHUSDT")
    decision = module.on_bar(view(bars + [other]))
    assert any(item.reason == "stream_changed" for item in decision.feature_deletes)
    assert decision.skips == ("liquidity_sweep.stream_changed_reset",)
    assert decision.diagnostics["active_levels"] == 0


def test_min_penetration_boundary_is_inclusive():
    module = make_module(min_penetration_bps=100.0, max_penetration_bps=100.0)
    bars = high_level_bars() + [candle(3, 11.6, 12.12, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    assert sweep_event(decision).value["penetration_bps"] == pytest.approx(100.0)


def test_sweep_setup_has_created_then_confirmed_lifecycle():
    module = make_module()
    decision = feed(module, high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)])[-1]
    assert [event.status for event in decision.setups] == [
        SetupStatus.CREATED,
        SetupStatus.CONFIRMED,
    ]
    assert decision.setups[0].setup_id == decision.setups[1].setup_id
    assert (
        sweep_event(decision).value["decision_time"] == decision.features[0].created_at.isoformat()
    )


def test_repeated_explicit_timeframe_bar_is_expected_wait_not_error_skip():
    module = make_module(timeframe="15m")
    tf_bar = candle(0, 9, 10, 8, 9, timeframe="15m")
    base = [candle(0, 9, 10, 8, 9)]
    first = module.on_bar(view(base, tf_history={"15m": [tf_bar]}))
    second = module.on_bar(view(base + [candle(1, 9, 10, 8, 9)], tf_history={"15m": [tf_bar]}))
    assert first.skips == ()
    assert second.skips == ()
    assert second.diagnostics["state"] == "waiting_for_new_timeframe_bar"
    assert module.snapshot_state()["sequence"] == 1


def test_snapshot_is_safe_during_parallel_reads():
    from concurrent.futures import ThreadPoolExecutor

    module = make_module()
    feed(module, high_level_bars())
    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = list(executor.map(lambda _: module.snapshot_state(), range(100)))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_empty_market_view_fails_safe_without_state_mutation():
    module = make_module()
    empty = MarketView((), {}, 0, 10_000, 10_000, None, ())
    decision = module.on_bar(empty)
    assert decision.skips == ("liquidity_sweep.empty_market_view",)
    assert module.snapshot_state()["sequence"] == 0


def test_explicit_timeframe_mismatch_fails_safe():
    module = make_module(timeframe="15m")
    wrong = candle(0, 9, 10, 8, 9, timeframe="5m")
    decision = module.on_bar(view([wrong], tf_history={"15m": [wrong]}))
    assert decision.skips == ("liquidity_sweep.timeframe_mismatch",)
    assert decision.diagnostics["expected_timeframe"] == "15m"
    assert module.snapshot_state()["sequence"] == 0


def test_restore_rejects_orphan_levels_without_last_bar():
    module = make_module()
    feed(module, high_level_bars())
    broken = deepcopy(dict(module.snapshot_state()))
    broken["bars"] = []
    broken["last_bar"] = None
    with pytest.raises(ValueError, match="last_bar"):
        make_module().restore_state(broken)


def test_empty_state_restore_resets_module_cleanly():
    module = make_module()
    feed(module, high_level_bars())
    module.restore_state({})
    assert module.snapshot_state() == {
        "schema_version": "1",
        "config_fingerprint": module.snapshot_state()["config_fingerprint"],
        "sequence": 0,
        "stats": {
            "processed_bars": 0,
            "levels_created": 0,
            "levels_swept": 0,
            "levels_invalidated": 0,
            "levels_expired": 0,
            "levels_evicted": 0,
            "rejected_candidates": 0,
            "gap_resets": 0,
            "stream_resets": 0,
        },
        "bars": [],
        "levels": [],
        "last_bar": None,
    }


def test_restore_rejects_state_from_different_configuration():
    source = make_module(max_active_levels=8)
    feed(source, high_level_bars())
    target = make_module(max_active_levels=9)
    with pytest.raises(ValueError, match="configuration"):
        target.restore_state(source.snapshot_state())


def test_restore_rejects_partial_state_payload():
    module = make_module()
    state = {"schema_version": "1", "sequence": 0}
    with pytest.raises(ValueError, match="missing"):
        module.restore_state(state)


def test_restore_rejects_unknown_state_keys():
    module = make_module()
    state = dict(module.snapshot_state())
    state["unexpected"] = True
    with pytest.raises(ValueError, match="unknown"):
        module.restore_state(state)


def test_restore_rejects_last_bar_with_zero_sequence():
    module = make_module()
    feed(module, [candle(0, 9, 10, 8, 9)])
    state = deepcopy(dict(module.snapshot_state()))
    state["sequence"] = 0
    with pytest.raises(ValueError, match="processed_bars|positive"):
        make_module().restore_state(state)


def test_public_feature_key_constants_match_manifest():
    from strategy_modules.liquidity_sweep import LEVEL_FEATURE_KEY, SWEEP_FEATURE_KEY

    module = make_module()
    assert module.manifest.produced_features == (LEVEL_FEATURE_KEY, SWEEP_FEATURE_KEY)


def test_from_config_preserves_resolved_parameters():
    config = LiquiditySweepConfig(
        timeframe="15m",
        pivot_left_bars=3,
        pivot_right_bars=4,
        max_active_levels=32,
    )
    module = LiquiditySweepModule.from_config(config)
    assert dict(module.parameters) == dict(config.as_parameters())
    assert module.config is not config
    assert module.config == config


def test_from_config_rejects_wrong_type():
    with pytest.raises(TypeError):
        LiquiditySweepModule.from_config({})  # type: ignore[arg-type]


def test_feature_values_match_public_typed_contract_fields():
    level_decision = feed(make_module(), high_level_bars())[-1]
    level = created_level(level_decision).value
    assert set(level) == {"level_id", "liquidity_side", "price", "pivot_time", "confirmed_at"}

    sweep_decision = feed(make_module(), high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)])[
        -1
    ]
    event = sweep_event(sweep_decision).value
    assert set(event) == {
        "event_id",
        "decision_time",
        "level_id",
        "liquidity_side",
        "level_price",
        "close_price",
        "pivot_time",
        "confirmed_at",
        "bars_since_confirmation",
        "direction",
        "penetration_bps",
        "wick_to_body_ratio",
        "wick_fraction",
        "extreme_price",
    }


def test_downstream_consumer_can_depend_only_on_public_feature_key():
    from modular_core import ModuleDecision, ModuleManifest, ModuleRegistry, StrategyModule
    from strategy_modules.liquidity_sweep import SWEEP_FEATURE_KEY

    class Consumer(StrategyModule):
        name = "sweep_consumer"
        manifest = ModuleManifest(
            name=name,
            required_features=(SWEEP_FEATURE_KEY,),
        )

        def on_bar(self, view):
            return ModuleDecision()

    producer = make_module()
    consumer = Consumer()
    plan = ModuleRegistry().validate_and_plan((producer, consumer), base_timeframe="5m")
    assert plan.modules == (producer, consumer)


def test_cumulative_observability_counters_are_persisted():
    module = make_module()
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    totals = decision.diagnostics["totals"]
    assert totals["processed_bars"] == 4
    assert totals["levels_created"] >= 1
    assert totals["levels_swept"] == 1
    assert module.snapshot_state()["stats"] == totals


def test_observability_counters_survive_restore():
    source = make_module()
    feed(source, high_level_bars())
    restored = make_module()
    restored.restore_state(source.snapshot_state())
    assert restored.snapshot_state()["stats"] == source.snapshot_state()["stats"]


def test_duplicate_does_not_mutate_observability_state():
    module = make_module()
    bars = high_level_bars()
    feed(module, bars)
    before = deepcopy(dict(module.snapshot_state()))
    module.on_bar(view(bars))
    assert dict(module.snapshot_state()) == before


def test_zero_body_sweep_is_finite_and_does_not_crash():
    module = make_module(min_wick_to_body_ratio=0.0)
    bars = high_level_bars() + [candle(3, 11.2, 12.1, 10.8, 11.2)]
    decision = feed(module, bars)[-1]
    event = sweep_event(decision)
    assert event.value["wick_to_body_ratio"] > 0
    assert event.value["wick_to_body_ratio"] != float("inf")


def test_long_randomized_stream_remains_bounded_and_json_serializable():
    import random

    randomizer = random.Random(42)
    module = LiquiditySweepModule(max_active_levels=16)
    bars = []
    price = 100.0
    for index in range(2_000):
        price += randomizer.uniform(-0.5, 0.5)
        o = max(price + randomizer.uniform(-0.2, 0.2), 1.0)
        c = max(price + randomizer.uniform(-0.2, 0.2), 1.0)
        h = max(o, c) + randomizer.uniform(0.01, 0.8)
        low = max(min(o, c) - randomizer.uniform(0.01, 0.8), 0.01)
        bars.append(candle(index, o, h, low, c))
    feed(module, bars)
    state = module.snapshot_state()
    assert len(state["levels"]) <= 16
    assert len(state["bars"]) <= module.config.pivot_window_bars
    json.dumps(state)


def test_complete_state_is_deterministic_for_identical_streams():
    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    first = make_module()
    second = make_module()
    feed(first, bars)
    feed(second, bars)
    assert first.snapshot_state() == second.snapshot_state()


# Regression coverage added during optimization round 10 / audit 5.
def test_config_rejects_non_string_timeframe_with_informative_error():
    with pytest.raises(TypeError, match="timeframe"):
        LiquiditySweepConfig(timeframe=15)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["require_directional_close", "emit_signals"])
def test_config_rejects_non_boolean_flags(field):
    with pytest.raises(TypeError, match=field):
        LiquiditySweepConfig(**{field: "false"})


@pytest.mark.parametrize("field", ["min_penetration_bps", "min_wick_fraction"])
def test_numeric_config_rejects_boolean_values(field):
    with pytest.raises(TypeError, match=field):
        LiquiditySweepConfig(**{field: True})


def test_manifest_parameter_defaults_match_validated_config_defaults():
    module = LiquiditySweepModule()
    defaults = LiquiditySweepConfig().as_parameters()
    schema = module.manifest.parameters_schema
    assert set(schema) == set(defaults)
    assert {name: rules["default"] for name, rules in schema.items()} == dict(defaults)


def test_restore_rejects_unknown_nested_bar_state_key():
    source = make_module()
    feed(source, high_level_bars())
    broken = deepcopy(dict(source.snapshot_state()))
    broken["bars"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="invalid bar state keys"):
        make_module().restore_state(broken)


def test_restore_rejects_missing_nested_level_state_key():
    source = make_module()
    feed(source, high_level_bars())
    broken = deepcopy(dict(source.snapshot_state()))
    broken["levels"][0].pop("price")
    with pytest.raises(ValueError, match="invalid level state keys"):
        make_module().restore_state(broken)


def test_restore_rejects_oversized_bar_buffer_instead_of_silent_truncation():
    source = make_module()
    feed(source, high_level_bars())
    broken = deepcopy(dict(source.snapshot_state()))
    extra = deepcopy(broken["bars"][-1])
    extra["timestamp"] = (START + timedelta(minutes=15)).isoformat()
    extra["close_time"] = (START + timedelta(minutes=20)).isoformat()
    broken["bars"].append(extra)
    with pytest.raises(ValueError, match="more buffered bars"):
        make_module().restore_state(broken)


def test_restore_rejects_levels_above_configured_capacity():
    source = make_module(max_active_levels=1)
    feed(source, high_level_bars())
    broken = deepcopy(dict(source.snapshot_state()))
    duplicate = deepcopy(broken["levels"][0])
    duplicate["level_id"] += "-copy"
    duplicate["feature_id"] += "-copy"
    broken["levels"].append(duplicate)
    with pytest.raises(ValueError, match="more active levels"):
        make_module(max_active_levels=1).restore_state(broken)


def test_restore_rejects_processed_bar_counter_sequence_mismatch():
    source = make_module()
    feed(source, high_level_bars())
    broken = deepcopy(dict(source.snapshot_state()))
    broken["stats"]["processed_bars"] -= 1
    with pytest.raises(ValueError, match="must equal"):
        make_module().restore_state(broken)


def test_restart_republishes_active_levels_once_into_fresh_feature_store():
    source = make_module()
    bars = high_level_bars()
    feed(source, bars)
    restored = make_module()
    restored.restore_state(source.snapshot_state())

    next_bar = candle(3, 10.5, 11.5, 9.5, 10.5)
    first = restored.on_bar(view(bars + [next_bar]))
    assert [item.key for item in first.features].count("liquidity_sweep.level") == 1
    assert first.diagnostics["levels_republished"] == 1

    later_bar = candle(4, 10.4, 11.4, 9.4, 10.4)
    second = restored.on_bar(view(bars + [next_bar, later_bar]))
    restored_level_id = first.features[0].feature_id
    assert not any(item.feature_id == restored_level_id for item in second.features)
    assert second.diagnostics["levels_republished"] == 0


def test_restart_does_not_duplicate_level_already_visible_in_feature_store():
    source = make_module()
    bars = high_level_bars()
    decision = feed(source, bars)[-1]
    existing = created_level(decision)
    restored = make_module()
    restored.restore_state(source.snapshot_state())

    class ExistingFeatures:
        def get_all(self, *_args, **_kwargs):
            return (existing,)

    next_bar = candle(3, 10.5, 11.5, 9.5, 10.5)
    result = restored.on_bar(view(bars + [next_bar], features=ExistingFeatures()))
    assert not any(item.feature_id == existing.feature_id for item in result.features)
    assert result.diagnostics["levels_republished"] == 0


def test_single_bar_can_sweep_multiple_independent_active_levels():
    module = make_module(min_wick_to_body_ratio=0.0, max_penetration_bps=200.0)
    bars = [
        candle(0, 9.0, 10.0, 8.0, 9.0),
        candle(1, 10.0, 12.0, 9.0, 11.0),
        candle(2, 10.0, 11.0, 8.5, 9.5),
        candle(3, 9.0, 10.0, 7.0, 8.0),
        candle(4, 9.0, 11.0, 8.0, 10.0),
        candle(5, 10.0, 12.1, 6.9, 10.0),
    ]
    decision = feed(module, bars)[-1]
    events = [item for item in decision.features if item.key == "liquidity_sweep.event"]
    assert {item.value["direction"] for item in events} == {"bearish", "bullish"}
    assert len(decision.setups) == 4
    assert len([item for item in decision.feature_deletes if item.reason == "level_swept"]) == 2


def test_extreme_finite_market_values_never_publish_infinite_metrics():
    module = make_module(min_wick_to_body_ratio=0.0)
    bars = [
        candle(0, 9.0e307, 9.5e307, 8.5e307, 9.0e307),
        candle(1, 9.5e307, 1.0e308, 9.0e307, 9.5e307),
        candle(2, 9.4e307, 9.8e307, 9.1e307, 9.4e307),
        candle(3, 1.0e308, 1.005e308, 9.9e307, 1.0e308),
    ]
    event = sweep_event(feed(module, bars)[-1])
    for field in ("penetration_bps", "wick_to_body_ratio", "wick_fraction", "extreme_price"):
        assert math.isfinite(event.value[field])
