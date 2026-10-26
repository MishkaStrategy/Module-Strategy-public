from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_strategy_fibonacci.contracts import (
    CorrectionTouch,
    FibonacciWaveSnapshot,
    FibonacciWorkflowEvent,
    FibonacciWorkflowSnapshot,
    WorkflowEventType,
    WorkflowPhase,
)
from modular_strategy_fibonacci.corrections.short_setup import (
    active_bearish_continuation_setup,
    bearish_reaction_observations,
    build_bearish_reaction_profile,
    build_bearish_reaction_regime_profile,
)

BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _wave(
    index: int,
    *,
    parent: str | None,
    success: bool | None,
    level: str = "0.5",
    symbol_seed: int = 0,
) -> FibonacciWaveSnapshot:
    start = BASE + timedelta(hours=4 * (index + symbol_seed))
    if success is True:
        phase = WorkflowPhase.COMPLETED
    elif success is False:
        phase = WorkflowPhase.INVALIDATED
    else:
        phase = WorkflowPhase.AWAITING_EXTENSION
    return FibonacciWaveSnapshot(
        wave_id=f"wave-{symbol_seed}-{index}",
        sequence_number=index + 1,
        direction="bearish",
        phase=phase,
        start_time=start,
        start_price=Decimal("250") - index,
        end_time=start + timedelta(hours=4),
        end_price=Decimal("200") - index,
        endpoint_revision=2,
        correction=CorrectionTouch(
            touched_at=start + timedelta(hours=8),
            price=Decimal("225") - index,
            actual_ratio=Decimal(level),
            reference_level=Decimal(level),
            candle_index=index + 20,
        ),
        completion_target=Decimal("-0.18") if success is True else None,
        completion_time=start + timedelta(hours=12) if success is True else None,
        completion_price=Decimal("190") - index if success is True else None,
        parent_wave_id=parent,
        start_candle_index=index,
        end_candle_index=index + 1,
        phase_started_index=index + 2,
        endpoint_confirmed_at=start + timedelta(hours=8),
        endpoint_confirmation_index=index + 10,
    )


def _snapshot(
    outcomes: list[bool],
    *,
    levels: list[str] | None = None,
    active: bool = True,
    active_level: str = "0.5",
    symbol: str = "BTCUSDT",
    timeframe: str = "4h",
    break_before: int | None = None,
) -> FibonacciWorkflowSnapshot:
    completed = []
    invalidated = []
    events = []
    parent = None
    last_wave_id = None
    for index, success in enumerate(outcomes):
        if break_before is not None and index == break_before:
            parent = None
        level = (levels or ["0.5"] * len(outcomes))[index]
        item = _wave(index, parent=parent, success=success, level=level)
        (completed if success else invalidated).append(item)
        events.append(
            FibonacciWorkflowEvent(
                event_type=(WorkflowEventType.WAVE_COMPLETED if success else WorkflowEventType.WAVE_INVALIDATED),
                occurred_at=(item.completion_time or item.end_time + timedelta(hours=12)),
                wave_id=item.wave_id,
                candle_index=index + 50,
            )
        )
        parent = item.wave_id
        last_wave_id = item.wave_id
    active_wave = (
        _wave(
            len(outcomes),
            parent=last_wave_id,
            success=None,
            level=active_level,
        )
        if active
        else None
    )
    events.sort(key=lambda item: (item.occurred_at, item.wave_id))
    return FibonacciWorkflowSnapshot(
        symbol=symbol,
        market="binance-usdtm-futures",
        timeframe=timeframe,
        cutoff_time=BASE + timedelta(days=120),
        active_wave=active_wave,
        completed_waves=tuple(completed),
        invalidated_waves=tuple(invalidated),
        expired_waves=(),
        events=tuple(events),
        workflow_version="sequential-real-time/v2-full-endpoint",
        config_hash="config",
        dataset_hash="dataset",
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"minimum_samples": 0}, "positive"),
        ({"minimum_streak": 0}, "positive"),
        ({"lookback_observations": 0}, "positive"),
        ({"minimum_samples": 5, "lookback_observations": 4}, "minimum_samples"),
        ({"minimum_samples": 1, "minimum_streak": 5, "lookback_observations": 4}, "minimum_streak"),
        ({"minimum_success_rate": -0.1}, "success_rate"),
        ({"minimum_success_rate": 1.1}, "success_rate"),
    ],
)
def test_profile_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_bearish_reaction_profile(_snapshot([]), **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"window_size": 0}, "positive"),
        ({"minimum_window_samples": 0}, "positive"),
        ({"minimum_level_samples": 0}, "positive"),
        ({"window_size": 3, "minimum_window_samples": 4}, "cannot exceed"),
        ({"window_size": 3, "minimum_level_samples": 4}, "cannot exceed"),
        ({"minimum_success_rate": -0.1}, "minimum_success_rate"),
        ({"minimum_success_rate": 1.1}, "minimum_success_rate"),
        ({"minimum_level_success_rate": -0.1}, "minimum_level_success_rate"),
        ({"minimum_level_success_rate": 1.1}, "minimum_level_success_rate"),
        ({"change_threshold": -0.1}, "change_threshold"),
        ({"change_threshold": 1.1}, "change_threshold"),
    ],
)
def test_regime_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_bearish_reaction_regime_profile(_snapshot([]), **kwargs)


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([False] * 4 + [True] * 4, "improving"),
        ([True] * 4 + [False] * 4, "weakening"),
        ([True, False] * 4, "stable"),
        ([True] * 3, "insufficient_data"),
    ],
)
def test_regime_classification(outcomes: list[bool], expected: str) -> None:
    regime = build_bearish_reaction_regime_profile(
        _snapshot(outcomes),
        window_size=4,
        minimum_window_samples=4,
        minimum_success_rate=0.5,
        minimum_level_samples=1,
        minimum_level_success_rate=0.5,
        change_threshold=0.25,
    )
    assert regime.classification == expected


def test_level_statistics_are_independent() -> None:
    snapshot = _snapshot(
        [True, False, True, False],
        levels=["0.5", "0.618", "0.5", "0.618"],
    )
    regime = build_bearish_reaction_regime_profile(
        snapshot,
        window_size=4,
        minimum_window_samples=2,
        minimum_level_samples=1,
    )
    assert regime.recent_level_rates == (
        (Decimal("0.5"), 2, 2, 1.0),
        (Decimal("0.618"), 2, 0, 0.0),
    )


def test_contiguous_tail_does_not_jump_across_interleaved_root() -> None:
    snapshot = _snapshot([True] * 4 + [True] + [True] * 3, break_before=4)
    profile = build_bearish_reaction_profile(
        snapshot,
        minimum_samples=1,
        minimum_success_rate=0.0,
        minimum_streak=1,
    )
    assert profile.latest_sequence_sample_size == 4
    assert profile.current_success_streak == 4


def _qualified_setup_snapshot(active_level: str = "0.5") -> FibonacciWorkflowSnapshot:
    return _snapshot(
        [False] * 4 + [True] * 8,
        levels=["0.5", "0.618"] * 6,
        active_level=active_level,
    )


def _qualified_profiles(snapshot: FibonacciWorkflowSnapshot):
    profile = build_bearish_reaction_profile(
        snapshot,
        minimum_samples=8,
        minimum_success_rate=0.6,
        minimum_streak=3,
        lookback_observations=40,
    )
    regime = build_bearish_reaction_regime_profile(
        snapshot,
        window_size=8,
        minimum_window_samples=4,
        minimum_success_rate=0.6,
        minimum_level_samples=4,
        minimum_level_success_rate=0.6,
        change_threshold=0.15,
    )
    return profile, regime


def test_qualified_current_level_and_root_arm_setup() -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    setup = active_bearish_continuation_setup(snapshot, profile, regime)
    assert setup is not None
    assert setup.sequence_root_id == profile.latest_sequence_id
    assert setup.recent_level_sample_size == 4
    assert setup.recent_level_success_rate == 1.0
    assert setup.setup_id.startswith("fib-short-")
    assert "execution_not_performed=true" in setup.evidence


def test_weak_current_level_blocks_strong_aggregate() -> None:
    snapshot = _snapshot(
        [True] * 8,
        levels=["0.5"] * 6 + ["0.618"] * 2,
        active_level="0.618",
    )
    profile = build_bearish_reaction_profile(snapshot, minimum_samples=1, minimum_streak=1)
    regime = build_bearish_reaction_regime_profile(
        snapshot,
        window_size=8,
        minimum_window_samples=4,
        minimum_level_samples=4,
    )
    assert regime.currently_reliable is True
    assert active_bearish_continuation_setup(snapshot, profile, regime) is None


def test_active_root_must_match_contiguous_historical_root() -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    assert snapshot.active_wave is not None
    unrelated = replace(snapshot.active_wave, parent_wave_id=None, wave_id="unrelated")
    changed = replace(snapshot, active_wave=unrelated)
    assert active_bearish_continuation_setup(changed, profile, regime) is None


def test_profile_identity_mismatch_is_rejected() -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    with pytest.raises(ValueError, match="profile identity"):
        active_bearish_continuation_setup(
            snapshot,
            replace(profile, symbol="ETHUSDT"),
            regime,
        )


def test_regime_identity_mismatch_is_rejected() -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    with pytest.raises(ValueError, match="regime identity"):
        active_bearish_continuation_setup(
            snapshot,
            profile,
            replace(regime, timeframe="1d"),
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "endpoint_revision",
        "start_price",
        "end_price",
        "correction_price",
        "correction_ratio",
        "correction_level",
    ],
)
def test_setup_fingerprint_changes_with_wave_fact(field_name: str) -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    first = active_bearish_continuation_setup(snapshot, profile, regime)
    assert first is not None and snapshot.active_wave is not None
    wave = snapshot.active_wave
    if field_name == "endpoint_revision":
        changed_wave = replace(wave, endpoint_revision=wave.endpoint_revision + 1)
    elif field_name == "start_price":
        changed_wave = replace(wave, start_price=wave.start_price + Decimal("1"))
    elif field_name == "end_price":
        changed_wave = replace(wave, end_price=wave.end_price - Decimal("1"))
    else:
        assert wave.correction is not None
        if field_name == "correction_price":
            correction = replace(wave.correction, price=wave.correction.price + Decimal("1"))
        elif field_name == "correction_ratio":
            correction = replace(wave.correction, actual_ratio=Decimal("0.55"))
        else:
            correction = replace(
                wave.correction,
                reference_level=Decimal("0.618"),
                actual_ratio=Decimal("0.618"),
            )
        changed_wave = replace(wave, correction=correction)
    changed_snapshot = replace(snapshot, active_wave=changed_wave)
    changed = active_bearish_continuation_setup(changed_snapshot, profile, regime)
    assert changed is not None
    assert changed.setup_id != first.setup_id


@pytest.mark.parametrize(
    "profile_change",
    [
        {"minimum_samples": 7},
        {"minimum_success_rate": 0.55},
        {"minimum_streak": 2},
        {"lookback_observations": 39},
    ],
)
def test_setup_fingerprint_changes_with_profile_threshold(
    profile_change: dict[str, object],
) -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    first = active_bearish_continuation_setup(snapshot, profile, regime)
    changed = active_bearish_continuation_setup(
        snapshot,
        replace(profile, **profile_change),
        regime,
    )
    assert first is not None and changed is not None
    assert changed.setup_id != first.setup_id


@pytest.mark.parametrize(
    "regime_change",
    [
        {"minimum_window_samples": 3},
        {"minimum_success_rate": 0.55},
        {"minimum_level_samples": 3},
        {"minimum_level_success_rate": 0.55},
        {"change_threshold": 0.10},
        {"window_size": 7},
    ],
)
def test_setup_fingerprint_changes_with_regime_threshold(
    regime_change: dict[str, object],
) -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    first = active_bearish_continuation_setup(snapshot, profile, regime)
    changed = active_bearish_continuation_setup(
        snapshot,
        profile,
        replace(regime, **regime_change),
    )
    assert first is not None and changed is not None
    assert changed.setup_id != first.setup_id


def test_non_htf_has_no_observations_or_setup() -> None:
    snapshot = _snapshot([True] * 8, timeframe="1h")
    assert bearish_reaction_observations(snapshot) == ()
    profile = build_bearish_reaction_profile(snapshot, minimum_samples=1, minimum_streak=1)
    regime = build_bearish_reaction_regime_profile(
        snapshot,
        window_size=4,
        minimum_window_samples=1,
        minimum_level_samples=1,
    )
    assert profile.qualifies is False
    assert regime.currently_reliable is False
    assert active_bearish_continuation_setup(snapshot, profile, regime) is None


def test_no_active_wave_or_correction_returns_none() -> None:
    snapshot = _qualified_setup_snapshot()
    profile, regime = _qualified_profiles(snapshot)
    assert active_bearish_continuation_setup(replace(snapshot, active_wave=None), profile, regime) is None
    assert snapshot.active_wave is not None
    no_correction = replace(snapshot.active_wave, correction=None)
    assert active_bearish_continuation_setup(replace(snapshot, active_wave=no_correction), profile, regime) is None
