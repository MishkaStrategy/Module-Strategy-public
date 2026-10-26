"""Typed, stable reason codes emitted by the Liquidity Sweep module."""

from enum import Enum


class LiquiditySweepReason(str, Enum):
    """Public reason values used by skips, diagnostics and feature deletion."""

    EMPTY_MARKET_VIEW = "liquidity_sweep.empty_market_view"
    TIMEFRAME_UNAVAILABLE = "liquidity_sweep.timeframe_unavailable"
    TIMEFRAME_MISMATCH = "liquidity_sweep.timeframe_mismatch"
    DATA_GAP_RESET = "liquidity_sweep.data_gap_reset"
    STREAM_CHANGED_RESET = "liquidity_sweep.stream_changed_reset"
    DUPLICATE_BAR = "liquidity_sweep.duplicate_bar"
    CONFLICTING_DUPLICATE_BAR = "liquidity_sweep.conflicting_duplicate_bar"
    OUT_OF_ORDER_BAR = "liquidity_sweep.out_of_order_bar"

    ZERO_RANGE = "zero_range"
    PENETRATION_BELOW_MIN = "penetration_below_min"
    PENETRATION_ABOVE_MAX = "penetration_above_max"
    CLOSE_NOT_RECLAIMED = "close_not_reclaimed"
    DIRECTIONAL_CLOSE_REQUIRED = "directional_close_required"
    WICK_RATIO_BELOW_MIN = "wick_ratio_below_min"
    WICK_FRACTION_BELOW_MIN = "wick_fraction_below_min"

    DATA_GAP = "data_gap"
    STREAM_CHANGED = "stream_changed"
    LEVEL_EXPIRED = "level_expired"
    LEVEL_SWEPT = "level_swept"
    ACCEPTED_BREAKOUT = "accepted_breakout"
    CAPACITY_EVICTION = "capacity_eviction"
