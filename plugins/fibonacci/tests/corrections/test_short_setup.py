from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
)


BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def wave(
    index: int,
    *,
    parent: str | None,
    phase: WorkflowPhase,
    correction_level: str = "0.5",
) -> FibonacciWaveSnapshot:
    start = BASE + timedelta(hours=4 * index)
    return FibonacciWaveSnapshot(
        wave_id=f"wave-{index}",
        sequence_number=index + 1,
        direction="bearish",
        phase=phase,
        start_time=start,
        start_price=Decimal("200") - index,
        end_time=start + timedelta(hours=4),
        end_price=Decimal("150") - index,
        endpoint_revision=1,
        correction=CorrectionTouch(
            touched_at=start + timedelta(hours=8),
            price=Decimal("175") - index,
            actual_ratio=Decimal(correction_level),
            reference_level=Decimal(correction_level),
            candle_index=index + 10,
        ),
        completion_target=(Decimal("-0.18") if phase is WorkflowPhase.COMPLETED else None),
        completion_time=(start + timedelta(hours=12) if phase is WorkflowPhase.COMPLETED else None),
        completion_price=(Decimal("140") - index if phase is WorkflowPhase.COMPLETED else None),
        parent_wave_id=parent,
        start_candle_index=index,
        end_candle_index=index + 1,
        phase_started_index=index + 2,
        endpoint_confirmed_at=start + timedelta(hours=8),
        endpoint_confirmation_index=index + 10,
    )


def workflow(
    completed: tuple[FibonacciWaveSnapshot, ...],
    *,
    invalidated: tuple[FibonacciWaveSnapshot, ...] = (),
    active: FibonacciWaveSnapshot | None = None,
    timeframe: str = "4h",
) -> FibonacciWorkflowSnapshot:
    events = []
    for item in completed:
        events.append(
            FibonacciWorkflowEvent(
                event_type=WorkflowEventType.WAVE_COMPLETED,
                occurred_at=item.completion_time or item.end_time,
                wave_id=item.wave_id,
                candle_index=item.sequence_number + 20,
            )
        )
    for item in invalidated:
        events.append(
            FibonacciWorkflowEvent(
                event_type=WorkflowEventType.WAVE_INVALIDATED,
                occurred_at=item.end_time + timedelta(hours=12),
                wave_id=item.wave_id,
                candle_index=item.sequence_number + 20,
            )
        )
    events.sort(key=lambda item: item.occurred_at)
    return FibonacciWorkflowSnapshot(
        symbol="SOLUSDT",
        market="binance-usdtm-futures",
        timeframe=timeframe,
        cutoff_time=BASE + timedelta(days=30),
        active_wave=active,
        completed_waves=completed,
        invalidated_waves=invalidated,
        expired_waves=(),
        events=tuple(events),
        workflow_version="sequential-real-time/v2-full-endpoint",
        config_hash="config",
        dataset_hash="data",
    )


def test_repeated_linked_bearish_reactions_qualify_and_arm_setup() -> None:
    completed = []
    parent = None
    for index in range(8):
        item = wave(
            index, parent=parent, phase=WorkflowPhase.COMPLETED, correction_level="0.5" if index % 2 == 0 else "0.618"
        )
        completed.append(item)
        parent = item.wave_id
    active = wave(8, parent=parent, phase=WorkflowPhase.AWAITING_EXTENSION)
    snapshot = workflow(tuple(completed), active=active)

    observations = bearish_reaction_observations(snapshot)
    assert len(observations) == 8
    assert len({item.sequence_root_id for item in observations}) == 1

    profile = build_bearish_reaction_profile(snapshot)
    assert profile.qualifies is True
    assert profile.success_rate == 1.0
    assert profile.current_success_streak == 8

    setup = active_bearish_continuation_setup(snapshot, profile)
    assert setup is not None
    assert setup.correction_level == Decimal("0.5")
    assert setup.timeframe == "4h"
    assert "execution_not_performed=true" in setup.evidence


def test_latest_unrelated_failed_sequence_resets_streak() -> None:
    first_sequence = []
    parent = None
    for index in range(8):
        item = wave(index, parent=parent, phase=WorkflowPhase.COMPLETED)
        first_sequence.append(item)
        parent = item.wave_id
    failed = wave(20, parent=None, phase=WorkflowPhase.INVALIDATED)
    snapshot = workflow(tuple(first_sequence), invalidated=(failed,))

    profile = build_bearish_reaction_profile(snapshot)
    assert profile.sample_size == 9
    assert profile.success_rate == 8 / 9
    assert profile.latest_sequence_id == failed.wave_id
    assert profile.current_success_streak == 0
    assert profile.qualifies is False


def test_short_profile_is_disabled_outside_4h_and_1d() -> None:
    completed = tuple(
        wave(index, parent=(f"wave-{index - 1}" if index else None), phase=WorkflowPhase.COMPLETED)
        for index in range(8)
    )
    snapshot = workflow(completed, timeframe="1h")
    assert bearish_reaction_observations(snapshot) == ()
    assert build_bearish_reaction_profile(snapshot).qualifies is False
