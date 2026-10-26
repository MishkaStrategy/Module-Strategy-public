from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from modular_strategy_fibonacci.config import FibonacciConfig
from modular_strategy_fibonacci.contracts import Candle, WorkflowEventType, WorkflowPhase
from modular_strategy_fibonacci.workflow.state_machine import SequentialFibonacciEngine


def candle(
    index: int,
    *,
    open_price: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    open_time = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index)
    return Candle(
        open_time=open_time,
        close_time=open_time + timedelta(minutes=15),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


def engine() -> SequentialFibonacciEngine:
    return SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(min_atr_multiple=Decimal("0.5")),
    )


def seed_bullish(
    workflow: SequentialFibonacciEngine,
    *,
    start: str = "100",
    end: str = "200",
    phase: WorkflowPhase = WorkflowPhase.AWAITING_CORRECTION,
) -> None:
    workflow.seed_wave(
        direction="bullish",
        start_time=datetime(2024, 12, 31, 22, tzinfo=timezone.utc),
        start_price=Decimal(start),
        end_time=datetime(2024, 12, 31, 23, tzinfo=timezone.utc),
        end_price=Decimal(end),
        phase=phase,
    )


def test_bullish_correction_target_and_chained_wave() -> None:
    workflow = engine()
    seed_bullish(workflow)

    after_correction = workflow.process(candle(0, open_price="190", high="195", low="150", close="160"))
    assert after_correction.active_wave is not None
    assert after_correction.active_wave.phase == WorkflowPhase.AWAITING_EXTENSION
    assert after_correction.active_wave.correction is not None
    assert after_correction.active_wave.correction.reference_level == Decimal("0.5")

    after_target = workflow.process(candle(1, open_price="160", high="218", low="155", close="215"))
    assert len(after_target.completed_waves) == 1
    completed = after_target.completed_waves[0]
    assert completed.phase == WorkflowPhase.COMPLETED
    assert completed.completion_target == Decimal("-0.18")
    assert completed.completion_price == Decimal("218")

    active = after_target.active_wave
    assert active is not None
    assert active.sequence_number == 2
    assert active.start_price == Decimal("150")
    assert active.end_price == Decimal("218")
    assert active.parent_wave_id == completed.wave_id
    assert active.phase == WorkflowPhase.AWAITING_CORRECTION


def test_deeper_correction_is_classified_at_0786() -> None:
    workflow = engine()
    seed_bullish(workflow)

    snapshot = workflow.process(candle(0, open_price="190", high="195", low="121.4", close="130"))
    assert snapshot.active_wave is not None
    assert snapshot.active_wave.correction is not None
    assert snapshot.active_wave.correction.actual_ratio == Decimal("0.786")
    assert snapshot.active_wave.correction.reference_level == Decimal("0.786")


def test_correction_slightly_short_of_05_is_accepted() -> None:
    workflow = engine()
    seed_bullish(workflow)

    snapshot = workflow.process(candle(0, open_price="190", high="195", low="152", close="160"))
    assert snapshot.active_wave is not None
    assert snapshot.active_wave.correction is not None
    assert snapshot.active_wave.correction.actual_ratio == Decimal("0.48")
    assert snapshot.active_wave.correction.reference_level == Decimal("0.5")


def test_level_one_break_invalidates_wave() -> None:
    workflow = engine()
    seed_bullish(workflow)

    snapshot = workflow.process(candle(0, open_price="150", high="160", low="99", close="105"))
    assert snapshot.active_wave is None
    assert len(snapshot.invalidated_waves) == 1
    assert snapshot.invalidated_waves[0].phase == WorkflowPhase.INVALIDATED
    assert snapshot.events[-1].event_type == WorkflowEventType.WAVE_INVALIDATED


def test_no_correction_deep_extension_reanchors_endpoint() -> None:
    workflow = engine()
    seed_bullish(
        workflow,
        start="150",
        end="218",
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )

    extending = workflow.process(candle(0, open_price="220", high="253", low="218", close="250"))
    assert extending.active_wave is not None
    assert extending.active_wave.phase == WorkflowPhase.EXTENDING_WITHOUT_CORRECTION
    assert extending.active_wave.end_price == Decimal("253")
    assert extending.active_wave.endpoint_revision == 1
    assert any(event.event_type == WorkflowEventType.EXTENSION_REANCHOR_STARTED for event in extending.events)

    extended_again = workflow.process(candle(1, open_price="250", high="260", low="245", close="258"))
    assert extended_again.active_wave is not None
    assert extended_again.active_wave.end_price == Decimal("260")
    assert extended_again.active_wave.endpoint_revision == 2

    corrected = workflow.process(candle(2, open_price="255", high="258", low="205", close="210"))
    assert corrected.active_wave is not None
    assert corrected.active_wave.phase == WorkflowPhase.AWAITING_EXTENSION
    assert corrected.active_wave.correction is not None
    assert corrected.active_wave.correction.reference_level == Decimal("0.5")


def test_bearish_logic_is_mirrored() -> None:
    workflow = SequentialFibonacciEngine(
        symbol="ETHUSDT",
        timeframe="1h",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(min_atr_multiple=Decimal("0.5")),
    )
    workflow.seed_wave(
        direction="bearish",
        start_time=datetime(2024, 12, 31, 20, tzinfo=timezone.utc),
        start_price=Decimal("200"),
        end_time=datetime(2024, 12, 31, 21, tzinfo=timezone.utc),
        end_price=Decimal("100"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )

    correction = workflow.process(candle(0, open_price="110", high="150", low="105", close="145"))
    assert correction.active_wave is not None
    assert correction.active_wave.correction is not None
    assert correction.active_wave.correction.reference_level == Decimal("0.5")

    target = workflow.process(candle(1, open_price="145", high="148", low="82", close="85"))
    assert target.completed_waves[0].completion_target == Decimal("-0.18")
    assert target.active_wave is not None
    assert target.active_wave.start_price == Decimal("150")
    assert target.active_wave.end_price == Decimal("82")
    assert target.active_wave.direction == "bearish"


def test_future_candles_cannot_mutate_earlier_snapshot() -> None:
    workflow = engine()
    seed_bullish(workflow)
    earlier = workflow.process(candle(0, open_price="190", high="195", low="150", close="160"))
    earlier_events = earlier.events
    earlier_wave = earlier.active_wave

    workflow.process(candle(1, open_price="160", high="218", low="155", close="215"))
    workflow.process(candle(2, open_price="215", high="220", low="149", close="151"))

    assert earlier.events == earlier_events
    assert earlier.active_wave == earlier_wave
    assert earlier.cutoff_time == candle(0, open_price="190", high="195", low="150", close="160").close_time


def test_stale_timeframe_wave_expires_instead_of_spanning_years() -> None:
    workflow = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(
            min_atr_multiple=Decimal("0.5"),
            max_wait_correction_bars=2,
        ),
    )
    seed_bullish(
        workflow,
        start="100",
        end="200",
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )
    workflow.process(candle(0, open_price="201", high="205", low="190", close="202"))
    workflow.process(candle(1, open_price="202", high="206", low="191", close="203"))
    workflow.process(candle(2, open_price="203", high="207", low="192", close="204"))
    expired = workflow.process(candle(3, open_price="204", high="208", low="193", close="205"))
    assert expired.active_wave is None
    assert len(expired.expired_waves) == 1
    assert expired.expired_waves[0].phase == WorkflowPhase.EXPIRED
    assert expired.events[-1].event_type == WorkflowEventType.WAVE_EXPIRED


def test_initial_pivot_is_not_available_before_right_side_confirmation() -> None:
    workflow = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(
            pivot_left=2,
            pivot_right=2,
            min_atr_multiple=Decimal("0.5"),
            max_building_bars=20,
        ),
    )
    bars = (
        candle(0, open_price="105", high="106", low="104", close="105"),
        candle(1, open_price="105", high="105", low="101", close="102"),
        candle(2, open_price="102", high="103", low="95", close="96"),
        candle(3, open_price="96", high="102", low="96", close="101"),
        candle(4, open_price="101", high="110", low="100", close="109"),
    )
    for bar in bars[:4]:
        snapshot = workflow.process(bar)
    assert snapshot.active_wave is None

    confirmed = workflow.process(bars[4])
    assert confirmed.active_wave is not None
    assert confirmed.active_wave.direction == "bullish"
    assert confirmed.active_wave.start_price == Decimal("95")
    assert confirmed.active_wave.start_candle_index == 2


def test_chained_bullish_wave_tracks_full_endpoint_until_correction_confirms_it() -> None:
    workflow = engine()
    seed_bullish(workflow)

    workflow.process(candle(0, open_price="190", high="195", low="150", close="160"))
    at_first_target = workflow.process(candle(1, open_price="160", high="218", low="155", close="215"))
    assert at_first_target.active_wave is not None
    assert at_first_target.active_wave.start_price == Decimal("150")
    assert at_first_target.active_wave.end_price == Decimal("218")
    assert at_first_target.active_wave.endpoint_confirmed_at is None

    continued = workflow.process(candle(2, open_price="215", high="225", low="214", close="223"))
    assert continued.active_wave is not None
    assert continued.active_wave.end_price == Decimal("225")
    assert continued.active_wave.end_candle_index == 2
    assert continued.active_wave.endpoint_confirmed_at is None

    final_high = workflow.process(candle(3, open_price="223", high="240", low="220", close="238"))
    assert final_high.active_wave is not None
    assert final_high.active_wave.end_price == Decimal("240")
    assert final_high.active_wave.end_candle_index == 3
    assert final_high.active_wave.endpoint_confirmed_at is None

    confirmed = workflow.process(candle(4, open_price="238", high="239", low="195", close="205"))
    assert confirmed.active_wave is not None
    assert confirmed.active_wave.end_price == Decimal("240")
    assert confirmed.active_wave.end_candle_index == 3
    assert confirmed.active_wave.endpoint_confirmation_index == 4
    assert (
        confirmed.active_wave.endpoint_confirmed_at
        == candle(4, open_price="238", high="239", low="195", close="205").close_time
    )
    assert confirmed.active_wave.phase == WorkflowPhase.AWAITING_EXTENSION
    assert confirmed.active_wave.correction is not None
    assert confirmed.active_wave.correction.reference_level == Decimal("0.5")
    assert any(event.event_type == WorkflowEventType.ENDPOINT_CONFIRMED for event in confirmed.events)


def test_chained_bearish_wave_tracks_full_endpoint_until_correction_confirms_it() -> None:
    workflow = SequentialFibonacciEngine(
        symbol="ETHUSDT",
        timeframe="4h",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(min_atr_multiple=Decimal("0.5")),
    )
    workflow.seed_wave(
        direction="bearish",
        start_time=datetime(2024, 12, 31, 20, tzinfo=timezone.utc),
        start_price=Decimal("200"),
        end_time=datetime(2024, 12, 31, 21, tzinfo=timezone.utc),
        end_price=Decimal("100"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )

    workflow.process(candle(0, open_price="110", high="150", low="105", close="145"))
    first_target = workflow.process(candle(1, open_price="145", high="148", low="82", close="85"))
    assert first_target.active_wave is not None
    assert first_target.active_wave.start_price == Decimal("150")
    assert first_target.active_wave.end_price == Decimal("82")

    workflow.process(candle(2, open_price="85", high="88", low="75", close="77"))
    final_low = workflow.process(candle(3, open_price="77", high="79", low="60", close="62"))
    assert final_low.active_wave is not None
    assert final_low.active_wave.end_price == Decimal("60")
    assert final_low.active_wave.end_candle_index == 3
    assert final_low.active_wave.endpoint_confirmed_at is None

    confirmed = workflow.process(candle(4, open_price="62", high="105", low="61", close="100"))
    assert confirmed.active_wave is not None
    assert confirmed.active_wave.end_price == Decimal("60")
    assert confirmed.active_wave.endpoint_confirmation_index == 4
    assert confirmed.active_wave.phase == WorkflowPhase.AWAITING_EXTENSION
    assert confirmed.active_wave.correction is not None
    assert confirmed.active_wave.correction.reference_level == Decimal("0.5")


def test_same_candle_new_extreme_does_not_prematurely_confirm_endpoint() -> None:
    workflow = engine()
    seed_bullish(
        workflow,
        start="150",
        end="218",
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )

    ambiguous = workflow.process(candle(0, open_price="218", high="230", low="180", close="220"))
    assert ambiguous.active_wave is not None
    assert ambiguous.active_wave.end_price == Decimal("230")
    assert ambiguous.active_wave.correction is None
    assert ambiguous.active_wave.endpoint_confirmed_at is None
    assert ambiguous.active_wave.phase == WorkflowPhase.AWAITING_CORRECTION


def test_same_candle_correction_deepening_and_target_does_not_chain() -> None:
    workflow = engine()
    seed_bullish(workflow)
    workflow.process(candle(0, open_price="190", high="195", low="150", close="160"))

    ambiguous = workflow.process(candle(1, open_price="160", high="220", low="120", close="180"))
    assert ambiguous.completed_waves == ()
    assert ambiguous.active_wave is not None
    assert ambiguous.active_wave.phase == WorkflowPhase.AWAITING_EXTENSION
    assert ambiguous.active_wave.correction is not None
    assert ambiguous.active_wave.correction.candle_index == 1

    confirmed_later = workflow.process(candle(2, open_price="180", high="220", low="155", close="215"))
    assert len(confirmed_later.completed_waves) == 1
    assert confirmed_later.active_wave is not None
    assert confirmed_later.active_wave.start_candle_index == 1
    assert confirmed_later.active_wave.end_candle_index == 2
