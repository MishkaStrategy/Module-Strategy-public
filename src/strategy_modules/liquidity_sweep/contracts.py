"""Static value contracts for published Liquidity Sweep features."""

from typing import Literal, TypedDict


class LiquidityLevelValue(TypedDict):
    level_id: str
    liquidity_side: Literal["high", "low"]
    price: float
    pivot_time: str
    confirmed_at: str


class LiquiditySweepValue(TypedDict):
    event_id: str
    decision_time: str
    level_id: str
    liquidity_side: Literal["high", "low"]
    level_price: float
    close_price: float
    pivot_time: str
    confirmed_at: str
    bars_since_confirmation: int
    direction: Literal["bullish", "bearish"]
    penetration_bps: float
    wick_to_body_ratio: float
    wick_fraction: float
    extreme_price: float
