from __future__ import annotations

from typing import TypedDict

CURRENT_FEATURE_KEY = "market_regime.current"
TREND_FEATURE_KEY = "market_regime.trend"
VOLATILITY_FEATURE_KEY = "market_regime.volatility"
FEATURE_SCHEMA_VERSION = "1"


class IndicatorMetricsValue(TypedDict):
    ema_fast: float
    ema_slow: float
    ema_spread_pct: float
    ema_slope_pct: float
    efficiency_ratio: float
    atr: float
    atr_pct: float
    baseline_atr_pct: float
    volatility_ratio: float


class CurrentRegimeValue(TypedDict):
    regime: str
    raw_regime: str
    trend: str
    volatility: str
    raw_volatility: str
    confidence: float
    as_of: str
    metrics: IndicatorMetricsValue


class TrendFeatureValue(TypedDict):
    direction: str
    regime: str
    raw_regime: str
    confidence: float
    ema_fast: float
    ema_slow: float
    ema_spread_pct: float
    ema_slope_pct: float
    efficiency_ratio: float


class VolatilityFeatureValue(TypedDict):
    level: str
    raw_level: str
    atr: float
    atr_pct: float
    baseline_atr_pct: float
    ratio: float
