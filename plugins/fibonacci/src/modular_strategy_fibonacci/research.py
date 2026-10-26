"""Reproducible scanner and history-only walk-forward helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .config import FibonacciConfig
from .contracts import FibonacciWorkflowSnapshot
from .corrections.short_setup import (
    BearishReactionProfile,
    BearishReactionRegimeProfile,
    active_bearish_continuation_setup,
    build_bearish_reaction_profile,
    build_bearish_reaction_regime_profile,
)


@dataclass(frozen=True, slots=True)
class BearishReactionScanRow:
    symbol: str
    timeframe: str
    cutoff_time: str
    historical_sample_size: int
    historical_success_rate: float
    current_success_streak: int
    recent_sample_size: int
    recent_success_rate: float
    prior_sample_size: int
    prior_success_rate: float
    regime: str
    recent_level_rates: tuple[tuple[str, int, int, float], ...]
    active_setup_id: str | None


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    symbol: str
    timeframe: str
    evaluated_cutoffs: int
    selected_cutoffs: int
    selected_setup_ids: tuple[str, ...]


def _profiles(
    workflow: FibonacciWorkflowSnapshot,
    config: FibonacciConfig,
) -> tuple[BearishReactionProfile, BearishReactionRegimeProfile]:
    profile = build_bearish_reaction_profile(
        workflow,
        minimum_samples=config.short_reaction_minimum_samples,
        minimum_success_rate=float(config.short_reaction_minimum_success_rate),
        minimum_streak=config.short_reaction_minimum_streak,
        lookback_observations=config.short_reaction_lookback,
    )
    regime = build_bearish_reaction_regime_profile(
        workflow,
        window_size=config.short_regime_window_size,
        minimum_window_samples=config.short_regime_minimum_window_samples,
        minimum_success_rate=float(config.short_regime_minimum_success_rate),
        minimum_level_samples=config.short_regime_minimum_level_samples,
        minimum_level_success_rate=float(config.short_regime_minimum_level_success_rate),
        change_threshold=float(config.short_regime_change_threshold),
    )
    return profile, regime


def scan_bearish_reactions(
    workflows: Iterable[FibonacciWorkflowSnapshot],
    *,
    config: FibonacciConfig | None = None,
) -> tuple[BearishReactionScanRow, ...]:
    """Scan independent point-in-time snapshots without manufacturing trades."""

    selected_config = config or FibonacciConfig()
    rows: list[BearishReactionScanRow] = []
    for workflow in sorted(
        workflows,
        key=lambda item: (item.symbol, item.timeframe, item.cutoff_time),
    ):
        profile, regime = _profiles(workflow, selected_config)
        setup = active_bearish_continuation_setup(workflow, profile, regime)
        rows.append(
            BearishReactionScanRow(
                symbol=workflow.symbol,
                timeframe=workflow.timeframe,
                cutoff_time=workflow.cutoff_time.isoformat(),
                historical_sample_size=profile.sample_size,
                historical_success_rate=profile.success_rate,
                current_success_streak=profile.current_success_streak,
                recent_sample_size=regime.recent_sample_size,
                recent_success_rate=regime.recent_success_rate,
                prior_sample_size=regime.prior_sample_size,
                prior_success_rate=regime.prior_success_rate,
                regime=regime.classification,
                recent_level_rates=tuple(
                    (str(level), sample, successes, rate)
                    for level, sample, successes, rate in regime.recent_level_rates
                ),
                active_setup_id=None if setup is None else setup.setup_id,
            )
        )
    return tuple(rows)


def history_only_walk_forward(
    workflows: Iterable[FibonacciWorkflowSnapshot],
    *,
    config: FibonacciConfig | None = None,
) -> tuple[WalkForwardSummary, ...]:
    """Evaluate each supplied cutoff using only that cutoff's frozen history."""

    selected_config = config or FibonacciConfig()
    grouped: dict[tuple[str, str], list[FibonacciWorkflowSnapshot]] = {}
    for workflow in workflows:
        grouped.setdefault((workflow.symbol, workflow.timeframe), []).append(workflow)

    output: list[WalkForwardSummary] = []
    for (symbol, timeframe), snapshots in sorted(grouped.items()):
        ordered = sorted(snapshots, key=lambda item: item.cutoff_time)
        if len({item.cutoff_time for item in ordered}) != len(ordered):
            raise ValueError("walk-forward cutoffs must be unique per symbol/timeframe")
        setup_ids: list[str] = []
        for workflow in ordered:
            profile, regime = _profiles(workflow, selected_config)
            setup = active_bearish_continuation_setup(workflow, profile, regime)
            if setup is not None:
                setup_ids.append(setup.setup_id)
        output.append(
            WalkForwardSummary(
                symbol=symbol,
                timeframe=timeframe,
                evaluated_cutoffs=len(ordered),
                selected_cutoffs=len(setup_ids),
                selected_setup_ids=tuple(setup_ids),
            )
        )
    return tuple(output)


def scan_payload(rows: Iterable[BearishReactionScanRow]) -> tuple[Mapping[str, object], ...]:
    """Return JSON-ready, versioned scanner payload rows."""

    return tuple(
        {
            "schema_version": "fibonacci.bearish_reaction_scan/v2",
            **asdict(row),
            "orders": (),
            "trades": (),
        }
        for row in rows
    )
