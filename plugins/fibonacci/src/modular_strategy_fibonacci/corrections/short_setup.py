"""Regime- and level-aware bearish reaction profiles for 4h/1d setups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Literal

from ..contracts import (
    FibonacciWaveSnapshot,
    FibonacciWorkflowSnapshot,
    WorkflowEventType,
    WorkflowPhase,
)

BearishRegimeClassification = Literal[
    "insufficient_data",
    "improving",
    "stable",
    "weakening",
]


@dataclass(frozen=True, slots=True)
class BearishReactionObservation:
    wave_id: str
    sequence_root_id: str
    observed_at: datetime
    correction_level: Decimal
    actual_ratio: Decimal
    continued_lower: bool

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))
        if self.correction_level not in {Decimal("0.5"), Decimal("0.618")}:
            raise ValueError("bearish reaction level must be 0.5 or 0.618")


@dataclass(frozen=True, slots=True)
class BearishReactionProfile:
    symbol: str
    timeframe: str
    sample_size: int
    successes: int
    success_rate: float
    latest_sequence_id: str | None
    latest_sequence_sample_size: int
    current_success_streak: int
    level_counts: tuple[tuple[Decimal, int], ...]
    qualifies: bool
    minimum_samples: int
    minimum_success_rate: float
    minimum_streak: int
    lookback_observations: int


@dataclass(frozen=True, slots=True)
class BearishReactionRegimeProfile:
    symbol: str
    timeframe: str
    window_size: int
    minimum_window_samples: int
    minimum_success_rate: float
    minimum_level_samples: int
    minimum_level_success_rate: float
    change_threshold: float
    recent_sample_size: int
    prior_sample_size: int
    recent_successes: int
    prior_successes: int
    recent_success_rate: float
    prior_success_rate: float
    success_rate_delta: float | None
    classification: BearishRegimeClassification
    comparable_windows: bool
    currently_reliable: bool
    recent_level_rates: tuple[tuple[Decimal, int, int, float], ...]


@dataclass(frozen=True, slots=True)
class BearishContinuationSetup:
    setup_id: str
    symbol: str
    timeframe: str
    wave_id: str
    sequence_root_id: str
    endpoint_revision: int
    correction_level: Decimal
    correction_ratio: Decimal
    correction_price: Decimal
    impulse_start_price: Decimal
    impulse_end_price: Decimal
    armed_at: datetime
    historical_success_rate: float
    historical_sample_size: int
    current_success_streak: int
    current_regime: BearishRegimeClassification
    recent_success_rate: float
    recent_sample_size: int
    prior_success_rate: float
    prior_sample_size: int
    recent_level_success_rate: float
    recent_level_sample_size: int
    regime_success_rate_delta: float | None
    evidence: tuple[str, ...]


def bearish_reaction_observations(
    workflow: FibonacciWorkflowSnapshot,
    *,
    accepted_levels: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("0.618")),
) -> tuple[BearishReactionObservation, ...]:
    """Extract terminal outcomes without joining unrelated wave sequences."""

    if workflow.timeframe not in {"4h", "1d"}:
        return ()
    waves = {
        wave.wave_id: wave
        for wave in (
            *workflow.completed_waves,
            *workflow.invalidated_waves,
            *workflow.expired_waves,
        )
    }
    terminal_events = {
        event.wave_id: event
        for event in workflow.events
        if event.event_type
        in {
            WorkflowEventType.WAVE_COMPLETED,
            WorkflowEventType.WAVE_INVALIDATED,
            WorkflowEventType.WAVE_EXPIRED,
        }
    }

    def root_id(wave_id: str) -> str:
        seen: set[str] = set()
        current = waves[wave_id]
        while current.parent_wave_id is not None and current.parent_wave_id in waves:
            if current.wave_id in seen:
                raise ValueError("cycle detected in Fibonacci parent chain")
            seen.add(current.wave_id)
            current = waves[current.parent_wave_id]
        return current.wave_id

    output: list[BearishReactionObservation] = []
    completed_ids = {wave.wave_id for wave in workflow.completed_waves}
    for wave_id, wave in waves.items():
        if wave.direction != "bearish" or wave.correction is None:
            continue
        if wave.correction.reference_level not in accepted_levels:
            continue
        terminal = terminal_events.get(wave_id)
        if terminal is None:
            continue
        output.append(
            BearishReactionObservation(
                wave_id=wave_id,
                sequence_root_id=root_id(wave_id),
                observed_at=terminal.occurred_at,
                correction_level=wave.correction.reference_level,
                actual_ratio=wave.correction.actual_ratio,
                continued_lower=wave_id in completed_ids,
            )
        )
    output.sort(key=lambda item: (item.observed_at, item.wave_id))
    return tuple(output)


def build_bearish_reaction_profile(
    workflow: FibonacciWorkflowSnapshot,
    *,
    minimum_samples: int = 8,
    minimum_success_rate: float = 0.65,
    minimum_streak: int = 3,
    lookback_observations: int = 40,
) -> BearishReactionProfile:
    """Build the rolling all-history profile and contiguous latest-root streak."""

    if minimum_samples < 1 or minimum_streak < 1 or lookback_observations < 1:
        raise ValueError("profile limits must be positive")
    if minimum_samples > lookback_observations:
        raise ValueError("minimum_samples cannot exceed lookback_observations")
    if minimum_streak > lookback_observations:
        raise ValueError("minimum_streak cannot exceed lookback_observations")
    if not 0.0 <= minimum_success_rate <= 1.0:
        raise ValueError("minimum_success_rate must be in [0, 1]")
    observations = bearish_reaction_observations(workflow)[-lookback_observations:]
    successes = sum(item.continued_lower for item in observations)
    sample_size = len(observations)
    success_rate = successes / sample_size if sample_size else 0.0
    latest_sequence_id = observations[-1].sequence_root_id if observations else None
    latest_sequence_reversed: list[BearishReactionObservation] = []
    for item in reversed(observations):
        if item.sequence_root_id != latest_sequence_id:
            break
        latest_sequence_reversed.append(item)
    latest_sequence = tuple(reversed(latest_sequence_reversed))
    streak = 0
    for item in reversed(latest_sequence):
        if not item.continued_lower:
            break
        streak += 1
    counts = tuple(
        (level, sum(item.correction_level == level for item in observations))
        for level in (Decimal("0.5"), Decimal("0.618"))
    )
    qualifies = (
        workflow.timeframe in {"4h", "1d"}
        and sample_size >= minimum_samples
        and success_rate >= minimum_success_rate
        and streak >= minimum_streak
    )
    return BearishReactionProfile(
        symbol=workflow.symbol,
        timeframe=workflow.timeframe,
        sample_size=sample_size,
        successes=successes,
        success_rate=success_rate,
        latest_sequence_id=latest_sequence_id,
        latest_sequence_sample_size=len(latest_sequence),
        current_success_streak=streak,
        level_counts=counts,
        qualifies=qualifies,
        minimum_samples=minimum_samples,
        minimum_success_rate=minimum_success_rate,
        minimum_streak=minimum_streak,
        lookback_observations=lookback_observations,
    )


def build_bearish_reaction_regime_profile(
    workflow: FibonacciWorkflowSnapshot,
    *,
    window_size: int = 12,
    minimum_window_samples: int = 8,
    minimum_success_rate: float = 0.65,
    minimum_level_samples: int = 4,
    minimum_level_success_rate: float = 0.65,
    change_threshold: float = 0.15,
) -> BearishReactionRegimeProfile:
    """Compare the latest chronological window with its immediate predecessor."""

    if window_size < 1 or minimum_window_samples < 1 or minimum_level_samples < 1:
        raise ValueError("regime window limits must be positive")
    if minimum_window_samples > window_size:
        raise ValueError("minimum_window_samples cannot exceed window_size")
    if minimum_level_samples > window_size:
        raise ValueError("minimum_level_samples cannot exceed window_size")
    if not 0.0 <= minimum_success_rate <= 1.0:
        raise ValueError("minimum_success_rate must be in [0, 1]")
    if not 0.0 <= minimum_level_success_rate <= 1.0:
        raise ValueError("minimum_level_success_rate must be in [0, 1]")
    if not 0.0 <= change_threshold <= 1.0:
        raise ValueError("change_threshold must be in [0, 1]")

    observations = bearish_reaction_observations(workflow)
    recent = observations[-window_size:]
    prior = observations[-2 * window_size : -window_size]
    recent_successes = sum(item.continued_lower for item in recent)
    prior_successes = sum(item.continued_lower for item in prior)
    recent_rate = recent_successes / len(recent) if recent else 0.0
    prior_rate = prior_successes / len(prior) if prior else 0.0
    comparable = len(recent) >= minimum_window_samples and len(prior) >= minimum_window_samples
    delta = recent_rate - prior_rate if comparable else None
    if not comparable:
        classification: BearishRegimeClassification = "insufficient_data"
    elif delta is not None and delta >= change_threshold:
        classification = "improving"
    elif delta is not None and delta <= -change_threshold:
        classification = "weakening"
    else:
        classification = "stable"

    level_rates = tuple(
        (
            level,
            len(selected),
            sum(item.continued_lower for item in selected),
            (sum(item.continued_lower for item in selected) / len(selected) if selected else 0.0),
        )
        for level in (Decimal("0.5"), Decimal("0.618"))
        for selected in (tuple(item for item in recent if item.correction_level == level),)
    )
    currently_reliable = (
        workflow.timeframe in {"4h", "1d"}
        and len(recent) >= minimum_window_samples
        and recent_rate >= minimum_success_rate
        and classification != "weakening"
    )
    return BearishReactionRegimeProfile(
        symbol=workflow.symbol,
        timeframe=workflow.timeframe,
        window_size=window_size,
        minimum_window_samples=minimum_window_samples,
        minimum_success_rate=minimum_success_rate,
        minimum_level_samples=minimum_level_samples,
        minimum_level_success_rate=minimum_level_success_rate,
        change_threshold=change_threshold,
        recent_sample_size=len(recent),
        prior_sample_size=len(prior),
        recent_successes=recent_successes,
        prior_successes=prior_successes,
        recent_success_rate=recent_rate,
        prior_success_rate=prior_rate,
        success_rate_delta=delta,
        classification=classification,
        comparable_windows=comparable,
        currently_reliable=currently_reliable,
        recent_level_rates=level_rates,
    )


def _active_sequence_root_id(
    workflow: FibonacciWorkflowSnapshot,
    wave_id: str,
) -> str:
    waves: dict[str, FibonacciWaveSnapshot] = {
        wave.wave_id: wave
        for wave in (
            *workflow.completed_waves,
            *workflow.invalidated_waves,
            *workflow.expired_waves,
            *((workflow.active_wave,) if workflow.active_wave is not None else ()),
        )
    }
    current = waves.get(wave_id)
    if current is None:
        raise ValueError("active Fibonacci wave is missing from workflow snapshot")
    seen: set[str] = set()
    while current.parent_wave_id is not None and current.parent_wave_id in waves:
        if current.wave_id in seen:
            raise ValueError("cycle detected in active Fibonacci parent chain")
        seen.add(current.wave_id)
        current = waves[current.parent_wave_id]
    return current.wave_id


def _canonical_setup_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"fib-short-{hashlib.sha256(encoded).hexdigest()}"


def active_bearish_continuation_setup(
    workflow: FibonacciWorkflowSnapshot,
    profile: BearishReactionProfile,
    regime: BearishReactionRegimeProfile | None = None,
) -> BearishContinuationSetup | None:
    """Arm an analytical setup only after all linked regime gates pass."""

    if profile.symbol != workflow.symbol or profile.timeframe != workflow.timeframe:
        raise ValueError("bearish reaction profile identity mismatch")
    regime = regime or build_bearish_reaction_regime_profile(workflow)
    if regime.symbol != workflow.symbol or regime.timeframe != workflow.timeframe:
        raise ValueError("bearish reaction regime identity mismatch")

    wave = workflow.active_wave
    if wave is None:
        return None
    if wave.correction is None:
        return None
    current_level_stats = next(
        (row for row in regime.recent_level_rates if row[0] == wave.correction.reference_level),
        None,
    )
    current_level_reliable = (
        current_level_stats is not None
        and current_level_stats[1] >= regime.minimum_level_samples
        and current_level_stats[3] >= regime.minimum_level_success_rate
    )
    active_sequence_root = _active_sequence_root_id(workflow, wave.wave_id)
    if (
        not profile.qualifies
        or not regime.currently_reliable
        or not current_level_reliable
        or profile.latest_sequence_id is None
        or active_sequence_root != profile.latest_sequence_id
        or workflow.timeframe not in {"4h", "1d"}
        or wave.direction != "bearish"
        or wave.phase is not WorkflowPhase.AWAITING_EXTENSION
        or wave.correction.reference_level not in {Decimal("0.5"), Decimal("0.618")}
    ):
        return None

    assert current_level_stats is not None
    fingerprint_payload: dict[str, object] = {
        "schema": "fibonacci.short_continuation_setup/v2",
        "symbol": workflow.symbol,
        "timeframe": workflow.timeframe,
        "wave_id": wave.wave_id,
        "sequence_root_id": active_sequence_root,
        "parent_wave_id": wave.parent_wave_id,
        "sequence_number": wave.sequence_number,
        "endpoint_revision": wave.endpoint_revision,
        "start_time": wave.start_time.isoformat(),
        "start_price": str(wave.start_price),
        "end_time": wave.end_time.isoformat(),
        "end_price": str(wave.end_price),
        "correction_touched_at": wave.correction.touched_at.isoformat(),
        "correction_candle_index": wave.correction.candle_index,
        "correction_level": str(wave.correction.reference_level),
        "correction_ratio": str(wave.correction.actual_ratio),
        "correction_price": str(wave.correction.price),
        "historical_sample_size": profile.sample_size,
        "historical_successes": profile.successes,
        "historical_success_rate": profile.success_rate,
        "historical_latest_sequence_size": profile.latest_sequence_sample_size,
        "historical_streak": profile.current_success_streak,
        "historical_minimum_samples": profile.minimum_samples,
        "historical_minimum_success_rate": profile.minimum_success_rate,
        "historical_minimum_streak": profile.minimum_streak,
        "historical_lookback": profile.lookback_observations,
        "regime_window_size": regime.window_size,
        "regime_minimum_window_samples": regime.minimum_window_samples,
        "regime_minimum_success_rate": regime.minimum_success_rate,
        "regime_minimum_level_samples": regime.minimum_level_samples,
        "regime_minimum_level_success_rate": regime.minimum_level_success_rate,
        "regime_change_threshold": regime.change_threshold,
        "recent_sample_size": regime.recent_sample_size,
        "recent_successes": regime.recent_successes,
        "recent_success_rate": regime.recent_success_rate,
        "prior_sample_size": regime.prior_sample_size,
        "prior_successes": regime.prior_successes,
        "prior_success_rate": regime.prior_success_rate,
        "regime_delta": regime.success_rate_delta,
        "regime_classification": regime.classification,
        "current_level_sample_size": current_level_stats[1],
        "current_level_successes": current_level_stats[2],
        "current_level_success_rate": current_level_stats[3],
    }
    setup_id = _canonical_setup_id(fingerprint_payload)
    return BearishContinuationSetup(
        setup_id=setup_id,
        symbol=workflow.symbol,
        timeframe=workflow.timeframe,
        wave_id=wave.wave_id,
        sequence_root_id=active_sequence_root,
        endpoint_revision=wave.endpoint_revision,
        correction_level=wave.correction.reference_level,
        correction_ratio=wave.correction.actual_ratio,
        correction_price=wave.correction.price,
        impulse_start_price=wave.start_price,
        impulse_end_price=wave.end_price,
        armed_at=wave.correction.touched_at,
        historical_success_rate=profile.success_rate,
        historical_sample_size=profile.sample_size,
        current_success_streak=profile.current_success_streak,
        current_regime=regime.classification,
        recent_success_rate=regime.recent_success_rate,
        recent_sample_size=regime.recent_sample_size,
        prior_success_rate=regime.prior_success_rate,
        prior_sample_size=regime.prior_sample_size,
        recent_level_success_rate=current_level_stats[3],
        recent_level_sample_size=current_level_stats[1],
        regime_success_rate_delta=regime.success_rate_delta,
        evidence=(
            "direction=bearish",
            f"timeframe={workflow.timeframe}",
            f"sequence_root_id={active_sequence_root}",
            f"endpoint_revision={wave.endpoint_revision}",
            f"correction_level={wave.correction.reference_level}",
            f"historical_success_rate={profile.success_rate:.6f}",
            f"current_success_streak={profile.current_success_streak}",
            f"current_regime={regime.classification}",
            f"recent_success_rate={regime.recent_success_rate:.6f}",
            f"recent_level_success_rate={current_level_stats[3]:.6f}",
            "execution_not_performed=true",
        ),
    )
