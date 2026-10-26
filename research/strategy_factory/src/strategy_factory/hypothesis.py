"""Evidence-based regime descriptors and deterministic hypothesis generation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .canonical import canonical_sha256
from .contracts import (
    InstrumentProfile,
    MarketRegimeDefinition,
    ModuleCategory,
    ModuleReference,
    StrategyFactoryRequest,
    StrategyHypothesis,
)
from .registry import StrategyModuleCatalog


class RegimeLibrary:
    """Publish point-in-time regime definitions; it does not label with future data."""

    @staticmethod
    def default(timeframes: tuple[str, ...]) -> tuple[MarketRegimeDefinition, ...]:
        base = timeframes[0]
        context = timeframes[-1]
        raw = (
            (
                "uptrend",
                "Uptrend",
                "Completed close above a lagged EMA with positive EMA slope.",
                (base, context),
                {"ema_period": 200, "slope_lag_bars": 4, "direction": "up"},
            ),
            (
                "downtrend",
                "Downtrend",
                "Completed close below a lagged EMA with negative EMA slope.",
                (base, context),
                {"ema_period": 200, "slope_lag_bars": 4, "direction": "down"},
            ),
            (
                "sideways",
                "Sideways",
                "Low normalized EMA slope and bounded directional efficiency.",
                (base,),
                {"ema_period": 50, "maximum_slope_atr": "0.10"},
            ),
            (
                "low_volatility",
                "Low volatility",
                "Lagged ATR percentile below the expanding historical threshold.",
                (base,),
                {"atr_period": 14, "expanding_quantile": "0.25"},
            ),
            (
                "normal_volatility",
                "Normal volatility",
                "Lagged ATR percentile in the central expanding distribution.",
                (base,),
                {"atr_period": 14, "lower_quantile": "0.25", "upper_quantile": "0.75"},
            ),
            (
                "high_volatility",
                "High volatility",
                "Lagged ATR percentile above the expanding historical threshold.",
                (base,),
                {"atr_period": 14, "expanding_quantile": "0.75"},
            ),
            (
                "impulse",
                "Impulse",
                "Completed-bar range expansion with directional close strength.",
                (base,),
                {"range_atr_minimum": "1.5", "close_strength_minimum": "0.75"},
            ),
            (
                "mean_reversion",
                "Mean reversion",
                "Weak directional efficiency with negative lagged return autocorrelation.",
                (base,),
                {"autocorrelation_window": 100, "maximum_value": "-0.05"},
            ),
            (
                "crisis",
                "Crisis",
                "Extreme lagged volatility and tail-loss conditions using expanding history.",
                (base, context),
                {"atr_quantile": "0.95", "return_quantile": "0.01"},
            ),
            (
                "insufficient_liquidity",
                "Insufficient liquidity",
                "Lagged volume and notional are below configured rolling thresholds.",
                (base,),
                {"volume_window": 100, "minimum_quantile": "0.10"},
            ),
        )
        output: list[MarketRegimeDefinition] = []
        for regime_id, name, description, required, parameters in raw:
            output.append(
                MarketRegimeDefinition(
                    regime_id=regime_id,
                    name=name,
                    description=description,
                    feature_version="1.0",
                    point_in_time=True,
                    required_timeframes=tuple(dict.fromkeys(required)),
                    parameters=parameters,
                )
            )
        return tuple(output)


class HypothesisGenerator:
    """Generate falsifiable hypotheses from profile evidence and registered modules."""

    def __init__(self, catalog: StrategyModuleCatalog) -> None:
        self._catalog = catalog

    def generate(
        self,
        *,
        request: StrategyFactoryRequest,
        profile: InstrumentProfile,
    ) -> tuple[StrategyHypothesis, ...]:
        signals = self._catalog.by_category(ModuleCategory.SIGNAL)
        hypotheses: list[StrategyHypothesis] = []
        for family in sorted(request.allowed_strategy_families):
            candidates = tuple(item for item in signals if _module_matches_family(item, family))
            if not candidates:
                continue
            evidence = _family_evidence(family, profile)
            if evidence["eligible"] is False:
                continue
            seed = {
                "request": request.fingerprint,
                "profile": profile.profile_id,
                "family": family,
                "modules": tuple(item.key for item in candidates),
                "evidence": evidence,
            }
            hypotheses.append(
                StrategyHypothesis(
                    hypothesis_id=f"hyp-{canonical_sha256(seed)[:24]}",
                    description=f"Test the {family} family on {request.symbol}.",
                    expected_edge=str(evidence["expected_edge"]),
                    applicability=tuple(str(item) for item in evidence["applicability"]),
                    risks=tuple(str(item) for item in evidence["risks"]),
                    module_candidates=candidates,
                    parameter_ranges={item.key: dict(item.parameter_schema) for item in candidates},
                    falsification_criteria={
                        "minimum_trade_count": request.minimum_trade_count,
                        "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
                        "must_survive_cost_stress": True,
                        "must_survive_walk_forward": True,
                        "holdout_not_used_for_selection": True,
                    },
                    estimated_experiments=min(
                        request.budget.maximum_experiments,
                        max(1, 32 * len(candidates)),
                    ),
                    novelty=Decimal("0.25") if request.baseline_strategy_id else Decimal("0.50"),
                    origin={
                        "generator": "deterministic-profile-rules-v1",
                        "profile_id": profile.profile_id,
                        "family": family,
                        "evidence": evidence,
                        "baseline_strategy_id": request.baseline_strategy_id,
                    },
                )
            )
        return tuple(sorted(hypotheses, key=lambda item: item.hypothesis_id))


def _module_matches_family(module: ModuleReference, family: str) -> bool:
    normalized = family.lower().replace("-", "_")
    module_id = module.module_id.lower().replace("-", "_")
    tags = {item.lower().replace("-", "_") for item in module.compatibility_tags}
    aliases = {
        "trend_continuation": {"trend", "momentum", "pullback", "fvg"},
        "volatility_expansion": {"breakout", "range_breakout", "volatility"},
        "mean_reversion": {"mean_reversion", "liquidity_sweep"},
    }
    accepted = {normalized, *aliases.get(normalized, set())}
    return any(item in module_id for item in accepted) or bool(tags & accepted)


def _family_evidence(family: str, profile: InstrumentProfile) -> dict[str, Any]:
    autocorrelation = _number(profile.mean_reversion_metrics.get("lag1_autocorrelation"))
    directional = _number(profile.trend_metrics.get("directional_imbalance"))
    volatility = _number(profile.volatility_metrics.get("return_std"))
    normalized = family.lower().replace("-", "_")
    if normalized in {"mean_reversion", "liquidity_sweep"}:
        return {
            "eligible": autocorrelation is None or autocorrelation <= 0.05,
            "expected_edge": "short-horizon price reversion after statistically unusual displacement",
            "applicability": ("sideways", "normal_volatility"),
            "risks": ("trend persistence", "cost sensitivity", "tail continuation"),
            "lag1_autocorrelation": autocorrelation,
        }
    if normalized in {"breakout", "range_breakout", "volatility_expansion"}:
        return {
            "eligible": volatility is None or volatility > 0,
            "expected_edge": "continuation after compression or confirmed structural break",
            "applicability": ("impulse", "high_volatility", "trend"),
            "risks": ("false breakout", "slippage", "crowded exits"),
            "return_std": volatility,
        }
    if normalized in {"trend", "trend_continuation", "momentum", "pullback", "fvg"}:
        return {
            "eligible": directional is None or abs(directional) <= 1,
            "expected_edge": "continuation or retest inside a point-in-time directional regime",
            "applicability": ("uptrend", "downtrend", "impulse"),
            "risks": ("regime reversal", "late entry", "parameter instability"),
            "directional_imbalance": directional,
        }
    return {
        "eligible": True,
        "expected_edge": "registered family-specific behavior requiring empirical falsification",
        "applicability": ("profile-dependent",),
        "risks": ("insufficient evidence", "multiple testing"),
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        return float(value)
    except ValueError:
        return None
