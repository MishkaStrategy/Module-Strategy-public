"""Immutable public contracts used by the Fibonacci module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

Direction = Literal["bullish", "bearish"]


class WorkflowPhase(StrEnum):
    """Current real-time phase of one sequential Fibonacci wave."""

    BUILDING_IMPULSE = "building_impulse"
    AWAITING_CORRECTION = "awaiting_correction"
    AWAITING_EXTENSION = "awaiting_extension"
    EXTENDING_WITHOUT_CORRECTION = "extending_without_correction"
    COMPLETED = "completed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class WorkflowEventType(StrEnum):
    WAVE_STARTED = "wave_started"
    ENDPOINT_UPDATED = "endpoint_updated"
    ENDPOINT_CONFIRMED = "endpoint_confirmed"
    CORRECTION_TOUCHED = "correction_touched"
    CORRECTION_DEEPENED = "correction_deepened"
    TARGET_REACHED = "target_reached"
    WAVE_COMPLETED = "wave_completed"
    EXTENSION_REANCHOR_STARTED = "extension_reanchor_started"
    WAVE_EXPIRED = "wave_expired"
    WAVE_INVALIDATED = "wave_invalidated"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_decimal(value: Decimal, field_name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_time", _require_utc(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", _require_utc(self.close_time, "close_time"))
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, _finite_decimal(getattr(self, name), name))
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high violates OHLC invariant")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low violates OHLC invariant")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")


@dataclass(frozen=True, slots=True)
class FibonacciLevel:
    ratio: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        _finite_decimal(self.ratio, "ratio")
        _finite_decimal(self.price, "price")


@dataclass(frozen=True, slots=True)
class ImpulseCandidate:
    candidate_id: str
    direction: Direction
    start_index: int
    end_index: int
    start_time: datetime
    end_time: datetime
    start_price: Decimal
    end_price: Decimal
    score: float
    evidence: tuple[str, ...]
    confirmation_index: int | None = None
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", _require_utc(self.start_time, "start_time"))
        object.__setattr__(self, "end_time", _require_utc(self.end_time, "end_time"))
        _finite_decimal(self.start_price, "start_price")
        _finite_decimal(self.end_price, "end_price")
        if self.start_index < 0 or self.end_index <= self.start_index:
            raise ValueError("candidate indexes must be ordered")
        if self.end_time <= self.start_time:
            raise ValueError("candidate times must be ordered")
        if self.direction == "bullish" and self.end_price <= self.start_price:
            raise ValueError("bullish candidate must end above start")
        if self.direction == "bearish" and self.end_price >= self.start_price:
            raise ValueError("bearish candidate must end below start")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be in [0, 1]")
        if self.confirmation_index is not None and self.confirmation_index < self.end_index:
            raise ValueError("confirmation_index cannot precede end_index")
        if self.confirmed_at is not None:
            object.__setattr__(
                self,
                "confirmed_at",
                _require_utc(self.confirmed_at, "confirmed_at"),
            )
            if self.confirmed_at < self.end_time:
                raise ValueError("confirmed_at cannot precede end_time")


@dataclass(frozen=True, slots=True)
class CorrectionTouch:
    """Deepest known correction for a wave at one real-time cutoff."""

    touched_at: datetime
    price: Decimal
    actual_ratio: Decimal
    reference_level: Decimal
    candle_index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "touched_at", _require_utc(self.touched_at, "touched_at"))
        _finite_decimal(self.price, "price")
        _finite_decimal(self.actual_ratio, "actual_ratio")
        _finite_decimal(self.reference_level, "reference_level")
        if self.candle_index < 0:
            raise ValueError("candle_index must be non-negative")
        if not Decimal("0") <= self.actual_ratio < Decimal("1"):
            raise ValueError("correction ratio must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class FibonacciWorkflowEvent:
    event_type: WorkflowEventType
    occurred_at: datetime
    wave_id: str
    candle_index: int
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "occurred_at",
            _require_utc(self.occurred_at, "occurred_at"),
        )
        if not self.wave_id:
            raise ValueError("wave_id cannot be empty")
        if self.candle_index < 0:
            raise ValueError("candle_index must be non-negative")


@dataclass(frozen=True, slots=True)
class FibonacciWaveSnapshot:
    wave_id: str
    sequence_number: int
    direction: Direction
    phase: WorkflowPhase
    start_time: datetime
    start_price: Decimal
    end_time: datetime
    end_price: Decimal
    endpoint_revision: int
    correction: CorrectionTouch | None
    completion_target: Decimal | None
    completion_time: datetime | None
    completion_price: Decimal | None
    parent_wave_id: str | None = None
    start_candle_index: int = -1
    end_candle_index: int = -1
    phase_started_index: int = -1
    endpoint_confirmed_at: datetime | None = None
    endpoint_confirmation_index: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", _require_utc(self.start_time, "start_time"))
        object.__setattr__(self, "end_time", _require_utc(self.end_time, "end_time"))
        _finite_decimal(self.start_price, "start_price")
        _finite_decimal(self.end_price, "end_price")
        if self.sequence_number < 1:
            raise ValueError("sequence_number must be positive")
        if self.end_time < self.start_time:
            raise ValueError("wave end_time cannot precede start_time")
        if self.endpoint_revision < 0:
            raise ValueError("endpoint_revision must be non-negative")
        if self.direction == "bullish" and self.end_price <= self.start_price:
            raise ValueError("bullish wave must end above start")
        if self.direction == "bearish" and self.end_price >= self.start_price:
            raise ValueError("bearish wave must end below start")
        if self.completion_target is not None:
            _finite_decimal(self.completion_target, "completion_target")
        if self.completion_time is not None:
            object.__setattr__(
                self,
                "completion_time",
                _require_utc(self.completion_time, "completion_time"),
            )
        if self.completion_price is not None:
            _finite_decimal(self.completion_price, "completion_price")
        if (
            min(
                self.start_candle_index,
                self.end_candle_index,
                self.phase_started_index,
            )
            < -1
        ):
            raise ValueError("candle indexes cannot be below -1")
        if self.endpoint_confirmed_at is not None:
            object.__setattr__(
                self,
                "endpoint_confirmed_at",
                _require_utc(self.endpoint_confirmed_at, "endpoint_confirmed_at"),
            )
            if self.endpoint_confirmed_at < self.end_time:
                raise ValueError("endpoint confirmation cannot precede endpoint")
        if self.endpoint_confirmation_index is not None:
            if self.endpoint_confirmation_index < self.end_candle_index:
                raise ValueError("endpoint confirmation index cannot precede endpoint")
            if self.endpoint_confirmation_index < 0:
                raise ValueError("endpoint confirmation index must be non-negative")
        if (self.endpoint_confirmed_at is None) != (self.endpoint_confirmation_index is None):
            raise ValueError("endpoint confirmation time and index must be set together")


@dataclass(frozen=True, slots=True)
class FibonacciWorkflowSnapshot:
    symbol: str
    market: str
    timeframe: str
    cutoff_time: datetime
    active_wave: FibonacciWaveSnapshot | None
    completed_waves: tuple[FibonacciWaveSnapshot, ...]
    invalidated_waves: tuple[FibonacciWaveSnapshot, ...]
    events: tuple[FibonacciWorkflowEvent, ...]
    workflow_version: str
    config_hash: str
    dataset_hash: str
    expired_waves: tuple[FibonacciWaveSnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cutoff_time", _require_utc(self.cutoff_time, "cutoff_time"))


@dataclass(frozen=True, slots=True)
class FibonacciProjection:
    projection_id: str
    symbol: str
    market: str
    timeframe: str
    cutoff_time: datetime
    direction: Direction
    impulse_start_time: datetime
    impulse_start_price: Decimal
    impulse_end_time: datetime
    impulse_end_price: Decimal
    levels: tuple[FibonacciLevel, ...]
    confidence: float
    candidate_score: float
    model_version: str
    config_hash: str
    dataset_hash: str
    evidence: tuple[str, ...]
    workflow_phase: WorkflowPhase | None = None
    sequence_number: int = 1
    endpoint_revision: int = 0
    correction_actual_ratio: Decimal | None = None
    correction_reference_level: Decimal | None = None
    correction_level_probabilities: tuple[tuple[Decimal, float], ...] = ()
    preferred_correction_levels: tuple[Decimal, ...] = ()
    correction_profile_sample_size: int = 0
    endpoint_confirmed_at: datetime | None = None
    endpoint_confirmation_index: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cutoff_time", _require_utc(self.cutoff_time, "cutoff_time"))
        object.__setattr__(
            self,
            "impulse_start_time",
            _require_utc(self.impulse_start_time, "impulse_start_time"),
        )
        object.__setattr__(
            self,
            "impulse_end_time",
            _require_utc(self.impulse_end_time, "impulse_end_time"),
        )
        _finite_decimal(self.impulse_start_price, "impulse_start_price")
        _finite_decimal(self.impulse_end_price, "impulse_end_price")
        if self.impulse_end_time > self.cutoff_time:
            raise ValueError("projection cannot use data after cutoff")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not 0.0 <= self.candidate_score <= 1.0:
            raise ValueError("candidate_score must be in [0, 1]")
        if not self.levels:
            raise ValueError("levels cannot be empty")
        if self.sequence_number < 1:
            raise ValueError("sequence_number must be positive")
        if self.endpoint_revision < 0:
            raise ValueError("endpoint_revision must be non-negative")
        if self.correction_actual_ratio is not None:
            _finite_decimal(self.correction_actual_ratio, "correction_actual_ratio")
            if not Decimal("0") <= self.correction_actual_ratio < Decimal("1"):
                raise ValueError("correction_actual_ratio must be in [0, 1)")
        if self.correction_reference_level is not None:
            _finite_decimal(
                self.correction_reference_level,
                "correction_reference_level",
            )
        if self.correction_profile_sample_size < 0:
            raise ValueError("correction_profile_sample_size must be non-negative")
        if self.endpoint_confirmed_at is not None:
            object.__setattr__(
                self,
                "endpoint_confirmed_at",
                _require_utc(self.endpoint_confirmed_at, "endpoint_confirmed_at"),
            )
            if self.endpoint_confirmed_at < self.impulse_end_time:
                raise ValueError("endpoint confirmation cannot precede impulse endpoint")
        if self.endpoint_confirmation_index is not None and self.endpoint_confirmation_index < 0:
            raise ValueError("endpoint_confirmation_index must be non-negative")
        if (self.endpoint_confirmed_at is None) != (self.endpoint_confirmation_index is None):
            raise ValueError("projection endpoint confirmation fields must be set together")
        for level, probability in self.correction_level_probabilities:
            _finite_decimal(level, "correction_probability_level")
            if not 0.0 <= probability <= 1.0:
                raise ValueError("correction probability must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class NoFibonacciProjection:
    reason_code: str
    confidence: float
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("reason_code cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
