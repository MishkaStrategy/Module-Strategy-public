"""Correction-depth classification and regime-aware profiles."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Iterable

from ..contracts import Direction, FibonacciWorkflowSnapshot


@dataclass(frozen=True, slots=True)
class CorrectionDepthObservation:
    symbol: str
    timeframe: str
    direction: Direction
    observed_at: datetime
    actual_ratio: Decimal
    reference_level: Decimal
    wave_id: str
    dataset_hash: str

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))
        if not Decimal("0") <= self.actual_ratio < Decimal("1"):
            raise ValueError("actual_ratio must be in [0, 1)")
        if not Decimal("0") < self.reference_level < Decimal("1"):
            raise ValueError("reference_level must be in (0, 1)")
        if not self.wave_id:
            raise ValueError("wave_id cannot be empty")


@dataclass(frozen=True, slots=True)
class CorrectionProfileSnapshot:
    symbol: str
    timeframe: str
    direction: Direction | None
    sample_size: int
    level_counts: tuple[tuple[Decimal, int], ...]
    level_probabilities: tuple[tuple[Decimal, float], ...]
    median_actual_ratio: Decimal | None
    preferred_levels: tuple[Decimal, ...]
    window_start: datetime | None
    window_end: datetime | None
    confidence: float


def classify_correction_depth(
    actual_ratio: Decimal,
    levels: tuple[Decimal, ...],
    *,
    shallow_tolerance: Decimal,
) -> Decimal | None:
    """Map an actual retracement ratio to the deepest configured level reached.

    A small tolerance is intentionally applied only to the shallowest configured
    level. This represents the user's rule that a valid correction may stop just
    short of 0.5, while deeper levels remain exact structural thresholds.
    """

    if not Decimal("0") <= actual_ratio < Decimal("1"):
        return None
    if not levels:
        return None
    ordered = tuple(sorted(levels))
    shallowest = ordered[0]
    if actual_ratio < shallowest - shallow_tolerance:
        return None
    reached = shallowest
    for level in ordered[1:]:
        if actual_ratio >= level:
            reached = level
        else:
            break
    return reached


class CorrectionProfile:
    """Rolling empirical correction-depth distribution.

    Profiles are intentionally scoped by symbol, timeframe and optionally
    direction. This prevents a deep-correction instrument or regime from being
    flattened into a project-wide average.
    """

    def __init__(self, observations: Iterable[CorrectionDepthObservation] = ()) -> None:
        self._observations = list(observations)

    def add(self, observation: CorrectionDepthObservation) -> None:
        self._observations.append(observation)

    def observations(self) -> tuple[CorrectionDepthObservation, ...]:
        return tuple(self._observations)

    def snapshot(
        self,
        *,
        symbol: str,
        timeframe: str,
        levels: tuple[Decimal, ...],
        direction: Direction | None = None,
        as_of: datetime | None = None,
        lookback_observations: int = 200,
        lookback_period: timedelta | None = None,
    ) -> CorrectionProfileSnapshot:
        if lookback_observations < 1:
            raise ValueError("lookback_observations must be positive")
        normalized_as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        selected = [
            item
            for item in self._observations
            if item.symbol == symbol
            and item.timeframe == timeframe
            and (direction is None or item.direction == direction)
            and item.observed_at <= normalized_as_of
        ]
        if lookback_period is not None:
            boundary = normalized_as_of - lookback_period
            selected = [item for item in selected if item.observed_at >= boundary]
        selected.sort(key=lambda item: (item.observed_at, item.wave_id))
        selected = selected[-lookback_observations:]

        counts = Counter(item.reference_level for item in selected)
        ordered_counts = tuple((level, counts.get(level, 0)) for level in levels)
        total = len(selected)
        probabilities = tuple((level, count / total if total else 0.0) for level, count in ordered_counts)
        preferred = tuple(
            level
            for level, _ in sorted(
                ordered_counts,
                key=lambda item: (-item[1], levels.index(item[0])),
            )
            if counts.get(level, 0) > 0
        )
        median_ratio = Decimal(str(median([float(item.actual_ratio) for item in selected]))) if selected else None
        confidence = min(1.0, total / 30.0)
        return CorrectionProfileSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            sample_size=total,
            level_counts=ordered_counts,
            level_probabilities=probabilities,
            median_actual_ratio=median_ratio,
            preferred_levels=preferred,
            window_start=selected[0].observed_at if selected else None,
            window_end=selected[-1].observed_at if selected else None,
            confidence=confidence,
        )

    def score_level(
        self,
        level: Decimal,
        *,
        symbol: str,
        timeframe: str,
        levels: tuple[Decimal, ...],
        direction: Direction | None = None,
        as_of: datetime | None = None,
        lookback_observations: int = 200,
        prior_weight: float = 1.0,
    ) -> float:
        """Return a smoothed empirical probability for downstream decisions."""

        if level not in levels:
            raise ValueError("level is not configured")
        snapshot = self.snapshot(
            symbol=symbol,
            timeframe=timeframe,
            levels=levels,
            direction=direction,
            as_of=as_of,
            lookback_observations=lookback_observations,
        )
        count_by_level = dict(snapshot.level_counts)
        denominator = snapshot.sample_size + prior_weight * len(levels)
        return (count_by_level[level] + prior_weight) / denominator


def observations_from_workflow(
    workflow: FibonacciWorkflowSnapshot,
) -> tuple[CorrectionDepthObservation, ...]:
    """Extract one immutable training observation per completed wave."""

    observations: list[CorrectionDepthObservation] = []
    for wave in workflow.completed_waves:
        if wave.correction is None or wave.completion_time is None:
            continue
        observations.append(
            CorrectionDepthObservation(
                symbol=workflow.symbol,
                timeframe=workflow.timeframe,
                direction=wave.direction,
                observed_at=wave.completion_time,
                actual_ratio=wave.correction.actual_ratio,
                reference_level=wave.correction.reference_level,
                wave_id=wave.wave_id,
                dataset_hash=workflow.dataset_hash,
            )
        )
    return tuple(observations)
