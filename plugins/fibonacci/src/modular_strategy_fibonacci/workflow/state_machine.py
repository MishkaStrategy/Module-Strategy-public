"""Sequential real-time Fibonacci impulse workflow.

The engine processes completed candles strictly from left to right. It never
consults candles to the right of the current cutoff. Its state represents the
user's workflow:

1. build an impulse from a confirmed swing;
2. wait for a correction around 0.5 or deeper configured levels;
3. wait for continuation to -0.18 / -0.27;
4. chain the next wave from the correction extreme to the target extreme;
5. when no correction arrives and price expands toward -0.5 / -0.618,
   re-anchor the endpoint to the extended extreme;
6. invalidate the wave only when level 1 (the start anchor) is broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence, cast

from ..candidates.features import average_true_range
from ..config import FibonacciConfig
from ..contracts import (
    Candle,
    CorrectionTouch,
    Direction,
    FibonacciWaveSnapshot,
    FibonacciWorkflowEvent,
    FibonacciWorkflowSnapshot,
    WorkflowEventType,
    WorkflowPhase,
)
from ..corrections.profile import classify_correction_depth
from ..pivots.detector import Pivot, PivotKind


STATE_SCHEMA_VERSION = "1"


@dataclass(slots=True)
class _MutableWave:
    wave_id: str
    sequence_number: int
    direction: Direction
    phase: WorkflowPhase
    start_time: datetime
    start_price: Decimal
    end_time: datetime
    end_price: Decimal
    endpoint_revision: int = 0
    correction: CorrectionTouch | None = None
    completion_target: Decimal | None = None
    completion_time: datetime | None = None
    completion_price: Decimal | None = None
    parent_wave_id: str | None = None
    start_candle_index: int = -1
    end_candle_index: int = -1
    phase_started_index: int = -1
    endpoint_confirmed_at: datetime | None = None
    endpoint_confirmation_index: int | None = None

    def snapshot(self) -> FibonacciWaveSnapshot:
        return FibonacciWaveSnapshot(
            wave_id=self.wave_id,
            sequence_number=self.sequence_number,
            direction=self.direction,
            phase=self.phase,
            start_time=self.start_time,
            start_price=self.start_price,
            end_time=self.end_time,
            end_price=self.end_price,
            endpoint_revision=self.endpoint_revision,
            correction=self.correction,
            completion_target=self.completion_target,
            completion_time=self.completion_time,
            completion_price=self.completion_price,
            parent_wave_id=self.parent_wave_id,
            start_candle_index=self.start_candle_index,
            end_candle_index=self.end_candle_index,
            phase_started_index=self.phase_started_index,
            endpoint_confirmed_at=self.endpoint_confirmed_at,
            endpoint_confirmation_index=self.endpoint_confirmation_index,
        )


def _wave_id(
    symbol: str,
    timeframe: str,
    sequence_number: int,
    start_time: datetime,
    start_price: Decimal,
    parent_wave_id: str | None,
) -> str:
    payload = (
        f"{symbol}|{timeframe}|{sequence_number}|{start_time.isoformat()}|{start_price}|{parent_wave_id or '-'}"
    ).encode()
    return "fib-wave-" + hashlib.sha256(payload).hexdigest()[:20]


def _ratio_for_price(
    start: Decimal,
    end: Decimal,
    price: Decimal,
) -> Decimal:
    distance = start - end
    if distance == 0:
        raise ValueError("wave anchors cannot be equal")
    return (price - end) / distance


def _level_price(start: Decimal, end: Decimal, ratio: Decimal) -> Decimal:
    return end + ratio * (start - end)


class SequentialFibonacciEngine:
    """Mutable streaming engine with immutable snapshots."""

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        dataset_hash: str,
        config: FibonacciConfig | None = None,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.dataset_hash = dataset_hash
        self.config = config or FibonacciConfig()
        self._candles: list[Candle] = []
        self._active: _MutableWave | None = None
        self._completed: list[FibonacciWaveSnapshot] = []
        self._invalidated: list[FibonacciWaveSnapshot] = []
        self._expired: list[FibonacciWaveSnapshot] = []
        self._events: list[FibonacciWorkflowEvent] = []
        self._last_confirmed_pivot_index: int | None = None
        self._latest_low: Pivot | None = None
        self._latest_high: Pivot | None = None
        self._sequence_number = 0

    @property
    def active_wave(self) -> FibonacciWaveSnapshot | None:
        return self._active.snapshot() if self._active else None

    @property
    def last_processed_open_time(self) -> datetime | None:
        return self._candles[-1].open_time if self._candles else None

    def seed_wave(
        self,
        *,
        direction: Direction,
        start_time: datetime,
        start_price: Decimal,
        end_time: datetime,
        end_price: Decimal,
        phase: WorkflowPhase = WorkflowPhase.BUILDING_IMPULSE,
    ) -> FibonacciWaveSnapshot:
        """Seed a known real-time wave without consulting future candles.

        This boundary is useful when another Core component supplies an already
        confirmed swing or when deterministic tests start at a known cutoff.
        """

        if self._active is not None or self._completed or self._invalidated or self._expired:
            raise ValueError("engine already contains workflow state")
        if end_time < start_time:
            raise ValueError("end_time cannot precede start_time")
        if direction == "bullish" and end_price <= start_price:
            raise ValueError("bullish seed must end above start")
        if direction == "bearish" and end_price >= start_price:
            raise ValueError("bearish seed must end below start")
        self._sequence_number = 1
        self._active = _MutableWave(
            wave_id=_wave_id(
                self.symbol,
                self.timeframe,
                self._sequence_number,
                start_time,
                start_price,
                None,
            ),
            sequence_number=self._sequence_number,
            direction=direction,
            phase=phase,
            start_time=start_time,
            start_price=start_price,
            end_time=end_time,
            end_price=end_price,
            start_candle_index=-1,
            end_candle_index=-1,
            phase_started_index=0,
        )
        return self._active.snapshot()

    def _emit(
        self,
        event_type: WorkflowEventType,
        candle: Candle,
        candle_index: int,
        *details: str,
    ) -> None:
        if self._active is None:
            return
        self._events.append(
            FibonacciWorkflowEvent(
                event_type=event_type,
                occurred_at=candle.close_time,
                wave_id=self._active.wave_id,
                candle_index=candle_index,
                details=tuple(details),
            )
        )

    def _confirm_new_pivots(self) -> None:
        right = self.config.pivot_right
        if len(self._candles) < self.config.pivot_left + right + 1:
            return
        candidate_index = len(self._candles) - 1 - right
        if self._last_confirmed_pivot_index == candidate_index:
            return
        left = self.config.pivot_left
        neighborhood = self._candles[candidate_index - left : candidate_index + right + 1]
        candle = self._candles[candidate_index]
        highs = [item.high for item in neighborhood]
        lows = [item.low for item in neighborhood]
        new_pivots: list[Pivot] = []
        highest = max(highs)
        lowest = min(lows)
        if candle.high == highest and highs.index(highest) == left:
            distinct_highs = [value for value in highs if value < highest]
            prominence = highest - max(distinct_highs) if distinct_highs else Decimal("0")
            new_pivots.append(Pivot(candidate_index, "high", candle.high, prominence))
        if candle.low == lowest and lows.index(lowest) == left:
            distinct_lows = [value for value in lows if value > lowest]
            prominence = min(distinct_lows) - lowest if distinct_lows else Decimal("0")
            new_pivots.append(Pivot(candidate_index, "low", candle.low, prominence))
        for pivot in new_pivots:
            if pivot.kind == "low":
                self._latest_low = pivot
            else:
                self._latest_high = pivot
        self._last_confirmed_pivot_index = candidate_index

    def _start_initial_wave_if_ready(self, candle: Candle, candle_index: int) -> None:
        if self._active is not None:
            return
        # ATR only needs the latest period plus one previous close. Limiting the
        # slice keeps long historical replays linear rather than quadratic.
        atr = average_true_range(tuple(self._candles[-15:]))
        if atr <= 0:
            return
        possibilities: list[tuple[Decimal, Direction, Pivot, Decimal]] = []
        if (
            self._latest_low is not None
            and self._latest_low.index < candle_index
            and candle_index - self._latest_low.index <= self.config.max_building_bars
        ):
            move = candle.high - self._latest_low.price
            if move > 0:
                possibilities.append((move / atr, "bullish", self._latest_low, candle.high))
        if (
            self._latest_high is not None
            and self._latest_high.index < candle_index
            and candle_index - self._latest_high.index <= self.config.max_building_bars
        ):
            move = self._latest_high.price - candle.low
            if move > 0:
                possibilities.append((move / atr, "bearish", self._latest_high, candle.low))
        eligible = [item for item in possibilities if item[0] >= self.config.min_atr_multiple]
        if not eligible:
            return
        _, direction, pivot, end_price = max(
            eligible,
            key=lambda item: (item[0], item[2].index, item[1]),
        )
        self._sequence_number += 1
        self._active = _MutableWave(
            wave_id=_wave_id(
                self.symbol,
                self.timeframe,
                self._sequence_number,
                self._candles[pivot.index].open_time,
                pivot.price,
                None,
            ),
            sequence_number=self._sequence_number,
            direction=direction,
            phase=WorkflowPhase.BUILDING_IMPULSE,
            start_time=self._candles[pivot.index].open_time,
            start_price=pivot.price,
            end_time=candle.close_time,
            end_price=end_price,
            start_candle_index=pivot.index,
            end_candle_index=candle_index,
            phase_started_index=pivot.index,
        )
        self._emit(
            WorkflowEventType.WAVE_STARTED,
            candle,
            candle_index,
            f"direction={direction}",
            f"start_price={pivot.price}",
            f"end_price={end_price}",
        )

    def _is_invalidated(self, candle: Candle) -> bool:
        assert self._active is not None
        if self._active.direction == "bullish":
            return candle.low < self._active.start_price
        return candle.high > self._active.start_price

    def _favorable_extreme(self, candle: Candle) -> Decimal:
        assert self._active is not None
        return candle.high if self._active.direction == "bullish" else candle.low

    def _adverse_extreme(self, candle: Candle) -> Decimal:
        assert self._active is not None
        return candle.low if self._active.direction == "bullish" else candle.high

    def _is_new_favorable_extreme(self, price: Decimal) -> bool:
        assert self._active is not None
        if self._active.direction == "bullish":
            return price > self._active.end_price
        return price < self._active.end_price

    def _update_endpoint(
        self,
        candle: Candle,
        candle_index: int,
        price: Decimal,
        *,
        reason: str,
    ) -> None:
        assert self._active is not None
        self._active.end_price = price
        self._active.end_time = candle.close_time
        self._active.endpoint_revision += 1
        self._active.end_candle_index = candle_index
        self._emit(
            WorkflowEventType.ENDPOINT_UPDATED,
            candle,
            candle_index,
            f"reason={reason}",
            f"end_price={price}",
            f"revision={self._active.endpoint_revision}",
        )

    def _correction_touch(
        self,
        candle: Candle,
        candle_index: int,
    ) -> CorrectionTouch | None:
        assert self._active is not None
        adverse = self._adverse_extreme(candle)
        ratio = _ratio_for_price(
            self._active.start_price,
            self._active.end_price,
            adverse,
        )
        reference = classify_correction_depth(
            ratio,
            self.config.correction_levels,
            shallow_tolerance=self.config.shallow_correction_tolerance,
        )
        if reference is None:
            return None
        return CorrectionTouch(
            touched_at=candle.close_time,
            price=adverse,
            actual_ratio=ratio,
            reference_level=reference,
            candle_index=candle_index,
        )

    def _confirm_endpoint(
        self,
        candle: Candle,
        candle_index: int,
    ) -> None:
        """Confirm that the latest favorable extreme was the full impulse end.

        Confirmation is point-in-time: the endpoint itself remains on the candle
        that made the extreme, while a later completed correction candle confirms
        that no earlier anchor truncation is allowed.
        """

        assert self._active is not None
        if self._active.endpoint_confirmed_at is not None:
            return
        self._active.endpoint_confirmed_at = candle.close_time
        self._active.endpoint_confirmation_index = candle_index
        self._emit(
            WorkflowEventType.ENDPOINT_CONFIRMED,
            candle,
            candle_index,
            f"end_time={self._active.end_time.isoformat()}",
            f"end_price={self._active.end_price}",
            f"endpoint_candle_index={self._active.end_candle_index}",
        )

    def _update_correction(
        self,
        touch: CorrectionTouch,
        candle: Candle,
        candle_index: int,
    ) -> None:
        assert self._active is not None
        previous = self._active.correction
        if previous is None:
            self._confirm_endpoint(candle, candle_index)
            self._active.correction = touch
            self._active.phase = WorkflowPhase.AWAITING_EXTENSION
            self._active.phase_started_index = candle_index
            self._emit(
                WorkflowEventType.CORRECTION_TOUCHED,
                candle,
                candle_index,
                f"actual_ratio={touch.actual_ratio}",
                f"reference_level={touch.reference_level}",
                f"price={touch.price}",
            )
            return
        if touch.actual_ratio > previous.actual_ratio:
            self._active.correction = touch
            self._emit(
                WorkflowEventType.CORRECTION_DEEPENED,
                candle,
                candle_index,
                f"actual_ratio={touch.actual_ratio}",
                f"reference_level={touch.reference_level}",
                f"price={touch.price}",
            )

    def _deepest_target_reached(self, price: Decimal) -> Decimal | None:
        assert self._active is not None
        reached = [
            target
            for target in self.config.completion_targets
            if (
                price
                >= _level_price(
                    self._active.start_price,
                    self._active.end_price,
                    target,
                )
                if self._active.direction == "bullish"
                else price
                <= _level_price(
                    self._active.start_price,
                    self._active.end_price,
                    target,
                )
            )
        ]
        if not reached:
            return None
        return min(reached)

    def _reanchor_trigger_reached(self, price: Decimal) -> Decimal | None:
        assert self._active is not None
        reached = [
            level
            for level in self.config.extension_reanchor_levels
            if (
                price
                >= _level_price(
                    self._active.start_price,
                    self._active.end_price,
                    level,
                )
                if self._active.direction == "bullish"
                else price
                <= _level_price(
                    self._active.start_price,
                    self._active.end_price,
                    level,
                )
            )
        ]
        return min(reached) if reached else None

    def _invalidate(self, candle: Candle, candle_index: int) -> None:
        assert self._active is not None
        self._active.phase = WorkflowPhase.INVALIDATED
        self._emit(
            WorkflowEventType.WAVE_INVALIDATED,
            candle,
            candle_index,
            "reason=level_1_broken",
            f"start_price={self._active.start_price}",
        )
        self._invalidated.append(self._active.snapshot())
        self._active = None

    def _phase_limit(self) -> int:
        assert self._active is not None
        if self._active.phase == WorkflowPhase.BUILDING_IMPULSE:
            return self.config.max_building_bars
        if self._active.phase in {
            WorkflowPhase.AWAITING_CORRECTION,
            WorkflowPhase.EXTENDING_WITHOUT_CORRECTION,
        }:
            return self.config.max_wait_correction_bars
        return self.config.max_wait_extension_bars

    def _is_expired(self, candle_index: int) -> bool:
        assert self._active is not None
        start_index = self._active.phase_started_index
        if start_index < 0:
            return False
        return candle_index - start_index > self._phase_limit()

    def _expire(self, candle: Candle, candle_index: int) -> None:
        assert self._active is not None
        elapsed = candle_index - self._active.phase_started_index
        limit = self._phase_limit()
        self._active.phase = WorkflowPhase.EXPIRED
        self._emit(
            WorkflowEventType.WAVE_EXPIRED,
            candle,
            candle_index,
            f"elapsed_phase_bars={elapsed}",
            f"limit={limit}",
        )
        self._expired.append(self._active.snapshot())
        self._active = None

    def _complete_and_chain(
        self,
        candle: Candle,
        candle_index: int,
        target: Decimal,
        completion_price: Decimal,
    ) -> None:
        assert self._active is not None
        correction = self._active.correction
        assert correction is not None
        completed = self._active
        completed.completion_target = target
        completed.completion_time = candle.close_time
        completed.completion_price = completion_price
        completed.phase = WorkflowPhase.COMPLETED
        self._emit(
            WorkflowEventType.TARGET_REACHED,
            candle,
            candle_index,
            f"target={target}",
            f"completion_price={completion_price}",
        )
        self._emit(
            WorkflowEventType.WAVE_COMPLETED,
            candle,
            candle_index,
            f"correction_level={correction.reference_level}",
            f"correction_ratio={correction.actual_ratio}",
        )
        self._completed.append(completed.snapshot())

        self._sequence_number += 1
        next_start = correction
        next_wave_id = _wave_id(
            self.symbol,
            self.timeframe,
            self._sequence_number,
            next_start.touched_at,
            next_start.price,
            completed.wave_id,
        )
        self._active = _MutableWave(
            wave_id=next_wave_id,
            sequence_number=self._sequence_number,
            direction=completed.direction,
            phase=WorkflowPhase.AWAITING_CORRECTION,
            start_time=next_start.touched_at,
            start_price=next_start.price,
            end_time=candle.close_time,
            end_price=completion_price,
            parent_wave_id=completed.wave_id,
            start_candle_index=next_start.candle_index,
            end_candle_index=candle_index,
            phase_started_index=candle_index,
        )
        self._emit(
            WorkflowEventType.WAVE_STARTED,
            candle,
            candle_index,
            "reason=chained_from_completed_wave",
            f"start_price={next_start.price}",
            f"end_price={completion_price}",
        )

    def _process_candle(self, candle: Candle) -> None:
        if self._candles and candle.open_time <= self._candles[-1].open_time:
            raise ValueError("candles must be processed in strictly increasing order")
        self._candles.append(candle)
        candle_index = len(self._candles) - 1
        self._confirm_new_pivots()
        had_active_wave = self._active is not None
        self._start_initial_wave_if_ready(candle, candle_index)
        if self._active is None:
            return
        if not had_active_wave:
            # The same candle established the first endpoint. Its intrabar path
            # is unknown, so it cannot also confirm a correction.
            return

        if self._is_expired(candle_index):
            self._expire(candle, candle_index)
            return

        # A level-1 break always has priority over any same-candle target.
        if self._is_invalidated(candle):
            self._invalidate(candle, candle_index)
            return

        favorable = self._favorable_extreme(candle)

        if self._active.phase == WorkflowPhase.BUILDING_IMPULSE:
            if self._is_new_favorable_extreme(favorable):
                self._update_endpoint(
                    candle,
                    candle_index,
                    favorable,
                    reason="initial_impulse_building",
                )
                # Same-bar ordering is unknown; defer correction recognition.
                return
            touch = self._correction_touch(candle, candle_index)
            if touch is not None:
                self._update_correction(touch, candle, candle_index)
            return

        if self._active.phase == WorkflowPhase.AWAITING_CORRECTION:
            # Never freeze the endpoint at the first target touch. Until a real
            # correction confirms the turn, every new favorable extreme remains
            # part of the same impulse and Fibonacci must be stretched to it.
            if self._is_new_favorable_extreme(favorable):
                trigger = self._reanchor_trigger_reached(favorable)
                if trigger is not None:
                    self._active.phase = WorkflowPhase.EXTENDING_WITHOUT_CORRECTION
                    self._active.phase_started_index = candle_index
                    self._emit(
                        WorkflowEventType.EXTENSION_REANCHOR_STARTED,
                        candle,
                        candle_index,
                        f"trigger={trigger}",
                    )
                    reason = "deep_extension_without_correction"
                else:
                    reason = "impulse_continued_before_correction"
                self._update_endpoint(
                    candle,
                    candle_index,
                    favorable,
                    reason=reason,
                )
                # Same-candle path is unknown. A candle that makes a new extreme
                # cannot also confirm the correction in deterministic OHLC mode.
                return
            touch = self._correction_touch(candle, candle_index)
            if touch is not None:
                self._update_correction(touch, candle, candle_index)
            return

        if self._active.phase == WorkflowPhase.EXTENDING_WITHOUT_CORRECTION:
            if self._is_new_favorable_extreme(favorable):
                self._update_endpoint(
                    candle,
                    candle_index,
                    favorable,
                    reason="extended_impulse_new_extreme",
                )
                return
            touch = self._correction_touch(candle, candle_index)
            if touch is not None:
                self._update_correction(touch, candle, candle_index)
            return

        if self._active.phase == WorkflowPhase.AWAITING_EXTENSION:
            touch = self._correction_touch(candle, candle_index)
            if touch is not None:
                self._update_correction(touch, candle, candle_index)
            assert self._active.correction is not None
            if self._active.correction.candle_index == candle_index:
                # The latest correction extreme and a possible target are on the
                # same OHLC candle. Their intrabar order is unknowable, so do not
                # fabricate a zero-duration chained impulse.
                return
            target = self._deepest_target_reached(favorable)
            if target is not None:
                self._complete_and_chain(
                    candle,
                    candle_index,
                    target,
                    favorable,
                )
            return

    def snapshot_state(self) -> dict[str, object]:
        """Return an exact, JSON-serializable checkpoint.

        Cycle 3 deliberately stores full candle history. This is larger than a
        compact production checkpoint, but it preserves pivot confirmation and
        every global candle index exactly and therefore fails safe rather than
        silently shifting anchors after restart.
        """

        return {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "workflow_version": self.config.workflow_version,
            "config_hash": self.config.stable_hash(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "dataset_hash": self.dataset_hash,
            "sequence_number": self._sequence_number,
            "last_confirmed_pivot_index": self._last_confirmed_pivot_index,
            "latest_low": _pivot_to_state(self._latest_low),
            "latest_high": _pivot_to_state(self._latest_high),
            "candles": [_candle_to_state(item) for item in self._candles],
            "active_wave": (_wave_to_state(self._active.snapshot()) if self._active is not None else None),
            "completed_waves": [_wave_to_state(item) for item in self._completed],
            "invalidated_waves": [_wave_to_state(item) for item in self._invalidated],
            "expired_waves": [_wave_to_state(item) for item in self._expired],
            "events": [_event_to_state(item) for item in self._events],
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, object],
        *,
        config: FibonacciConfig | None = None,
    ) -> "SequentialFibonacciEngine":
        """Restore an engine atomically from a validated checkpoint mapping."""

        expected = {
            "state_schema_version",
            "workflow_version",
            "config_hash",
            "symbol",
            "timeframe",
            "dataset_hash",
            "sequence_number",
            "last_confirmed_pivot_index",
            "latest_low",
            "latest_high",
            "candles",
            "active_wave",
            "completed_waves",
            "invalidated_waves",
            "expired_waves",
            "events",
        }
        if set(state) != expected:
            missing = sorted(expected - set(state))
            unknown = sorted(set(state) - expected)
            raise ValueError(f"invalid checkpoint keys; missing={missing}, unknown={unknown}")
        if str(state["state_schema_version"]) != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported Fibonacci checkpoint schema")
        restored_config = config or FibonacciConfig()
        if str(state["workflow_version"]) != restored_config.workflow_version:
            raise ValueError("checkpoint workflow version mismatch")
        if str(state["config_hash"]) != restored_config.stable_hash():
            raise ValueError("checkpoint configuration mismatch")

        raw_candles = _require_sequence(state["candles"], "candles")
        raw_completed = _require_sequence(state["completed_waves"], "completed_waves")
        raw_invalidated = _require_sequence(state["invalidated_waves"], "invalidated_waves")
        raw_expired = _require_sequence(state["expired_waves"], "expired_waves")
        raw_events = _require_sequence(state["events"], "events")

        candles = [_candle_from_state(_require_mapping(row, "candle")) for row in raw_candles]
        if any(candles[index].open_time <= candles[index - 1].open_time for index in range(1, len(candles))):
            raise ValueError("checkpoint candles are not strictly increasing")
        completed = [_wave_from_state(_require_mapping(row, "completed wave")) for row in raw_completed]
        invalidated = [_wave_from_state(_require_mapping(row, "invalidated wave")) for row in raw_invalidated]
        expired = [_wave_from_state(_require_mapping(row, "expired wave")) for row in raw_expired]
        events = [_event_from_state(_require_mapping(row, "event")) for row in raw_events]
        raw_active = state["active_wave"]
        active_snapshot = None if raw_active is None else _wave_from_state(_require_mapping(raw_active, "active wave"))
        latest_low = _pivot_from_state(state["latest_low"])
        latest_high = _pivot_from_state(state["latest_high"])
        last_pivot = state["last_confirmed_pivot_index"]
        last_pivot_index = None if last_pivot is None else _require_int(last_pivot, "last_confirmed_pivot_index")
        if last_pivot_index is not None and not 0 <= last_pivot_index < len(candles):
            raise ValueError("last confirmed pivot index is outside candle history")
        for pivot in (latest_low, latest_high):
            if pivot is not None and not 0 <= pivot.index < len(candles):
                raise ValueError("checkpoint pivot index is outside candle history")

        sequence_number = _require_int(state["sequence_number"], "sequence_number")
        all_sequences = [
            item.sequence_number
            for item in (
                *completed,
                *invalidated,
                *expired,
                *((active_snapshot,) if active_snapshot is not None else ()),
            )
        ]
        if sequence_number < 0 or (all_sequences and sequence_number < max(all_sequences)):
            raise ValueError("checkpoint sequence number is inconsistent")

        engine = cls(
            symbol=str(state["symbol"]),
            timeframe=str(state["timeframe"]),
            dataset_hash=str(state["dataset_hash"]),
            config=restored_config,
        )
        engine._candles = candles
        engine._active = _mutable_from_snapshot(active_snapshot) if active_snapshot is not None else None
        engine._completed = completed
        engine._invalidated = invalidated
        engine._expired = expired
        engine._events = events
        engine._last_confirmed_pivot_index = last_pivot_index
        engine._latest_low = latest_low
        engine._latest_high = latest_high
        engine._sequence_number = sequence_number
        return engine

    def save_checkpoint(self, path: Path) -> Path:
        """Atomically write a UTF-8 JSON checkpoint."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        payload = json.dumps(
            self.snapshot_state(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
        temporary.replace(path)
        return path

    @classmethod
    def load_checkpoint(
        cls,
        path: Path,
        *,
        config: FibonacciConfig | None = None,
    ) -> "SequentialFibonacciEngine":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read Fibonacci checkpoint: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("Fibonacci checkpoint root must be a mapping")
        return cls.from_state(raw, config=config)

    def process(self, candle: Candle) -> FibonacciWorkflowSnapshot:
        """Process one completed candle and return the state at this cutoff."""

        self._process_candle(candle)
        return self.snapshot(candle.close_time)

    def snapshot(self, cutoff_time: datetime) -> FibonacciWorkflowSnapshot:
        return FibonacciWorkflowSnapshot(
            symbol=self.symbol,
            market="binance-usdtm-futures",
            timeframe=self.timeframe,
            cutoff_time=cutoff_time,
            active_wave=self._active.snapshot() if self._active else None,
            completed_waves=tuple(self._completed),
            invalidated_waves=tuple(self._invalidated),
            events=tuple(self._events),
            workflow_version=self.config.workflow_version,
            config_hash=self.config.stable_hash(),
            dataset_hash=self.dataset_hash,
            expired_waves=tuple(self._expired),
        )


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"checkpoint {name} must be a mapping")
    return value


def _require_sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"checkpoint {name} must be a sequence")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"checkpoint {name} must be an integer")
    return value


def _parse_utc(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"checkpoint {name} is not a valid datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"checkpoint {name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _candle_to_state(candle: Candle) -> dict[str, object]:
    return {
        "open_time": candle.open_time.isoformat(),
        "close_time": candle.close_time.isoformat(),
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }


def _candle_from_state(state: Mapping[str, object]) -> Candle:
    expected = {"open_time", "close_time", "open", "high", "low", "close", "volume"}
    if set(state) != expected:
        raise ValueError("checkpoint candle keys are invalid")
    return Candle(
        open_time=_parse_utc(state["open_time"], "candle.open_time"),
        close_time=_parse_utc(state["close_time"], "candle.close_time"),
        open=Decimal(str(state["open"])),
        high=Decimal(str(state["high"])),
        low=Decimal(str(state["low"])),
        close=Decimal(str(state["close"])),
        volume=Decimal(str(state["volume"])),
    )


def _touch_to_state(touch: CorrectionTouch | None) -> dict[str, object] | None:
    if touch is None:
        return None
    return {
        "touched_at": touch.touched_at.isoformat(),
        "price": str(touch.price),
        "actual_ratio": str(touch.actual_ratio),
        "reference_level": str(touch.reference_level),
        "candle_index": touch.candle_index,
    }


def _touch_from_state(value: object) -> CorrectionTouch | None:
    if value is None:
        return None
    state = _require_mapping(value, "correction touch")
    expected = {"touched_at", "price", "actual_ratio", "reference_level", "candle_index"}
    if set(state) != expected:
        raise ValueError("checkpoint correction touch keys are invalid")
    return CorrectionTouch(
        touched_at=_parse_utc(state["touched_at"], "correction.touched_at"),
        price=Decimal(str(state["price"])),
        actual_ratio=Decimal(str(state["actual_ratio"])),
        reference_level=Decimal(str(state["reference_level"])),
        candle_index=_require_int(state["candle_index"], "correction.candle_index"),
    )


def _wave_to_state(wave: FibonacciWaveSnapshot) -> dict[str, object]:
    return {
        "wave_id": wave.wave_id,
        "sequence_number": wave.sequence_number,
        "direction": wave.direction,
        "phase": wave.phase.value,
        "start_time": wave.start_time.isoformat(),
        "start_price": str(wave.start_price),
        "end_time": wave.end_time.isoformat(),
        "end_price": str(wave.end_price),
        "endpoint_revision": wave.endpoint_revision,
        "correction": _touch_to_state(wave.correction),
        "completion_target": None if wave.completion_target is None else str(wave.completion_target),
        "completion_time": None if wave.completion_time is None else wave.completion_time.isoformat(),
        "completion_price": None if wave.completion_price is None else str(wave.completion_price),
        "parent_wave_id": wave.parent_wave_id,
        "start_candle_index": wave.start_candle_index,
        "end_candle_index": wave.end_candle_index,
        "phase_started_index": wave.phase_started_index,
        "endpoint_confirmed_at": (
            None if wave.endpoint_confirmed_at is None else wave.endpoint_confirmed_at.isoformat()
        ),
        "endpoint_confirmation_index": wave.endpoint_confirmation_index,
    }


def _wave_from_state(state: Mapping[str, object]) -> FibonacciWaveSnapshot:
    expected = {
        "wave_id",
        "sequence_number",
        "direction",
        "phase",
        "start_time",
        "start_price",
        "end_time",
        "end_price",
        "endpoint_revision",
        "correction",
        "completion_target",
        "completion_time",
        "completion_price",
        "parent_wave_id",
        "start_candle_index",
        "end_candle_index",
        "phase_started_index",
        "endpoint_confirmed_at",
        "endpoint_confirmation_index",
    }
    if set(state) != expected:
        raise ValueError("checkpoint wave keys are invalid")
    direction = str(state["direction"])
    if direction not in {"bullish", "bearish"}:
        raise ValueError("checkpoint wave direction is invalid")
    return FibonacciWaveSnapshot(
        wave_id=str(state["wave_id"]),
        sequence_number=_require_int(state["sequence_number"], "wave.sequence_number"),
        direction=cast(Direction, direction),
        phase=WorkflowPhase(str(state["phase"])),
        start_time=_parse_utc(state["start_time"], "wave.start_time"),
        start_price=Decimal(str(state["start_price"])),
        end_time=_parse_utc(state["end_time"], "wave.end_time"),
        end_price=Decimal(str(state["end_price"])),
        endpoint_revision=_require_int(state["endpoint_revision"], "wave.endpoint_revision"),
        correction=_touch_from_state(state["correction"]),
        completion_target=(None if state["completion_target"] is None else Decimal(str(state["completion_target"]))),
        completion_time=(
            None if state["completion_time"] is None else _parse_utc(state["completion_time"], "wave.completion_time")
        ),
        completion_price=(None if state["completion_price"] is None else Decimal(str(state["completion_price"]))),
        parent_wave_id=(None if state["parent_wave_id"] is None else str(state["parent_wave_id"])),
        start_candle_index=_require_int(state["start_candle_index"], "wave.start_candle_index"),
        end_candle_index=_require_int(state["end_candle_index"], "wave.end_candle_index"),
        phase_started_index=_require_int(state["phase_started_index"], "wave.phase_started_index"),
        endpoint_confirmed_at=(
            None
            if state["endpoint_confirmed_at"] is None
            else _parse_utc(state["endpoint_confirmed_at"], "wave.endpoint_confirmed_at")
        ),
        endpoint_confirmation_index=(
            None
            if state["endpoint_confirmation_index"] is None
            else _require_int(
                state["endpoint_confirmation_index"],
                "wave.endpoint_confirmation_index",
            )
        ),
    )


def _mutable_from_snapshot(wave: FibonacciWaveSnapshot) -> _MutableWave:
    return _MutableWave(
        wave_id=wave.wave_id,
        sequence_number=wave.sequence_number,
        direction=wave.direction,
        phase=wave.phase,
        start_time=wave.start_time,
        start_price=wave.start_price,
        end_time=wave.end_time,
        end_price=wave.end_price,
        endpoint_revision=wave.endpoint_revision,
        correction=wave.correction,
        completion_target=wave.completion_target,
        completion_time=wave.completion_time,
        completion_price=wave.completion_price,
        parent_wave_id=wave.parent_wave_id,
        start_candle_index=wave.start_candle_index,
        end_candle_index=wave.end_candle_index,
        phase_started_index=wave.phase_started_index,
        endpoint_confirmed_at=wave.endpoint_confirmed_at,
        endpoint_confirmation_index=wave.endpoint_confirmation_index,
    )


def _event_to_state(event: FibonacciWorkflowEvent) -> dict[str, object]:
    return {
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(),
        "wave_id": event.wave_id,
        "candle_index": event.candle_index,
        "details": list(event.details),
    }


def _event_from_state(state: Mapping[str, object]) -> FibonacciWorkflowEvent:
    expected = {"event_type", "occurred_at", "wave_id", "candle_index", "details"}
    if set(state) != expected:
        raise ValueError("checkpoint event is invalid")
    return FibonacciWorkflowEvent(
        event_type=WorkflowEventType(str(state["event_type"])),
        occurred_at=_parse_utc(state["occurred_at"], "event.occurred_at"),
        wave_id=str(state["wave_id"]),
        candle_index=_require_int(state["candle_index"], "event.candle_index"),
        details=tuple(str(item) for item in _require_sequence(state["details"], "event.details")),
    )


def _pivot_to_state(pivot: Pivot | None) -> dict[str, object] | None:
    if pivot is None:
        return None
    return {
        "index": pivot.index,
        "kind": pivot.kind,
        "price": str(pivot.price),
        "prominence": str(pivot.prominence),
    }


def _pivot_from_state(value: object) -> Pivot | None:
    if value is None:
        return None
    state = _require_mapping(value, "pivot")
    expected = {"index", "kind", "price", "prominence"}
    if set(state) != expected:
        raise ValueError("checkpoint pivot keys are invalid")
    kind = str(state["kind"])
    if kind not in {"high", "low"}:
        raise ValueError("checkpoint pivot kind is invalid")
    return Pivot(
        index=_require_int(state["index"], "pivot.index"),
        kind=cast(PivotKind, kind),
        price=Decimal(str(state["price"])),
        prominence=Decimal(str(state["prominence"])),
    )


def replay_sequential_fibonacci(
    candles: tuple[Candle, ...],
    *,
    symbol: str,
    timeframe: str,
    dataset_hash: str,
    config: FibonacciConfig | None = None,
) -> FibonacciWorkflowSnapshot:
    if not candles:
        raise ValueError("candles cannot be empty")
    engine = SequentialFibonacciEngine(
        symbol=symbol,
        timeframe=timeframe,
        dataset_hash=dataset_hash,
        config=config,
    )
    for candle in candles:
        engine._process_candle(candle)
    return engine.snapshot(candles[-1].close_time)
