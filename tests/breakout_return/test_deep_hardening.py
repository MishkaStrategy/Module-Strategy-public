from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_core import BacktestEngine, Candle, Feature, FeatureView, MarketView, SetupStatus, Side
from strategy_modules.breakout_return import (
    BreakoutReturnDataError,
    BreakoutReturnModule,
    BreakoutReturnStateError,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def bar(
    i: int,
    o: float | int,
    h: float | int,
    low: float | int,
    c: float | int,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    volume: float | int = 1,
) -> Candle:
    return Candle(START + timedelta(minutes=5 * i), o, h, low, c, volume, symbol, timeframe)


def view(
    bars: list[Candle], *, features: tuple[Feature, ...] = (), gap: bool = False
) -> MarketView:
    current = bars[-1]
    return MarketView(
        base_bars=bars,
        timeframe_bars={},
        index=len(bars) - 1,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        data_gap=gap,
        features=FeatureView(features, current.close_time),
    )


def feed(
    module: BreakoutReturnModule,
    bars: list[Candle],
    *,
    features_by_bar: list[tuple[Feature, ...]] | None = None,
    gaps: list[bool] | None = None,
):
    visible: list[Candle] = []
    decisions = []
    features_by_bar = features_by_bar or [()] * len(bars)
    gaps = gaps or [False] * len(bars)
    for index, current in enumerate(bars):
        visible.append(current)
        decisions.append(
            module.on_bar(view(visible, features=features_by_bar[index], gap=gaps[index]))
        )
    return decisions


def seed() -> list[Candle]:
    return [bar(0, 95, 100, 90, 96), bar(1, 96, 99, 91, 95), bar(2, 95, 98, 92, 94)]


def completed_range(
    created_index: int = 0,
    *,
    schema: str = "1",
    timeframe: str = "5m",
    source: str = "range_builder",
    range_id: str = "range-1",
) -> Feature:
    created = bar(created_index, 95, 100, 90, 95).close_time
    return Feature(
        key="range.current",
        source_module=source,
        created_at=created,
        value={"high": 100.0, "low": 90.0, "range_id": range_id},
        symbol="BTCUSDT",
        timeframe=timeframe,
        schema_version=schema,
        feature_id=f"{source}:{range_id}:{created_index}",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback_bars": True},
        {"lookback_bars": 2.5},
        {"lookback_bars": 100_001},
        {"max_return_bars": True},
        {"max_return_bars": 1.5},
        {"max_return_bars": 100_001},
        {"max_reference_age_seconds": True},
        {"max_reference_age_seconds": 0},
        {"reference_feature_key": " range.current"},
        {"reference_feature_key": "range..current"},
        {"reference_feature_timeframe": " 5m"},
        {"reference_feature_schema_version": ""},
        {"breakout_buffer_bps": True},
        {"min_return_depth": "0.1"},
    ],
)
def test_direct_constructor_strictly_rejects_invalid_types(kwargs):
    with pytest.raises(ValueError, match="invalid breakout_return configuration"):
        BreakoutReturnModule(**kwargs)


def test_decimal_configuration_is_canonical_and_registry_compatible():
    module = BreakoutReturnModule(
        breakout_buffer_bps=Decimal("10.000"),
        min_return_depth=Decimal("0.0200"),
        max_return_depth=Decimal("0.2000"),
    )
    assert module.parameters["breakout_buffer_bps"] == 10
    assert module.parameters["min_return_depth"] == 0.02
    assert module.parameters["max_return_depth"] == 0.2


def test_optional_parameters_are_omitted_when_disabled_for_run_identity_compatibility():
    module = BreakoutReturnModule()
    assert "reference_feature_timeframe" not in module.parameters
    assert "reference_feature_schema_version" not in module.parameters
    assert "max_reference_age_seconds" not in module.parameters


def test_numeric_equivalent_duplicate_is_idempotent():
    module = BreakoutReturnModule(lookback_bars=2)
    first = bar(0, 95, 100, 90, 96, volume=1)
    equivalent = bar(0, 95.0, 100.0, 90.0, 96.0, volume=1.0)
    module.on_bar(view([first]))
    decision = module.on_bar(view([equivalent]))
    assert decision.skips == ("breakout_return.duplicate_event_ignored",)
    assert module.diagnostic_counters["duplicate_events"] == 1


def test_true_conflicting_duplicate_still_raises():
    module = BreakoutReturnModule(lookback_bars=2)
    module.on_bar(view([bar(0, 95, 100, 90, 96)]))
    with pytest.raises(BreakoutReturnDataError, match="contradictory duplicate"):
        module.on_bar(view([bar(0, 95, 101, 90, 96)]))


def test_v2_state_rejects_different_effective_configuration_atomically():
    original = BreakoutReturnModule(lookback_bars=3, max_return_bars=6)
    feed(original, seed())
    state = original.snapshot_state()
    target = BreakoutReturnModule(lookback_bars=5, max_return_bars=6)
    before = target.snapshot_state()
    with pytest.raises(BreakoutReturnStateError, match="configuration fingerprint"):
        target.restore_state(state)
    assert target.snapshot_state() == before


def test_v2_state_roundtrip_preserves_counters_and_fingerprint():
    module = BreakoutReturnModule(lookback_bars=3)
    bars = seed() + [bar(3, 101, 104, 100.5, 103)]
    feed(module, bars)
    state = json.loads(json.dumps(module.snapshot_state()))
    restored = BreakoutReturnModule(lookback_bars=3)
    restored.restore_state(state)
    assert restored.config_fingerprint == module.config_fingerprint
    assert restored.snapshot_state()["active"] == state["active"]
    assert restored.diagnostic_counters["bars_processed"] == len(bars)
    assert restored.diagnostic_counters["restores"] == 1


def test_legacy_v1_state_migrates_and_canonicalizes_signature():
    module = BreakoutReturnModule(lookback_bars=2)
    legacy = {
        "payload_version": 1,
        "sequence": 2,
        "history_count": 2,
        "maxima": [[1, "100.0"], [2, "99.00"]],
        "minima": [[1, "90.0"], [2, "91.00"]],
        "active": None,
        "last_bar_time": bar(1, 96, 99, 91, 95).timestamp.isoformat(),
        "last_bar_signature": [
            bar(1, 96, 99, 91, 95).timestamp.isoformat(),
            "96.0",
            "99.00",
            "91.000",
            "95.0",
            "1.00",
            "BTCUSDT",
            "5m",
        ],
        "bound_symbol": "BTCUSDT",
        "bound_timeframe": "5m",
    }
    module.restore_state(legacy)
    state = module.snapshot_state()
    assert state["payload_version"] == 2
    assert state["last_bar_signature"][1:6] == ["96", "99", "91", "95", "1"]
    assert module.diagnostic_counters["legacy_state_migrations"] == 1


def test_restored_extrema_must_be_inside_history_horizon():
    module = BreakoutReturnModule(lookback_bars=3)
    state = {
        "payload_version": 1,
        "sequence": 100,
        "history_count": 3,
        "maxima": [[1, "100"], [100, "99"]],
        "minima": [[98, "90"], [100, "91"]],
        "active": None,
        "last_bar_time": "2026-01-01T00:00:00+00:00",
        "last_bar_signature": [
            "2026-01-01T00:00:00+00:00",
            "95",
            "99",
            "91",
            "96",
            "1",
            "BTCUSDT",
            "5m",
        ],
        "bound_symbol": "BTCUSDT",
        "bound_timeframe": "5m",
    }
    with pytest.raises(BreakoutReturnStateError, match="rolling-history horizon"):
        module.restore_state(state)


def test_restored_current_extrema_must_match_last_bar():
    module = BreakoutReturnModule(lookback_bars=2)
    state = {
        "payload_version": 1,
        "sequence": 2,
        "history_count": 2,
        "maxima": [[1, "100"], [2, "98"]],
        "minima": [[1, "90"], [2, "91"]],
        "active": None,
        "last_bar_time": bar(1, 96, 99, 91, 95).timestamp.isoformat(),
        "last_bar_signature": [
            bar(1, 96, 99, 91, 95).timestamp.isoformat(),
            "96",
            "99",
            "91",
            "95",
            "1",
            "BTCUSDT",
            "5m",
        ],
        "bound_symbol": "BTCUSDT",
        "bound_timeframe": "5m",
    }
    with pytest.raises(BreakoutReturnStateError, match="match the last bar high/low"):
        module.restore_state(state)


def test_state_unknown_keys_rejected_without_mutation():
    module = BreakoutReturnModule(lookback_bars=2)
    feed(module, seed()[:2])
    state = deepcopy(dict(module.snapshot_state()))
    state["unexpected"] = 1
    before = module.snapshot_state()
    with pytest.raises(BreakoutReturnStateError, match="unknown"):
        module.restore_state(state)
    assert module.snapshot_state() == before


def test_nested_unknown_active_key_rejected():
    module = BreakoutReturnModule(lookback_bars=3)
    feed(module, seed() + [bar(3, 101, 104, 100.5, 103)])
    state = deepcopy(dict(module.snapshot_state()))
    state["active"] = dict(state["active"])
    state["active"]["unexpected"] = "x"
    with pytest.raises(BreakoutReturnStateError, match="active keys"):
        BreakoutReturnModule(lookback_bars=3).restore_state(state)


def test_tampered_deterministic_setup_id_rejected():
    module = BreakoutReturnModule(lookback_bars=3)
    feed(module, seed() + [bar(3, 101, 104, 100.5, 103)])
    state = deepcopy(dict(module.snapshot_state()))
    state["active"] = dict(state["active"])
    state["active"]["setup_id"] = "tampered"
    with pytest.raises(BreakoutReturnStateError, match="deterministic identity"):
        BreakoutReturnModule(lookback_bars=3).restore_state(state)


def test_non_string_state_identifiers_are_rejected():
    module = BreakoutReturnModule(lookback_bars=3)
    feed(module, seed() + [bar(3, 101, 104, 100.5, 103)])
    state = deepcopy(dict(module.snapshot_state()))
    state["active"] = dict(state["active"])
    state["active"]["reference"] = dict(state["active"]["reference"])
    state["active"]["reference"]["identifier"] = None
    with pytest.raises(BreakoutReturnStateError):
        BreakoutReturnModule(lookback_bars=3).restore_state(state)


def test_invalid_counter_consistency_rejected():
    module = BreakoutReturnModule(lookback_bars=2)
    feed(module, seed()[:2])
    state = deepcopy(dict(module.snapshot_state()))
    state["counters"] = dict(state["counters"])
    state["counters"]["events_seen"] = 999
    with pytest.raises(BreakoutReturnStateError, match="events_seen"):
        BreakoutReturnModule(lookback_bars=2).restore_state(state)


def test_feature_schema_guard_returns_structured_reason():
    module = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_schema_version="2",
    )
    current = bar(1, 101, 104, 100.5, 103)
    decision = module.on_bar(view([current], features=(completed_range(schema="1"),)))
    assert decision.skips == ("breakout_return.reference_schema_mismatch",)
    assert decision.diagnostics["expected_schema_version"] == "2"


def test_feature_age_guard_returns_structured_stale_reason():
    module = BreakoutReturnModule(
        reference_mode="feature",
        max_reference_age_seconds=299,
    )
    feature = completed_range(created_index=0)
    current = bar(1, 101, 104, 100.5, 103)
    decision = module.on_bar(view([current], features=(feature,)))
    assert decision.skips == ("breakout_return.reference_stale",)
    assert decision.diagnostics["reference_age_seconds"] == 300.0


def test_feature_timeframe_filter_selects_only_configured_contract():
    module = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_timeframe="15m",
    )
    current = bar(1, 101, 104, 100.5, 103)
    wrong = completed_range(timeframe="5m")
    decision = module.on_bar(view([current], features=(wrong,)))
    assert decision.skips == ("breakout_return.reference_missing",)
    right = completed_range(timeframe="15m")
    module2 = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_timeframe="15m",
    )
    armed = module2.on_bar(view([current], features=(right,)))
    assert armed.setups[0].status is SetupStatus.ARMED


def test_feature_range_id_must_be_a_string_when_provided():
    module = BreakoutReturnModule(reference_mode="feature")
    current = bar(1, 101, 104, 100.5, 103)
    feature = Feature(
        key="range.current",
        source_module="range_builder",
        created_at=bar(0, 95, 100, 90, 95).close_time,
        value={"high": 100, "low": 90, "range_id": 123},
        symbol="BTCUSDT",
        timeframe="5m",
    )
    with pytest.raises(BreakoutReturnDataError, match="range_id"):
        module.on_bar(view([current], features=(feature,)))


def test_active_reference_source_change_invalidates_even_with_same_id_and_geometry():
    first = completed_range(source="producer_a")
    breakout = bar(1, 101, 104, 100.5, 103)
    module = BreakoutReturnModule(reference_mode="feature")
    module.on_bar(view([breakout], features=(first,)))
    second = completed_range(created_index=1, source="producer_b")
    waiting = bar(2, 103, 105, 102, 104)
    decision = module.on_bar(view([breakout, waiting], features=(second,)))
    assert decision.setups[0].status is SetupStatus.INVALIDATED
    assert decision.skips == ("breakout_return.reference_changed",)


def test_buy_sell_symmetry_for_equal_normalized_geometry():
    sell = BreakoutReturnModule(lookback_bars=3)
    sell_bars = seed() + [bar(3, 101, 104, 100.5, 103), bar(4, 99.5, 101, 98.5, 99)]
    sell_decision = feed(sell, sell_bars)[-1]

    buy = BreakoutReturnModule(lookback_bars=3)
    buy_bars = seed() + [bar(3, 89, 89.5, 86, 87), bar(4, 90.5, 92.5, 88, 91)]
    buy_decision = feed(buy, buy_bars)[-1]

    assert sell_decision.signals[0].side is Side.SELL
    assert buy_decision.signals[0].side is Side.BUY
    assert (
        sell_decision.signals[0].metadata["return_depth"]
        == buy_decision.signals[0].metadata["return_depth"]
        == 0.1
    )


def test_future_suffix_does_not_change_prefix_signal_absence():
    module = BreakoutReturnModule(lookback_bars=3)
    prefix = seed() + [bar(3, 101, 104, 100.5, 103)]
    prefix_result = BacktestEngine(module).run(prefix)
    assert prefix_result.journal.signals == []

    full = prefix + [bar(4, 99.5, 101, 98.5, 99)]
    full_result = BacktestEngine(BreakoutReturnModule(lookback_bars=3)).run(full)
    assert len(full_result.journal.signals) == 1
    assert (
        prefix_result.journal.setups[0]["setup"].setup_id
        == full_result.journal.setups[0]["setup"].setup_id
    )


def test_counters_are_bounded_and_explain_lifecycle():
    module = BreakoutReturnModule(lookback_bars=3)
    bars = seed() + [bar(3, 101, 104, 100.5, 103), bar(4, 99.5, 101, 98.5, 99)]
    feed(module, bars)
    counters = module.diagnostic_counters
    assert len(counters) == 13
    assert counters["events_seen"] == len(bars)
    assert counters["bars_processed"] == len(bars)
    assert counters["armed_above"] == 1
    assert counters["confirmed"] == 1


def test_state_size_is_bounded_by_rolling_window_not_total_history():
    module = BreakoutReturnModule(lookback_bars=50, direction="above", breakout_buffer_bps=5000)
    bars = []
    for i in range(5000):
        center = 150 - (i % 50) * 0.1
        bars.append(bar(i, center, center + 10, center - 10, center))
    feed(module, bars)
    state = json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":"))
    assert len(state) < 10_000
    assert len(module.snapshot_state()["maxima"]) <= 50
    assert len(module.snapshot_state()["minima"]) <= 50


def test_stream_validation_failure_is_atomic_for_persisted_state():
    module = BreakoutReturnModule(lookback_bars=2)
    first = bar(0, 95, 100, 90, 96)
    module.on_bar(view([first]))
    before = module.snapshot_state()
    with pytest.raises(BreakoutReturnDataError):
        module.on_bar(view([bar(0, 95, 101, 90, 96)]))
    assert module.snapshot_state() == before


def test_stale_age_boundary_is_inclusive_and_next_second_rejects():
    feature = completed_range(created_index=0)
    accepted = BreakoutReturnModule(reference_mode="feature", max_reference_age_seconds=300)
    current = bar(1, 101, 104, 100.5, 103)
    assert accepted.on_bar(view([current], features=(feature,))).setups

    rejected = BreakoutReturnModule(reference_mode="feature", max_reference_age_seconds=299)
    decision = rejected.on_bar(view([current], features=(feature,)))
    assert decision.skips == ("breakout_return.reference_stale",)


def test_config_fingerprint_is_representation_invariant_and_semantic_sensitive():
    one = BreakoutReturnModule(
        breakout_buffer_bps=10,
        min_return_depth=Decimal("0.0200"),
        max_return_depth=0.2,
    )
    equivalent = BreakoutReturnModule(
        breakout_buffer_bps=Decimal("10.0"),
        min_return_depth=0.02,
        max_return_depth=Decimal("0.20000"),
    )
    changed = BreakoutReturnModule(
        breakout_buffer_bps=11,
        min_return_depth=0.02,
        max_return_depth=0.2,
    )
    assert one.config_fingerprint == equivalent.config_fingerprint
    assert one.config_fingerprint != changed.config_fingerprint


def test_feature_guard_defaults_preserve_v1_unbounded_feature_behavior():
    module = BreakoutReturnModule(reference_mode="feature")
    current = bar(1000, 101, 104, 100.5, 103)
    decision = module.on_bar(view([current], features=(completed_range(created_index=0),)))
    assert decision.setups[0].status is SetupStatus.ARMED


def test_outputs_are_immutable_through_real_core_dtos():
    module = BreakoutReturnModule(lookback_bars=3)
    decision = feed(module, seed() + [bar(3, 101, 104, 100.5, 103)])[-1]
    with pytest.raises(TypeError):
        decision.setups[0].metadata["range_high"] = 999
    with pytest.raises(TypeError):
        decision.features[0].value["range_high"] = 999


def test_restore_rejects_oversized_extrema_collection():
    module = BreakoutReturnModule(lookback_bars=2)
    feed(module, seed()[:2])
    state = deepcopy(dict(module.snapshot_state()))
    state["maxima"] = [[1, "101"], [2, "100"], [3, "99"]]
    with pytest.raises(BreakoutReturnStateError):
        BreakoutReturnModule(lookback_bars=2).restore_state(state)


def test_expired_and_future_features_are_not_visible_to_module():
    current = bar(2, 101, 104, 100.5, 103)
    old = completed_range(created_index=0)
    expired = Feature(
        key=old.key,
        source_module=old.source_module,
        created_at=old.created_at,
        value=old.value,
        symbol=old.symbol,
        timeframe=old.timeframe,
        valid_until=bar(1, 95, 100, 90, 95).close_time,
        feature_id="expired-range",
    )
    decision = BreakoutReturnModule(reference_mode="feature").on_bar(
        view([current], features=(expired,))
    )
    assert decision.skips == ("breakout_return.reference_missing",)

    future = Feature(
        key=old.key,
        source_module=old.source_module,
        created_at=bar(3, 95, 100, 90, 95).close_time,
        value=old.value,
        symbol=old.symbol,
        timeframe=old.timeframe,
        feature_id="future-range",
    )
    future_decision = BreakoutReturnModule(reference_mode="feature").on_bar(
        view([current], features=(future,))
    )
    assert future_decision.skips == ("breakout_return.reference_missing",)


def test_core_checkpoint_capture_and_restore_uses_v2_payload():
    from modular_core import capture_module_states, restore_module_states

    source = BreakoutReturnModule(lookback_bars=3)
    feed(source, seed() + [bar(3, 101, 104, 100.5, 103)])
    states = capture_module_states([source])
    assert states[0].payload["payload_version"] == 2
    target = BreakoutReturnModule(lookback_bars=3)
    restore_module_states([target], states)
    assert target.snapshot_state()["active"] == source.snapshot_state()["active"]
    assert target.diagnostic_counters["restores"] == 1


def test_randomized_buy_sell_symmetry_property():
    import random

    rng = random.Random(20260721)
    for case in range(100):
        low = Decimal(str(rng.uniform(100, 1000)))
        width = Decimal(str(rng.uniform(0.1, 100)))
        high = low + width
        depth = Decimal(str(rng.uniform(0.02, 0.20)))
        extension = Decimal(str(rng.uniform(0.01, 0.5))) * width
        created = START + timedelta(minutes=case * 15)
        feature = Feature(
            key="range.current",
            source_module="property_range",
            created_at=created,
            value={"high": float(high), "low": float(low), "range_id": f"p-{case}"},
            symbol="BTCUSDT",
            timeframe="5m",
            feature_id=f"p-{case}",
        )
        sell_break = Candle(
            created,
            float(high + extension / 2),
            float(high + extension),
            float(high + extension / 4),
            float(high + extension / 2),
            1,
            "BTCUSDT",
            "5m",
        )
        sell_close = high - depth * width
        sell_return = Candle(
            created + timedelta(minutes=5),
            float(sell_close),
            float(high),
            float(sell_close - width * Decimal("0.01")),
            float(sell_close),
            1,
            "BTCUSDT",
            "5m",
        )
        buy_break = Candle(
            created,
            float(low - extension / 2),
            float(low - extension / 4),
            float(low - extension),
            float(low - extension / 2),
            1,
            "ETHUSDT",
            "5m",
        )
        buy_close = low + depth * width
        buy_feature = Feature(
            key="range.current",
            source_module="property_range",
            created_at=created,
            value={"high": float(high), "low": float(low), "range_id": f"p-{case}"},
            symbol="ETHUSDT",
            timeframe="5m",
            feature_id=f"p-buy-{case}",
        )
        buy_return = Candle(
            created + timedelta(minutes=5),
            float(buy_close),
            float(buy_close + width * Decimal("0.01")),
            float(low),
            float(buy_close),
            1,
            "ETHUSDT",
            "5m",
        )
        sell = BreakoutReturnModule(reference_mode="feature", return_confirmation="close")
        sell.on_bar(view([sell_break], features=(feature,)))
        sell_decision = sell.on_bar(view([sell_break, sell_return], features=(feature,)))
        buy = BreakoutReturnModule(reference_mode="feature", return_confirmation="close")
        buy.on_bar(view([buy_break], features=(buy_feature,)))
        buy_decision = buy.on_bar(view([buy_break, buy_return], features=(buy_feature,)))
        assert sell_decision.signals[0].side is Side.SELL
        assert buy_decision.signals[0].side is Side.BUY
        assert sell_decision.signals[0].metadata["return_depth"] == pytest.approx(
            buy_decision.signals[0].metadata["return_depth"], abs=1e-10
        )


def test_hash_seed_does_not_change_deterministic_public_result(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "hash_seed_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            from datetime import datetime, timedelta, timezone
            from modular_core import BacktestEngine, Candle
            from strategy_modules.breakout_return import BreakoutReturnModule
            start=datetime(2026,1,1,tzinfo=timezone.utc)
            rows=[(95,100,90,96),(96,99,91,95),(95,98,92,94),(101,104,100.5,103),(99.5,101,98.5,99)]
            bars=[Candle(start+timedelta(minutes=5*i),*row,1,'BTCUSDT','5m') for i,row in enumerate(rows)]
            result=BacktestEngine(BreakoutReturnModule(lookback_bars=3)).run(bars)
            signal=result.journal.signals[0]['signal']
            print(json.dumps({'run_id':result.run_manifest.run_id,'setup_id':signal.metadata['setup_id'],'metadata':dict(signal.metadata)},sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    outputs = []
    for seed_value in ("0", "1", "42", "random"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed_value
        completed = subprocess.run(
            [sys.executable, str(script)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        outputs.append(completed.stdout.strip())
    assert len(set(outputs)) == 1
