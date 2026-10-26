from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
import math
import re
from types import MappingProxyType
from typing import Mapping

_TIMEFRAME_RE = re.compile(r"([1-9]\d*)([smhdw])")
_TIMEFRAME_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_MAX_TIMEDELTA_SECONDS = timedelta.max.days * 86400 + timedelta.max.seconds


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    try:
        numeric = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return 0.0 if numeric == 0.0 else numeric


def _validate_timeframe(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("timeframe must be a string")
    timeframe = value.strip().lower()
    if not timeframe:
        raise ValueError("timeframe must be non-blank")
    if timeframe == "base":
        return timeframe
    match = _TIMEFRAME_RE.fullmatch(timeframe)
    if match is None:
        raise ValueError("timeframe must be 'base' or a positive interval such as '5m' or '4h'")
    amount = int(match.group(1))
    unit_seconds = _TIMEFRAME_SECONDS[match.group(2)]
    if amount > _MAX_TIMEDELTA_SECONDS // unit_seconds:
        raise ValueError("timeframe duration is out of supported range")
    return timeframe


@dataclass(frozen=True, slots=True)
class LiquiditySweepConfig:
    """Validated configuration for :class:`LiquiditySweepModule`.

    ``timeframe='base'`` processes the engine base candle. Any explicit
    timeframe (for example ``'15m'``) is requested through the module manifest
    and read from ``MarketView.timeframe_bars``.
    """

    timeframe: str = "base"
    pivot_left_bars: int = 2
    pivot_right_bars: int = 2
    min_level_age_bars: int = 1
    max_level_age_bars: int = 500
    max_active_levels: int = 128
    min_penetration_bps: float = 0.0
    max_penetration_bps: float = 100.0
    close_reclaim_bps: float = 0.0
    invalidation_close_bps: float = 0.0
    min_wick_to_body_ratio: float = 1.0
    min_wick_fraction: float = 0.35
    require_directional_close: bool = False
    emit_signals: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", _validate_timeframe(self.timeframe))

        for name in (
            "pivot_left_bars",
            "pivot_right_bars",
            "min_level_age_bars",
            "max_level_age_bars",
            "max_active_levels",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")

        if self.pivot_left_bars < 1:
            raise ValueError("pivot_left_bars must be >= 1")
        if self.pivot_right_bars < 1:
            raise ValueError("pivot_right_bars must be >= 1")
        if self.min_level_age_bars < 0:
            raise ValueError("min_level_age_bars must be >= 0")
        if self.max_level_age_bars < self.min_level_age_bars:
            raise ValueError("max_level_age_bars must be >= min_level_age_bars")
        if self.max_active_levels < 1:
            raise ValueError("max_active_levels must be >= 1")

        for name in (
            "min_penetration_bps",
            "max_penetration_bps",
            "close_reclaim_bps",
            "invalidation_close_bps",
            "min_wick_to_body_ratio",
            "min_wick_fraction",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))

        if self.min_penetration_bps < 0:
            raise ValueError("min_penetration_bps must be >= 0")
        if self.max_penetration_bps < self.min_penetration_bps:
            raise ValueError("max_penetration_bps must be >= min_penetration_bps")
        if not 0 <= self.close_reclaim_bps <= 10_000:
            raise ValueError("close_reclaim_bps must be in [0, 10000]")
        if not 0 <= self.invalidation_close_bps <= 10_000:
            raise ValueError("invalidation_close_bps must be in [0, 10000]")
        if self.min_wick_to_body_ratio < 0:
            raise ValueError("min_wick_to_body_ratio must be >= 0")
        if not 0 <= self.min_wick_fraction <= 1:
            raise ValueError("min_wick_fraction must be in [0, 1]")
        for name in ("require_directional_close", "emit_signals"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")

    @property
    def pivot_window_bars(self) -> int:
        return self.pivot_left_bars + 1 + self.pivot_right_bars

    def as_parameters(self) -> Mapping[str, object]:
        return MappingProxyType(asdict(self))
