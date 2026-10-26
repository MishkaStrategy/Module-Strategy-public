from collections.abc import Mapping
from typing import Any

from modular_core import ModuleManifest

from .config import WaveAnalysisConfig
from .contracts import PRODUCED_FEATURES

MODULE_NAME = "wave_analysis"
MODULE_VERSION = "0.2.0"
API_VERSION = "1.0"
PARAMETERS_SCHEMA: Mapping[str, Mapping[str, Any]] = {
    "timeframes": {"type": "array", "default": ["15m"]},
    "primary_timeframe": {"type": "string", "default": "15m"},
    "pivot_method": {
        "type": "string",
        "enum": ["fractal", "percentage_zigzag", "atr_zigzag", "hybrid_adaptive"],
        "default": "fractal",
    },
    "left_bars": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 3},
    "right_bars": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 3},
    "tie_policy": {"type": "string", "enum": ["earliest", "latest", "reject"], "default": "earliest"},
    "price_basis": {"type": "string", "enum": ["close", "high_low"], "default": "high_low"},
    "minimum_price_movement": {"type": "string", "default": "0"},
    "minimum_leg_bars": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 1},
    "reversal_threshold": {"type": "string", "default": "0.01"},
    "atr_period": {"type": "integer", "minimum": 2, "maximum": 100000, "default": 14},
    "atr_multiplier": {"type": "string", "default": "2"},
    "minimum_atr_threshold": {"type": "string", "default": "0"},
    "maximum_atr_threshold": {"type": "string", "default": "1000000000"},
    "minimum_absolute_movement": {"type": "string", "default": "0"},
    "hybrid_volatility_adjustment": {"type": "string", "default": "0.25"},
    "equal_level_tolerance": {"type": "string", "default": "0.0005"},
    "impulse_atr_threshold": {"type": "string", "default": "1.5"},
    "impulse_efficiency_threshold": {"type": "string", "default": "0.55"},
    "expansion_atr_threshold": {"type": "string", "default": "2.5"},
    "range_efficiency_threshold": {"type": "string", "default": "0.25"},
    "exhaustion_efficiency_threshold": {"type": "string", "default": "0.35"},
    "elliott_enabled": {"type": "boolean", "default": False},
    "maximum_hypotheses": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
    "hypothesis_min_confidence": {"type": "string", "default": "0.35"},
    "maximum_retained_bars": {"type": "integer", "minimum": 16, "maximum": 1000000, "default": 4096},
    "maximum_retained_pivots": {"type": "integer", "minimum": 4, "maximum": 100000, "default": 256},
    "maximum_retained_waves": {"type": "integer", "minimum": 3, "maximum": 100000, "default": 255},
    "maximum_retained_atr_values": {"type": "integer", "minimum": 2, "maximum": 100000, "default": 128},
    "feature_snapshot_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 32},
    "recovery_bars": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
    "publish_provisional": {"type": "boolean", "default": True},
    "publish_diagnostics": {"type": "boolean", "default": True},
    "optional_market_regime_feature": {"type": "string", "default": "market_regime.current"},
}


def build_manifest(config: WaveAnalysisConfig) -> ModuleManifest:
    return ModuleManifest(
        name=MODULE_NAME,
        version=MODULE_VERSION,
        api_version=API_VERSION,
        required_timeframes=config.timeframes,
        required_features=(),
        optional_features=(config.optional_market_regime_feature,),
        produced_features=PRODUCED_FEATURES,
        warmup_bars={t: config.warmup_bars for t in config.timeframes},
        parameters_schema=PARAMETERS_SCHEMA,
        skip_until_ready=False,
        description="Deterministic non-repainting pivot, wave-leg and market-structure feature producer. Optional Elliott-like output is probabilistic and never emits orders.",
        state_schema_version="1",
    )
