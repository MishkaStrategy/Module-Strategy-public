from __future__ import annotations

import json
from copy import deepcopy
from datetime import timezone
from decimal import Decimal

import pytest
from modular_core import Feature, Side

from strategy_modules.breakout_return import (
    BreakoutDirection,
    BreakoutReturnDataError,
    BreakoutReturnModule,
    BreakoutReturnStateError,
    ReferenceRange,
)
from strategy_modules.breakout_return.models import ActiveBreakout


def _feed(module, bars, make_view):
    decisions = []
    visible = []
    for current in bars:
        visible.append(current)
        decisions.append(module.on_bar(make_view(visible)))
    return decisions


def _seed(make_bar):
    return [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reference_feature_timeframe": "15m"},
        {"reference_feature_schema_version": "2"},
        {"max_reference_age_seconds": 300},
    ],
)
def test_feature_only_options_require_feature_mode(kwargs):
    with pytest.raises(ValueError, match="feature-only parameters"):
        BreakoutReturnModule(**kwargs)


def test_high_precision_decimal_is_rejected_before_parameter_identity_loss():
    with pytest.raises(ValueError, match="represented exactly"):
        BreakoutReturnModule(min_return_depth=Decimal("0.123456789012345678901"))


def test_feature_range_identifier_must_be_trimmed(make_bar, make_view):
    module = BreakoutReturnModule(reference_mode="feature")
    breakout = make_bar(1, 101, 104, 100.5, 103)
    feature = Feature(
        key="range.current",
        source_module="range_builder",
        created_at=make_bar(0, 95, 100, 90, 95).close_time,
        value={"high": 100, "low": 90, "range_id": " range-1 "},
        symbol="BTCUSDT",
        timeframe="5m",
    )
    with pytest.raises(BreakoutReturnDataError, match="trimmed"):
        module.on_bar(make_view([breakout], features=(feature,)))


def test_reference_and_active_domain_objects_reject_ambiguous_values():
    now = _seed_time()
    with pytest.raises(ValueError, match="must be Decimal"):
        ReferenceRange(100.0, 90.0, "r", "source", now)  # type: ignore[arg-type]
    reference = ReferenceRange(
        Decimal("100"),
        Decimal("90"),
        "r",
        "source",
        now,
    )
    with pytest.raises(ValueError, match="signal_side contradicts"):
        ActiveBreakout(
            setup_id="setup",
            direction=BreakoutDirection.ABOVE,
            signal_side=Side.BUY,
            reference=reference,
            breakout_time=now,
            breakout_sequence=1,
            extreme=Decimal("101"),
            symbol="BTCUSDT",
            timeframe="5m",
        )


def _seed_time():
    from datetime import datetime

    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_checkpoint_decimal_representation_is_numeric_representation_invariant(
    make_bar,
    make_view,
):
    integer_module = BreakoutReturnModule(lookback_bars=3)
    float_module = BreakoutReturnModule(lookback_bars=3)
    integer_bars = _seed(make_bar)
    float_bars = [
        make_bar(0, 95.0, 100.0, 90.0, 96.0),
        make_bar(1, 96.0, 99.0, 91.0, 95.0),
        make_bar(2, 95.0, 98.0, 92.0, 94.0),
    ]
    _feed(integer_module, integer_bars, make_view)
    _feed(float_module, float_bars, make_view)
    assert integer_module.snapshot_state() == float_module.snapshot_state()
    assert integer_module.snapshot_state()["maxima"][0][1] == "100"


def test_active_diagnostics_use_current_processed_sequence(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = _seed(make_bar) + [
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 103, 108, 102, 104),
    ]
    decision = _feed(module, bars, make_view)[-1]
    assert decision.diagnostics["sequence"] == len(bars)


def test_restore_rejects_stream_binding_contradictions_atomically(make_bar, make_view):
    empty = BreakoutReturnModule(lookback_bars=3)
    state = deepcopy(dict(empty.snapshot_state()))
    state["bound_symbol"] = "BTCUSDT"
    state["bound_timeframe"] = "5m"
    before = empty.snapshot_state()
    with pytest.raises(BreakoutReturnStateError, match="empty state"):
        empty.restore_state(state)
    assert empty.snapshot_state() == before

    processed = BreakoutReturnModule(lookback_bars=3)
    _feed(processed, _seed(make_bar), make_view)
    state = deepcopy(dict(processed.snapshot_state()))
    state["bound_symbol"] = None
    state["bound_timeframe"] = None
    with pytest.raises(BreakoutReturnStateError, match="requires stream binding"):
        BreakoutReturnModule(lookback_bars=3).restore_state(state)


def test_restore_rejects_counter_lifecycle_mismatch_atomically(make_bar, make_view):
    source = BreakoutReturnModule(lookback_bars=3)
    _feed(
        source,
        _seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)],
        make_view,
    )
    state = json.loads(json.dumps(source.snapshot_state()))
    state["counters"]["confirmed"] = 1
    target = BreakoutReturnModule(lookback_bars=3)
    before = target.snapshot_state()
    with pytest.raises(BreakoutReturnStateError, match="lifecycle totals"):
        target.restore_state(state)
    assert target.snapshot_state() == before


def test_checkpoint_excludes_replay_observation_side_effects(make_bar, make_view):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = _seed(make_bar)
    _feed(module, bars, make_view)
    baseline = module.snapshot_state()
    module.on_bar(make_view(bars))
    assert module.diagnostic_counters["duplicate_events"] == 1
    assert module.snapshot_state() == baseline

    restored = BreakoutReturnModule(lookback_bars=3)
    restored.restore_state(json.loads(json.dumps(baseline)))
    assert restored.diagnostic_counters["restores"] == 1
    assert restored.snapshot_state() == baseline


def test_legacy_active_state_migrates_to_self_restorable_v2_checkpoint(
    make_bar,
    make_view,
):
    source = BreakoutReturnModule(lookback_bars=3)
    bars = _seed(make_bar) + [make_bar(3, 101, 104, 100.5, 103)]
    _feed(source, bars, make_view)

    legacy = json.loads(json.dumps(source.snapshot_state()))
    legacy["payload_version"] = 1
    legacy.pop("config_fingerprint")
    legacy.pop("counters")

    migrated = BreakoutReturnModule(lookback_bars=3)
    migrated.restore_state(legacy)
    checkpoint = json.loads(json.dumps(migrated.snapshot_state()))

    assert checkpoint["payload_version"] == 2
    assert checkpoint["active"] is not None
    assert checkpoint["counters"]["armed_above"] == 1

    restarted = BreakoutReturnModule(lookback_bars=3)
    restarted.restore_state(checkpoint)
    assert restarted.snapshot_state() == migrated.snapshot_state()


def test_public_domain_models_and_restore_fail_closed_on_wrong_container_types():
    now = _seed_time()
    with pytest.raises(ValueError, match="created_at must be datetime"):
        ReferenceRange(  # type: ignore[arg-type]
            Decimal("100"),
            Decimal("90"),
            "r",
            "source",
            "2026-01-01T00:00:00+00:00",
        )

    reference = ReferenceRange(
        Decimal("100"),
        Decimal("90"),
        "r",
        "source",
        now,
    )
    with pytest.raises(ValueError, match="breakout_time must be datetime"):
        ActiveBreakout(  # type: ignore[arg-type]
            setup_id="setup",
            direction=BreakoutDirection.ABOVE,
            signal_side=Side.SELL,
            reference=reference,
            breakout_time="2026-01-01T00:00:00+00:00",
            breakout_sequence=1,
            extreme=Decimal("101"),
            symbol="BTCUSDT",
            timeframe="5m",
        )

    with pytest.raises(BreakoutReturnStateError, match="state must be a mapping"):
        BreakoutReturnModule().restore_state([])  # type: ignore[arg-type]
