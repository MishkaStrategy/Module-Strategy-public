from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType

from .enums import PivotMethod, PriceBasis, TiePolicy
from .exceptions import WaveAnalysisConfigurationError

_TIMEFRAME_RE = re.compile(r"^([1-9]\d*)([smhdw])$")
_FEATURE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$")


def to_decimal(value: int | float | str | Decimal, name: str) -> Decimal:
    if isinstance(value, bool):
        raise WaveAnalysisConfigurationError(f"{name} must be numeric, not boolean")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise WaveAnalysisConfigurationError(f"{name} must be a finite decimal number") from exc
    if not result.is_finite():
        raise WaveAnalysisConfigurationError(f"{name} must be finite")
    return result


def canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def timeframe_seconds(timeframe: str) -> int:
    if not isinstance(timeframe, str):
        raise WaveAnalysisConfigurationError("timeframe must be a string")
    match = _TIMEFRAME_RE.fullmatch(timeframe.strip().lower())
    if match is None:
        raise WaveAnalysisConfigurationError("timeframe must match '<positive integer><s|m|h|d|w>', e.g. '15m'")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def _enum[EnumT: Enum](value: object, enum_type: type[EnumT], name: str) -> EnumT:
    try:
        return value if isinstance(value, enum_type) else enum_type(str(value))
    except ValueError as exc:
        raise WaveAnalysisConfigurationError(f"{name} must be one of: {', '.join(i.value for i in enum_type)}") from exc


@dataclass(frozen=True, slots=True)
class WaveAnalysisConfig:
    timeframes: tuple[str, ...] = ("15m",)
    primary_timeframe: str = "15m"
    pivot_method: PivotMethod = PivotMethod.FRACTAL
    left_bars: int = 3
    right_bars: int = 3
    tie_policy: TiePolicy = TiePolicy.EARLIEST
    price_basis: PriceBasis = PriceBasis.HIGH_LOW
    minimum_price_movement: Decimal = Decimal("0")
    minimum_leg_bars: int = 1
    reversal_threshold: Decimal = Decimal("0.01")
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("2")
    minimum_atr_threshold: Decimal = Decimal("0")
    maximum_atr_threshold: Decimal = Decimal("1000000000")
    minimum_absolute_movement: Decimal = Decimal("0")
    hybrid_volatility_adjustment: Decimal = Decimal("0.25")
    equal_level_tolerance: Decimal = Decimal("0.0005")
    impulse_atr_threshold: Decimal = Decimal("1.5")
    impulse_efficiency_threshold: Decimal = Decimal("0.55")
    expansion_atr_threshold: Decimal = Decimal("2.5")
    range_efficiency_threshold: Decimal = Decimal("0.25")
    exhaustion_efficiency_threshold: Decimal = Decimal("0.35")
    elliott_enabled: bool = False
    maximum_hypotheses: int = 5
    hypothesis_min_confidence: Decimal = Decimal("0.35")
    maximum_retained_bars: int = 4096
    maximum_retained_pivots: int = 256
    maximum_retained_waves: int = 255
    maximum_retained_atr_values: int = 128
    feature_snapshot_limit: int = 32
    recovery_bars: int = 0
    publish_provisional: bool = True
    publish_diagnostics: bool = True
    optional_market_regime_feature: str = "market_regime.current"
    _parameters: Mapping[str, object] = field(init=False, repr=False, compare=False)
    _signature: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        raw_timeframes: object = self.timeframes
        if isinstance(raw_timeframes, (str, bytes)):
            raise WaveAnalysisConfigurationError("timeframes must be a sequence of timeframe strings, not a scalar")
        normalized = []
        for item in self.timeframes:
            if not isinstance(item, str):
                raise WaveAnalysisConfigurationError("every timeframe must be a string")
            value = item.strip().lower()
            timeframe_seconds(value)
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise WaveAnalysisConfigurationError("at least one timeframe is required")
        normalized.sort(key=timeframe_seconds)
        object.__setattr__(self, "timeframes", tuple(normalized))
        if not isinstance(self.primary_timeframe, str):
            raise WaveAnalysisConfigurationError("primary_timeframe must be a string")
        primary = self.primary_timeframe.strip().lower()
        timeframe_seconds(primary)
        if primary not in normalized:
            raise WaveAnalysisConfigurationError("primary_timeframe must be present in timeframes")
        object.__setattr__(self, "primary_timeframe", primary)
        object.__setattr__(self, "pivot_method", _enum(self.pivot_method, PivotMethod, "pivot_method"))
        object.__setattr__(self, "tie_policy", _enum(self.tie_policy, TiePolicy, "tie_policy"))
        object.__setattr__(self, "price_basis", _enum(self.price_basis, PriceBasis, "price_basis"))
        dec = (
            "minimum_price_movement",
            "reversal_threshold",
            "atr_multiplier",
            "minimum_atr_threshold",
            "maximum_atr_threshold",
            "minimum_absolute_movement",
            "hybrid_volatility_adjustment",
            "equal_level_tolerance",
            "impulse_atr_threshold",
            "impulse_efficiency_threshold",
            "expansion_atr_threshold",
            "range_efficiency_threshold",
            "exhaustion_efficiency_threshold",
            "hypothesis_min_confidence",
        )
        for n in dec:
            object.__setattr__(self, n, to_decimal(getattr(self, n), n))
        ranges = {
            "left_bars": (1, 10000),
            "right_bars": (1, 10000),
            "minimum_leg_bars": (1, 100000),
            "atr_period": (2, 100000),
            "maximum_hypotheses": (1, 100),
            "maximum_retained_bars": (16, 1000000),
            "maximum_retained_pivots": (4, 100000),
            "maximum_retained_waves": (3, 100000),
            "maximum_retained_atr_values": (2, 100000),
            "feature_snapshot_limit": (1, 1000),
            "recovery_bars": (0, 100000),
        }
        for n, (lo, hi) in ranges.items():
            v = getattr(self, n)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise WaveAnalysisConfigurationError(f"{n} must be an integer in [{lo}, {hi}]")
        for n in (
            "minimum_price_movement",
            "minimum_atr_threshold",
            "minimum_absolute_movement",
            "hybrid_volatility_adjustment",
            "equal_level_tolerance",
        ):
            if getattr(self, n) < 0:
                raise WaveAnalysisConfigurationError(f"{n} must be non-negative")
        for n in (
            "reversal_threshold",
            "atr_multiplier",
            "maximum_atr_threshold",
            "impulse_atr_threshold",
            "expansion_atr_threshold",
        ):
            if getattr(self, n) <= 0:
                raise WaveAnalysisConfigurationError(f"{n} must be positive")
        for n in (
            "impulse_efficiency_threshold",
            "range_efficiency_threshold",
            "exhaustion_efficiency_threshold",
            "hypothesis_min_confidence",
        ):
            if not Decimal("0") <= getattr(self, n) <= Decimal("1"):
                raise WaveAnalysisConfigurationError(f"{n} must be in [0, 1]")
        if self.minimum_atr_threshold > self.maximum_atr_threshold:
            raise WaveAnalysisConfigurationError("minimum_atr_threshold cannot exceed maximum_atr_threshold")
        if self.expansion_atr_threshold < self.impulse_atr_threshold:
            raise WaveAnalysisConfigurationError("expansion_atr_threshold cannot be smaller than impulse_atr_threshold")
        if not (
            self.range_efficiency_threshold <= self.exhaustion_efficiency_threshold <= self.impulse_efficiency_threshold
        ):
            raise WaveAnalysisConfigurationError("efficiency thresholds must satisfy range <= exhaustion <= impulse")
        if self.maximum_retained_waves >= self.maximum_retained_pivots:
            raise WaveAnalysisConfigurationError("maximum_retained_waves must be smaller than maximum_retained_pivots")
        if self.maximum_retained_bars < self.warmup_bars:
            raise WaveAnalysisConfigurationError("maximum_retained_bars must be at least the required warm-up")
        for n in ("elliott_enabled", "publish_provisional", "publish_diagnostics"):
            if not isinstance(getattr(self, n), bool):
                raise WaveAnalysisConfigurationError(f"{n} must be boolean")
        if not isinstance(self.optional_market_regime_feature, str) or not _FEATURE_KEY_RE.fullmatch(
            self.optional_market_regime_feature
        ):
            raise WaveAnalysisConfigurationError(
                "optional_market_regime_feature must be a namespaced feature key without whitespace"
            )
        p = MappingProxyType(self._parameter_values())
        enc = json.dumps(dict(p), sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "_parameters", p)
        object.__setattr__(self, "_signature", hashlib.sha256(enc).hexdigest())

    @property
    def warmup_bars(self) -> int:
        detector = (
            self.left_bars + self.right_bars + 1 if self.pivot_method is PivotMethod.FRACTAL else self.atr_period + 2
        )
        return max(detector, self.minimum_leg_bars + 2)

    @property
    def effective_recovery_bars(self) -> int:
        return self.recovery_bars or self.warmup_bars

    def _parameter_values(self) -> dict[str, object]:
        return {
            "timeframes": list(self.timeframes),
            "primary_timeframe": self.primary_timeframe,
            "pivot_method": self.pivot_method.value,
            "left_bars": self.left_bars,
            "right_bars": self.right_bars,
            "tie_policy": self.tie_policy.value,
            "price_basis": self.price_basis.value,
            "minimum_price_movement": canonical_decimal(self.minimum_price_movement),
            "minimum_leg_bars": self.minimum_leg_bars,
            "reversal_threshold": canonical_decimal(self.reversal_threshold),
            "atr_period": self.atr_period,
            "atr_multiplier": canonical_decimal(self.atr_multiplier),
            "minimum_atr_threshold": canonical_decimal(self.minimum_atr_threshold),
            "maximum_atr_threshold": canonical_decimal(self.maximum_atr_threshold),
            "minimum_absolute_movement": canonical_decimal(self.minimum_absolute_movement),
            "hybrid_volatility_adjustment": canonical_decimal(self.hybrid_volatility_adjustment),
            "equal_level_tolerance": canonical_decimal(self.equal_level_tolerance),
            "impulse_atr_threshold": canonical_decimal(self.impulse_atr_threshold),
            "impulse_efficiency_threshold": canonical_decimal(self.impulse_efficiency_threshold),
            "expansion_atr_threshold": canonical_decimal(self.expansion_atr_threshold),
            "range_efficiency_threshold": canonical_decimal(self.range_efficiency_threshold),
            "exhaustion_efficiency_threshold": canonical_decimal(self.exhaustion_efficiency_threshold),
            "elliott_enabled": self.elliott_enabled,
            "maximum_hypotheses": self.maximum_hypotheses,
            "hypothesis_min_confidence": canonical_decimal(self.hypothesis_min_confidence),
            "maximum_retained_bars": self.maximum_retained_bars,
            "maximum_retained_pivots": self.maximum_retained_pivots,
            "maximum_retained_waves": self.maximum_retained_waves,
            "maximum_retained_atr_values": self.maximum_retained_atr_values,
            "feature_snapshot_limit": self.feature_snapshot_limit,
            "recovery_bars": self.recovery_bars,
            "publish_provisional": self.publish_provisional,
            "publish_diagnostics": self.publish_diagnostics,
            "optional_market_regime_feature": self.optional_market_regime_feature,
        }

    def as_parameters(self) -> Mapping[str, object]:
        return self._parameters

    @property
    def signature(self) -> str:
        return self._signature
