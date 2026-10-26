from __future__ import annotations

from typing import Any, Mapping

from modular_core import ModuleManifest

from .config import MarketRegimeConfig
from .contracts import CURRENT_FEATURE_KEY, TREND_FEATURE_KEY, VOLATILITY_FEATURE_KEY

MODULE_NAME = "market_regime"
MODULE_VERSION = "1.2.0"
API_VERSION = "1.0"

PARAMETERS_SCHEMA: Mapping[str, Mapping[str, Any]] = {
    "timeframe": {"type": "string", "default": "1h"},
    "fast_ema_period": {"type": "integer", "minimum": 2, "maximum": 10_000, "default": 20},
    "slow_ema_period": {"type": "integer", "minimum": 3, "maximum": 20_000, "default": 50},
    "atr_period": {"type": "integer", "minimum": 2, "maximum": 10_000, "default": 14},
    "volatility_baseline_period": {
        "type": "integer",
        "minimum": 5,
        "maximum": 50_000,
        "default": 50,
    },
    "efficiency_period": {"type": "integer", "minimum": 2, "maximum": 10_000, "default": 20},
    "slope_lookback": {"type": "integer", "minimum": 1, "maximum": 10_000, "default": 5},
    "trend_spread_threshold": {"type": "string", "default": "0.001"},
    "trend_slope_threshold": {"type": "string", "default": "0.00015"},
    "trend_efficiency_threshold": {"type": "string", "default": "0.35"},
    "range_efficiency_threshold": {"type": "string", "default": "0.22"},
    "low_volatility_ratio": {"type": "string", "default": "0.75"},
    "high_volatility_ratio": {"type": "string", "default": "1.35"},
    "regime_confirmation_bars": {"type": "integer", "minimum": 1, "maximum": 100, "default": 2},
    "volatility_confirmation_bars": {"type": "integer", "minimum": 1, "maximum": 100, "default": 2},
    "publish_diagnostics": {"type": "boolean", "default": True},
}


def build_manifest(config: MarketRegimeConfig) -> ModuleManifest:
    return ModuleManifest(
        name=MODULE_NAME,
        version=MODULE_VERSION,
        api_version=API_VERSION,
        required_timeframes=(config.timeframe,),
        required_features=(),
        optional_features=(),
        produced_features=(
            CURRENT_FEATURE_KEY,
            TREND_FEATURE_KEY,
            VOLATILITY_FEATURE_KEY,
        ),
        warmup_bars={config.timeframe: config.minimum_observations},
        parameters_schema=PARAMETERS_SCHEMA,
        skip_until_ready=False,
        description=(
            "Point-in-time market regime feature producer using EMA direction, "
            "Kaufman efficiency and ATR-relative volatility without trade execution."
        ),
        state_schema_version="2",
    )
