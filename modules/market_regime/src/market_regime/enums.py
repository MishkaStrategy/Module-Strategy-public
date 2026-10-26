from __future__ import annotations

from enum import Enum


class Regime(str, Enum):
    UNKNOWN = "unknown"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    TRANSITION = "transition"


class TrendDirection(str, Enum):
    UNKNOWN = "unknown"
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class VolatilityLevel(str, Enum):
    UNKNOWN = "unknown"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class SkipReason(str, Enum):
    EMPTY_VIEW = "market_regime.empty_view"
    NO_TARGET_BAR = "market_regime.no_target_bar"
    MODULE_NOT_READY = "market_regime.not_ready"
    DATA_GAP_RESET = "market_regime.data_gap_reset"
    TIMEFRAME_GAP_RESET = "market_regime.timeframe_gap_reset"
    DUPLICATE_BAR = "market_regime.duplicate_bar"
    STALE_TARGET = "market_regime.stale_target"
    INCOMPLETE_BAR = "market_regime.incomplete_bar"
