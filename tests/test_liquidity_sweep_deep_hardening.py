from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import random
from types import MappingProxyType

import pytest

from modular_core import Candle, FeatureStore, MarketView
from strategy_modules.liquidity_sweep import (
    LEVEL_FEATURE_KEY,
    MODULE_VERSION,
    SWEEP_FEATURE_KEY,
    LiquiditySweepModule,
    LiquiditySweepReason,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        symbol=symbol,
        timeframe=timeframe,
    )


def market_view(
    candle: Candle,
    index: int,
    *,
    data_gap: bool = False,
    features=None,
    timeframe_bars=None,
) -> MarketView:
    return MarketView(
        base_bars=(candle,),
        timeframe_bars=timeframe_bars or {},
        index=index,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
        features=features,
    )


def feed(module: LiquiditySweepModule, candles: list[Candle]):
    return [module.on_bar(market_view(candle, index)) for index, candle in enumerate(candles)]


def high_level_prefix(scale: float = 1.0) -> list[Candle]:
    return [
        bar(0, 9 * scale, 10 * scale, 8 * scale, 9 * scale),
        bar(1, 10 * scale, 12 * scale, 9 * scale, 11 * scale),
        bar(2, 10.5 * scale, 11 * scale, 9.5 * scale, 10 * scale),
    ]


def low_level_prefix(scale: float = 1.0) -> list[Candle]:
    return [
        bar(0, 10 * scale, 11 * scale, 9 * scale, 10 * scale),
        bar(1, 9 * scale, 10 * scale, 7 * scale, 8 * scale),
        bar(2, 8.5 * scale, 10 * scale, 8 * scale, 9 * scale),
    ]


def module(**kwargs) -> LiquiditySweepModule:
    defaults = {
        "pivot_left_bars": 1,
        "pivot_right_bars": 1,
        "min_wick_to_body_ratio": 0.0,
        "min_wick_fraction": 0.0,
        "max_penetration_bps": 500.0,
    }
    defaults.update(kwargs)
    return LiquiditySweepModule(**defaults)


def feature_by_key(decision, key: str):
    return next(feature for feature in decision.features if feature.key == key)


def test_module_version_bumped_without_state_schema_break():
    instance = module()
    assert MODULE_VERSION == "0.1.2"
    assert instance.manifest.version == "0.1.2"
    assert instance.manifest.state_schema_version == "1"


def test_public_reason_enum_preserves_wire_values():
    assert LiquiditySweepReason.DUPLICATE_BAR.value == "liquidity_sweep.duplicate_bar"
    assert LiquiditySweepReason.LEVEL_SWEPT.value == "level_swept"
    assert LiquiditySweepReason.WICK_RATIO_BELOW_MIN.value == "wick_ratio_below_min"


def test_zero_body_wick_ratio_is_scale_independent_for_tiny_price_high_sweep():
    scale = 1e-20
    instance = module()
    prefix = high_level_prefix(scale)
    feed(instance, prefix)
    sweep = bar(3, 10 * scale, 12.1 * scale, 9.8 * scale, 10 * scale)
    decision = instance.on_bar(market_view(sweep, 3))
    event = feature_by_key(decision, SWEEP_FEATURE_KEY)
    assert event.value["direction"] == "bearish"
    assert math.isfinite(event.value["wick_to_body_ratio"])
    assert event.value["wick_to_body_ratio"] == pytest.approx(
        float.fromhex("0x1.fffffffffffffp+1023")
    )


def test_zero_body_wick_ratio_is_scale_independent_for_tiny_price_low_sweep():
    scale = 1e-20
    instance = module()
    prefix = low_level_prefix(scale)
    feed(instance, prefix)
    sweep = bar(3, 9 * scale, 9.2 * scale, 6.9 * scale, 9 * scale)
    event = feature_by_key(instance.on_bar(market_view(sweep, 3)), SWEEP_FEATURE_KEY)
    assert event.value["direction"] == "bullish"
    assert math.isfinite(event.value["wick_to_body_ratio"])


def test_nonzero_tiny_body_uses_exact_decimal_ratio_not_absolute_epsilon():
    scale = 1e-20
    instance = module(min_wick_to_body_ratio=10.0)
    feed(instance, high_level_prefix(scale))
    sweep = bar(3, 10.0 * scale, 12.1 * scale, 9.8 * scale, 10.01 * scale)
    event = feature_by_key(instance.on_bar(market_view(sweep, 3)), SWEEP_FEATURE_KEY)
    assert event.value["wick_to_body_ratio"] > 100


def test_restore_then_exact_duplicate_republishes_level_without_advancing_sequence():
    source = module()
    prefix = high_level_prefix()
    feed(source, prefix)
    restored = module()
    restored.restore_state(source.snapshot_state())
    before = restored.snapshot_state()["sequence"]
    decision = restored.on_bar(market_view(prefix[-1], 2))
    assert [feature.key for feature in decision.features] == [LEVEL_FEATURE_KEY]
    assert decision.skips == (LiquiditySweepReason.DUPLICATE_BAR.value,)
    assert decision.diagnostics["levels_republished"] == 1
    assert restored.snapshot_state()["sequence"] == before


def test_restore_then_repeated_htf_bar_republishes_without_skip():
    source = module(timeframe="15m")
    htf = [
        bar(0, 9, 10, 8, 9, timeframe="15m"),
        bar(3, 10, 12, 9, 11, timeframe="15m"),
        bar(6, 10.5, 11, 9.5, 10, timeframe="15m"),
    ]
    for index, current in enumerate(htf):
        source.on_bar(market_view(current, index, timeframe_bars={"15m": tuple(htf[: index + 1])}))
    restored = module(timeframe="15m")
    restored.restore_state(source.snapshot_state())
    decision = restored.on_bar(market_view(htf[-1], 7, timeframe_bars={"15m": tuple(htf)}))
    assert decision.skips == ()
    assert [feature.key for feature in decision.features] == [LEVEL_FEATURE_KEY]
    assert decision.diagnostics["state"] == "waiting_for_new_timeframe_bar"


def test_first_post_restore_sweep_publishes_level_before_event_then_deletes():
    source = module()
    prefix = high_level_prefix()
    feed(source, prefix)
    restored = module()
    restored.restore_state(source.snapshot_state())
    sweep = bar(3, 11.5, 12.1, 10.5, 11.0)
    decision = restored.on_bar(market_view(sweep, 3))
    assert [feature.key for feature in decision.features[:2]] == [
        LEVEL_FEATURE_KEY,
        SWEEP_FEATURE_KEY,
    ]
    assert any(
        item.reason == LiquiditySweepReason.LEVEL_SWEPT.value for item in decision.feature_deletes
    )
    assert decision.diagnostics["levels_republished"] == 1


def test_first_post_restore_invalidation_publishes_level_before_deletion():
    source = module()
    feed(source, high_level_prefix())
    restored = module()
    restored.restore_state(source.snapshot_state())
    breakout = bar(3, 12.1, 12.8, 11.8, 12.4)
    decision = restored.on_bar(market_view(breakout, 3))
    assert decision.features[0].key == LEVEL_FEATURE_KEY
    assert any(
        item.reason == LiquiditySweepReason.ACCEPTED_BREAKOUT.value
        for item in decision.feature_deletes
    )


def test_restore_does_not_duplicate_level_visible_in_feature_store():
    source = module()
    prefix = high_level_prefix()
    level_decision = feed(source, prefix)[-1]
    level = feature_by_key(level_decision, LEVEL_FEATURE_KEY)
    store = FeatureStore()
    store.publish(level)
    restored = module()
    restored.restore_state(source.snapshot_state())
    decision = restored.on_bar(
        market_view(prefix[-1], 2, features=store.snapshot(prefix[-1].close_time))
    )
    assert decision.features == ()
    assert decision.diagnostics["levels_republished"] == 0


def test_gap_after_restore_clears_pending_republish_instead_of_republishing_stale_level():
    source = module()
    prefix = high_level_prefix()
    feed(source, prefix)
    restored = module()
    restored.restore_state(source.snapshot_state())
    new_bar = bar(3, 10, 11, 9, 10)
    decision = restored.on_bar(market_view(new_bar, 3, data_gap=True))
    assert not any(feature.key == LEVEL_FEATURE_KEY for feature in decision.features)
    assert any(
        item.reason == LiquiditySweepReason.DATA_GAP.value for item in decision.feature_deletes
    )


def test_runtime_restore_diagnostics_are_not_persisted_into_checkpoint_schema():
    source = module()
    feed(source, high_level_prefix())
    restored = module()
    restored.restore_state(source.snapshot_state())
    decision = restored.on_bar(market_view(high_level_prefix()[-1], 2))
    assert decision.diagnostics["runtime_restore_count"] == 1
    assert decision.diagnostics["runtime_republished_levels"] == 1
    state = restored.snapshot_state()
    assert "runtime_restore_count" not in state
    assert "runtime_republished_levels" not in state


def test_failed_restore_does_not_increment_runtime_restore_count():
    instance = module()
    broken = dict(instance.snapshot_state())
    broken["schema_version"] = "999"
    with pytest.raises(ValueError):
        instance.restore_state(broken)
    decision = instance.on_bar(market_view(bar(0, 9, 10, 8, 9), 0))
    assert decision.diagnostics["runtime_restore_count"] == 0


def test_future_data_invariance_for_prefix_decisions():
    candles = high_level_prefix() + [
        bar(3, 11.5, 12.1, 10.5, 11.0),
        bar(4, 11, 13, 10, 12),
        bar(5, 12, 14, 11, 13),
    ]
    prefix_instance = module()
    prefix_decisions = feed(prefix_instance, candles[:4])
    full_instance = module()
    full_decisions = feed(full_instance, candles)
    assert prefix_decisions == full_decisions[:4]


def test_high_low_symmetry_across_random_scales_and_penetrations():
    rng = random.Random(20260721)
    for case in range(100):
        scale = 10 ** rng.uniform(-12, 12)
        penetration = rng.uniform(0.0001, 0.02)
        high_instance = module(max_penetration_bps=500.0)
        low_instance = module(max_penetration_bps=500.0)
        feed(high_instance, high_level_prefix(scale))
        feed(low_instance, low_level_prefix(scale))
        high_level = 12 * scale
        low_level = 7 * scale
        high_bar = bar(3, 10 * scale, high_level * (1 + penetration), 9 * scale, 10 * scale)
        low_bar = bar(3, 9 * scale, 10 * scale, low_level * (1 - penetration), 9 * scale)
        high_event = feature_by_key(
            high_instance.on_bar(market_view(high_bar, case + 3)), SWEEP_FEATURE_KEY
        )
        low_event = feature_by_key(
            low_instance.on_bar(market_view(low_bar, case + 3)), SWEEP_FEATURE_KEY
        )
        assert high_event.value["penetration_bps"] == pytest.approx(
            low_event.value["penetration_bps"], rel=1e-12
        )
        assert high_event.value["direction"] == "bearish"
        assert low_event.value["direction"] == "bullish"


def test_config_fingerprint_is_canonical_for_equivalent_numeric_inputs():
    first = module(min_penetration_bps=1, max_penetration_bps=100)
    second = module(min_penetration_bps=1.0, max_penetration_bps=100.0)
    assert (
        first.snapshot_state()["config_fingerprint"]
        == second.snapshot_state()["config_fingerprint"]
    )


def test_public_decision_payloads_remain_deeply_immutable():
    instance = module()
    decision = feed(instance, high_level_prefix())[-1]
    level = feature_by_key(decision, LEVEL_FEATURE_KEY)
    assert isinstance(level.value, MappingProxyType)
    with pytest.raises(TypeError):
        level.value["price"] = 99  # type: ignore[index]


def test_deterministic_replay_after_midstream_checkpoint():
    rng = random.Random(7)
    candles: list[Candle] = []
    price = 100.0
    for index in range(300):
        price += rng.uniform(-0.4, 0.4)
        open_ = max(price + rng.uniform(-0.1, 0.1), 1.0)
        close = max(price + rng.uniform(-0.1, 0.1), 1.0)
        candles.append(
            bar(
                index,
                open_,
                max(open_, close) + rng.uniform(0.01, 0.5),
                min(open_, close) - rng.uniform(0.01, 0.5),
                close,
            )
        )
    uninterrupted = module()
    full = feed(uninterrupted, candles)
    first_half = module()
    left = feed(first_half, candles[:150])
    resumed = module()
    resumed.restore_state(first_half.snapshot_state())
    right = [
        resumed.on_bar(market_view(candle, index))
        for index, candle in enumerate(candles[150:], 150)
    ]
    assert [
        [feature.value for feature in decision.features if feature.key == SWEEP_FEATURE_KEY]
        for decision in full[150:]
    ] == [
        [feature.value for feature in decision.features if feature.key == SWEEP_FEATURE_KEY]
        for decision in right
    ]
    assert uninterrupted.snapshot_state() == resumed.snapshot_state()
    assert left == full[:150]


def test_checkpoint_size_and_state_cardinality_remain_bounded_on_long_stream():
    rng = random.Random(11)
    instance = module(max_active_levels=16)
    price = 100.0
    for index in range(20_000):
        price += rng.uniform(-0.5, 0.5)
        open_ = max(price, 1.0)
        close = max(price + rng.uniform(-0.1, 0.1), 1.0)
        candle = bar(index, open_, max(open_, close) + 0.2, min(open_, close) - 0.2, close)
        instance.on_bar(market_view(candle, index))
    state = instance.snapshot_state()
    assert len(state["bars"]) == instance.config.pivot_window_bars
    assert len(state["levels"]) <= 16
    assert len(json.dumps(state).encode()) < 10_000


def test_all_public_reasons_are_unique():
    values = [reason.value for reason in LiquiditySweepReason]
    assert len(values) == len(set(values))


def test_internal_measurement_rejects_nan_and_invalid_geometry():
    from decimal import Decimal
    from strategy_modules.liquidity_sweep.models import SweepDirection, SweepMeasurement

    with pytest.raises(ValueError, match="penetration"):
        SweepMeasurement(
            SweepDirection.BEARISH, Decimal("NaN"), Decimal("1"), Decimal("0.5"), Decimal("1")
        )
    with pytest.raises(ValueError, match="wick_fraction"):
        SweepMeasurement(
            SweepDirection.BEARISH, Decimal("1"), Decimal("1"), Decimal("1.1"), Decimal("1")
        )
    with pytest.raises(ValueError, match="extreme_price"):
        SweepMeasurement(
            SweepDirection.BEARISH, Decimal("1"), Decimal("1"), Decimal("0.5"), Decimal("0")
        )


def test_liquidity_level_requires_positive_confirmation_sequence():
    from decimal import Decimal
    from strategy_modules.liquidity_sweep.models import LiquidityLevel, LiquiditySide

    with pytest.raises(ValueError, match="positive"):
        LiquidityLevel(
            level_id="level",
            feature_id="feature",
            side=LiquiditySide.HIGH,
            price=Decimal("1"),
            symbol="BTCUSDT",
            timeframe="5m",
            pivot_time=START,
            confirmed_at=START + timedelta(minutes=5),
            confirmed_sequence=0,
        )


def test_real_module_registry_loads_patch_version_from_public_import_path():
    from modular_core import ModuleRegistry

    loaded = ModuleRegistry().load_import_path(
        "strategy_modules.liquidity_sweep:LiquiditySweepModule",
        {"pivot_left_bars": 1, "pivot_right_bars": 1},
    )
    plan = ModuleRegistry().validate_and_plan((loaded,), base_timeframe="5m")
    assert plan.modules == (loaded,)
    assert plan.manifests[0].version == "0.1.2"
    assert plan.required_timeframes == ()


def test_real_backtest_engine_lifecycle_remains_order_free():
    from modular_core import BacktestEngine

    instance = module(emit_signals=True)
    candles = high_level_prefix() + [bar(3, 11.5, 12.1, 10.5, 11.0)]
    result = BacktestEngine(instance).run(candles)
    assert result.bars_processed == 4
    assert not result.journal.errors
    assert any(row["feature"].key == SWEEP_FEATURE_KEY for row in result.journal.features)
    assert result.journal.signals
    assert result.journal.orders == []
    assert result.checkpoint.module_states[0].module_version == "0.1.2"
