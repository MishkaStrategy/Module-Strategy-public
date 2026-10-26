from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from types import MappingProxyType

import pytest

from modular_core import BacktestEngine, Candle, MarketView
from modular_core.checkpoints import capture_module_states, restore_module_states
from modular_core.serialization import deep_freeze
from strategy_modules.liquidity_sweep import LiquiditySweepConfig, LiquiditySweepModule
from strategy_modules.liquidity_sweep.models import BarSnapshot, LiquidityLevel, LiquiditySide

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    timeframe: str = "15m",
    symbol: str = "BTCUSDT",
) -> Candle:
    return Candle(
        START + timedelta(minutes=15 * index), open_, high, low, close, 1.0, symbol, timeframe
    )


def market_view(history: list[Candle]) -> MarketView:
    return MarketView(
        base_bars=tuple(history),
        timeframe_bars={},
        index=len(history) - 1,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
    )


def module(**overrides: object) -> LiquiditySweepModule:
    parameters: dict[str, object] = {
        "pivot_left_bars": 1,
        "pivot_right_bars": 1,
        "min_level_age_bars": 0,
        "min_wick_to_body_ratio": 0.0,
        "min_wick_fraction": 0.0,
    }
    parameters.update(overrides)
    return LiquiditySweepModule(**parameters)


def prefix() -> list[Candle]:
    return [
        candle(0, 9.0, 10.0, 8.0, 9.0),
        candle(1, 10.0, 12.0, 9.0, 11.0),
        candle(2, 10.5, 11.0, 9.5, 10.0),
    ]


def feed(instance: LiquiditySweepModule, bars: list[Candle]) -> None:
    history: list[Candle] = []
    for item in bars:
        history.append(item)
        instance.on_bar(market_view(history))


def test_kernel_deep_frozen_module_state_round_trip() -> None:
    source = module()
    feed(source, prefix())
    states = capture_module_states((source,))
    assert isinstance(states[0].payload["bars"], tuple)
    restored = module()
    restore_module_states((restored,), states)
    assert restored.snapshot_state() == source.snapshot_state()


def test_restore_accepts_deep_frozen_tuple_state_directly() -> None:
    source = module()
    feed(source, prefix())
    frozen = deep_freeze(source.snapshot_state())
    restored = module()
    restored.restore_state(frozen)
    assert restored.snapshot_state() == source.snapshot_state()


def test_same_engine_instance_can_run_twice_without_state_leakage() -> None:
    instance = module()
    engine = BacktestEngine(instance)
    first = engine.run(prefix())
    second = engine.run(prefix())
    assert first.checkpoint.module_states[0].payload == second.checkpoint.module_states[0].payload
    assert not second.journal.errors
    assert not any(row["reason"].endswith("out_of_order_bar") for row in second.journal.skips)
    assert not any(row["reason"].endswith("duplicate_bar") for row in second.journal.skips)


def test_restored_state_is_preserved_when_engine_calls_on_start() -> None:
    source = module()
    feed(source, prefix())
    restored = module()
    restored.restore_state(source.snapshot_state())
    before = restored.snapshot_state()
    restored.on_start()
    assert restored.snapshot_state() == before


def test_default_base_timeframe_accepts_default_completed_candle() -> None:
    item = Candle(START, 10.0, 11.0, 9.0, 10.0, symbol="BTCUSDT")
    decision = LiquiditySweepModule().on_bar(market_view([item]))
    assert decision.diagnostics["processed_sequence"] == 1


def test_explicit_timeframe_still_requires_close_after_open() -> None:
    with pytest.raises(ValueError, match="after timestamp"):
        BarSnapshot(
            timestamp=START,
            close_time=START,
            symbol="BTCUSDT",
            timeframe="15m",
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        )


@pytest.mark.parametrize(
    ("path", "bad_value"),
    [
        (("bars", 0, "symbol"), 123),
        (("bars", 0, "timeframe"), None),
        (("levels", 0, "level_id"), 123),
        (("levels", 0, "feature_id"), None),
        (("levels", 0, "symbol"), 123),
        (("levels", 0, "timeframe"), None),
    ],
)
def test_restore_rejects_non_string_identifiers(
    path: tuple[object, ...], bad_value: object
) -> None:
    source = module()
    feed(source, prefix())
    broken = deepcopy(dict(source.snapshot_state()))
    target: object = broken
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = bad_value  # type: ignore[index]
    restored = module()
    before = restored.snapshot_state()
    with pytest.raises(ValueError, match="string"):
        restored.restore_state(broken)
    assert restored.snapshot_state() == before


def test_restore_rejects_feature_identity_collision() -> None:
    source = module(max_active_levels=4)
    feed(source, prefix())
    state = deepcopy(dict(source.snapshot_state()))
    duplicate = deepcopy(state["levels"][0])
    duplicate["level_id"] = "liq-level-aaaaaaaaaaaaaaaaaaaaaaaa"
    state["levels"].append(duplicate)
    with pytest.raises(ValueError, match="deterministic"):
        module(max_active_levels=4).restore_state(state)


def test_restore_rejects_nondeterministic_feature_id() -> None:
    source = module()
    feed(source, prefix())
    state = deepcopy(dict(source.snapshot_state()))
    state["levels"][0]["feature_id"] = "feat-wrong"
    with pytest.raises(ValueError, match="deterministic"):
        module().restore_state(state)


def test_stream_change_resets_even_when_new_stream_timestamp_is_earlier() -> None:
    instance = module()
    feed(instance, prefix())
    changed = candle(-10, 10.0, 11.0, 9.0, 10.0, symbol="ETHUSDT")
    decision = instance.on_bar(market_view([changed]))
    assert "liquidity_sweep.stream_changed_reset" in decision.skips
    assert "liquidity_sweep.out_of_order_bar" not in decision.skips


def test_equivalent_timeframe_case_does_not_reset_stream() -> None:
    instance = module()
    first = candle(0, 10.0, 11.0, 9.0, 10.0, timeframe="15M")
    second = candle(1, 10.0, 11.0, 9.0, 10.0, timeframe="15m")
    instance.on_bar(market_view([first]))
    decision = instance.on_bar(market_view([first, second]))
    assert "liquidity_sweep.stream_changed_reset" not in decision.skips


def test_semantically_equal_decimal_scales_are_duplicate_not_conflict() -> None:
    first = BarSnapshot(
        timestamp=START,
        close_time=START + timedelta(minutes=15),
        symbol="BTCUSDT",
        timeframe="15m",
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("0.5"),
        close=Decimal("1"),
    )
    second = BarSnapshot(
        timestamp=START,
        close_time=START + timedelta(minutes=15),
        symbol="BTCUSDT",
        timeframe="15m",
        open=Decimal("1.0"),
        high=Decimal("2.00"),
        low=Decimal("0.500"),
        close=Decimal("1.000"),
    )
    assert first.fingerprint() == second.fingerprint()


def test_negative_and_positive_zero_share_config_fingerprint() -> None:
    positive = LiquiditySweepModule(min_penetration_bps=0.0)
    negative = LiquiditySweepModule(min_penetration_bps=-0.0)
    assert (
        positive.snapshot_state()["config_fingerprint"]
        == negative.snapshot_state()["config_fingerprint"]
    )


def test_config_huge_integer_fails_with_stable_validation_error() -> None:
    with pytest.raises(ValueError, match="finite"):
        LiquiditySweepConfig(max_penetration_bps=10**10000)


def test_restored_extreme_price_never_publishes_infinite_public_value() -> None:
    level = LiquidityLevel(
        level_id="liq-level-extreme",
        feature_id="feat-liq-level-extreme",
        side=LiquiditySide.HIGH,
        price=Decimal("1e10000"),
        symbol="BTCUSDT",
        timeframe="15m",
        pivot_time=START,
        confirmed_at=START + timedelta(minutes=15),
        confirmed_sequence=1,
    )
    from strategy_modules.liquidity_sweep.outputs import level_feature

    value = level_feature("liquidity_sweep", level).value["price"]
    assert math.isfinite(value)


def test_restore_canonicalizes_level_order() -> None:
    source = module(max_active_levels=4)
    feed(source, prefix())
    state = deepcopy(dict(source.snapshot_state()))
    original = deepcopy(state["levels"][0])
    second = deepcopy(original)
    second["level_id"] = "liq-level-ffffffffffffffffffffffff"
    second["feature_id"] = "feat-liq-level-ffffffffffffffffffffffff"
    second["confirmed_sequence"] = original["confirmed_sequence"] + 1
    second["confirmed_at"] = original["confirmed_at"]
    state["levels"] = [second, original]
    state["sequence"] += 1
    state["stats"]["processed_bars"] += 1
    state["stats"]["levels_created"] += 1
    restored = module(max_active_levels=4)
    restored.restore_state(state)
    ids = [row["level_id"] for row in restored.snapshot_state()["levels"]]
    assert ids == [original["level_id"], second["level_id"]]


def test_mappingproxy_state_root_is_accepted() -> None:
    source = module()
    feed(source, prefix())
    restored = module()
    restored.restore_state(MappingProxyType(dict(source.snapshot_state())))
    assert restored.snapshot_state() == source.snapshot_state()


def test_restore_rejects_non_mapping_root_atomically() -> None:
    restored = module()
    before = restored.snapshot_state()
    with pytest.raises(ValueError, match="must be a mapping"):
        restored.restore_state([("schema_version", "1")])  # type: ignore[arg-type]
    assert restored.snapshot_state() == before


def test_restore_rejects_impossible_level_lifecycle_counters() -> None:
    source = module()
    feed(source, prefix())
    state = deepcopy(dict(source.snapshot_state()))
    state["stats"]["levels_created"] = 0
    with pytest.raises(ValueError, match="level"):
        module().restore_state(state)


def test_internal_timestamps_are_normalized_to_utc() -> None:
    offset = timezone(timedelta(hours=3))
    snapshot = BarSnapshot(
        timestamp=datetime(2026, 1, 1, 3, tzinfo=offset),
        close_time=datetime(2026, 1, 1, 3, 15, tzinfo=offset),
        symbol="BTCUSDT",
        timeframe="15M",
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
    )
    assert snapshot.timestamp == START
    assert snapshot.close_time == START + timedelta(minutes=15)
    assert snapshot.timeframe == "15m"


def test_engine_default_base_timeframe_runs_without_errors() -> None:
    bars = [
        Candle(
            START + timedelta(minutes=i), 10.0, 11.0, 9.0, 10.0, symbol="BTCUSDT", timeframe="1m"
        )
        for i in range(5)
    ]
    result = BacktestEngine(LiquiditySweepModule(pivot_left_bars=1, pivot_right_bars=1)).run(bars)
    assert result.bars_processed == len(bars)
    assert not result.journal.errors


def test_fresh_on_start_resets_runtime_observability() -> None:
    source = module()
    feed(source, prefix())
    restored = module()
    restored.restore_state(source.snapshot_state())
    assert restored._runtime_restore_count == 1
    restored.on_start()
    restored.on_finish()
    restored.on_start()
    assert restored.snapshot_state()["sequence"] == 0
    assert restored._runtime_restore_count == 0
    assert restored._runtime_republished_levels == 0
