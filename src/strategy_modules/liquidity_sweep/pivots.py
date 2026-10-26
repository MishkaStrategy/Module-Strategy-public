from __future__ import annotations

import hashlib
from collections.abc import Sequence

from .config import LiquiditySweepConfig
from .constants import MODULE_NAME
from .models import BarSnapshot, LiquidityLevel, LiquiditySide


def confirm_levels(
    bars: Sequence[BarSnapshot],
    config: LiquiditySweepConfig,
    sequence: int,
    existing_ids: set[str],
) -> tuple[LiquidityLevel, ...]:
    if len(bars) < config.pivot_window_bars:
        return ()
    candidate_index = config.pivot_left_bars
    candidate = bars[candidate_index]
    neighbours = tuple(bars[:candidate_index]) + tuple(bars[candidate_index + 1 :])
    confirmation_bar = bars[-1]
    levels: list[LiquidityLevel] = []
    if all(candidate.high > item.high for item in neighbours):
        levels.append(_new_level(candidate, confirmation_bar, LiquiditySide.HIGH, sequence))
    if all(candidate.low < item.low for item in neighbours):
        levels.append(_new_level(candidate, confirmation_bar, LiquiditySide.LOW, sequence))
    return tuple(level for level in levels if level.level_id not in existing_ids)


def _new_level(
    candidate: BarSnapshot,
    confirmation_bar: BarSnapshot,
    side: LiquiditySide,
    sequence: int,
) -> LiquidityLevel:
    raw = "|".join(
        (
            MODULE_NAME,
            candidate.symbol,
            candidate.timeframe,
            side.value,
            candidate.timestamp.isoformat(),
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    level_id = f"liq-level-{digest}"
    return LiquidityLevel(
        level_id=level_id,
        feature_id=f"feat-{level_id}",
        side=side,
        price=candidate.high if side is LiquiditySide.HIGH else candidate.low,
        symbol=candidate.symbol,
        timeframe=candidate.timeframe,
        pivot_time=candidate.timestamp,
        confirmed_at=confirmation_bar.close_time,
        confirmed_sequence=sequence,
    )
