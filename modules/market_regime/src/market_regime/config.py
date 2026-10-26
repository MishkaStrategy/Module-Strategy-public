from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Context, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from .exceptions import MarketRegimeConfigurationError

_TIMEFRAME_RE = re.compile(r"^([1-9]\d*)([smhdw])$")
_MAX_TIMEFRAME_SECONDS = timedelta.max.days * 86_400 + timedelta.max.seconds
DECIMAL_CONTEXT = Context(prec=28)


def to_decimal(value: int | float | str | Decimal, name: str) -> Decimal:
    if isinstance(value, bool):
        raise MarketRegimeConfigurationError(f"{name} must be numeric, not boolean")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketRegimeConfigurationError(f"{name} must be a finite decimal number") from exc
    if not result.is_finite():
        raise MarketRegimeConfigurationError(f"{name} must be finite")
    return result


def canonical_decimal(value: Decimal) -> str:
    """Return a stable, non-exponent decimal string for manifests and hashes."""
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonical_timeframe(timeframe: str) -> str:
    if not isinstance(timeframe, str):
        raise MarketRegimeConfigurationError("timeframe must be a string")
    normalized = timeframe.strip().lower()
    timeframe_seconds(normalized)
    return normalized


def timeframe_seconds(timeframe: str) -> int:
    if not isinstance(timeframe, str):
        raise MarketRegimeConfigurationError("timeframe must be a string")
    match = _TIMEFRAME_RE.fullmatch(timeframe.strip().lower())
    if match is None:
        raise MarketRegimeConfigurationError(
            "timeframe must match '<positive integer><s|m|h|d|w>', e.g. '1h'"
        )
    try:
        amount = int(match.group(1))
    except ValueError as exc:
        raise MarketRegimeConfigurationError(
            "timeframe amount exceeds the supported integer size"
        ) from exc
    seconds = (
        amount
        * {
            "s": 1,
            "m": 60,
            "h": 3_600,
            "d": 86_400,
            "w": 604_800,
        }[match.group(2)]
    )
    if seconds > _MAX_TIMEFRAME_SECONDS:
        raise MarketRegimeConfigurationError(
            "timeframe exceeds the maximum duration supported by datetime"
        )
    return seconds


@dataclass(frozen=True, slots=True)
class MarketRegimeConfig:
    timeframe: str = "1h"
    fast_ema_period: int = 20
    slow_ema_period: int = 50
    atr_period: int = 14
    volatility_baseline_period: int = 50
    efficiency_period: int = 20
    slope_lookback: int = 5
    trend_spread_threshold: Decimal = Decimal("0.001")
    trend_slope_threshold: Decimal = Decimal("0.00015")
    trend_efficiency_threshold: Decimal = Decimal("0.35")
    range_efficiency_threshold: Decimal = Decimal("0.22")
    low_volatility_ratio: Decimal = Decimal("0.75")
    high_volatility_ratio: Decimal = Decimal("1.35")
    regime_confirmation_bars: int = 2
    volatility_confirmation_bars: int = 2
    publish_diagnostics: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", canonical_timeframe(self.timeframe))

        decimal_fields = (
            "trend_spread_threshold",
            "trend_slope_threshold",
            "trend_efficiency_threshold",
            "range_efficiency_threshold",
            "low_volatility_ratio",
            "high_volatility_ratio",
        )
        for field_name in decimal_fields:
            object.__setattr__(
                self,
                field_name,
                to_decimal(getattr(self, field_name), field_name),
            )

        integer_ranges = {
            "fast_ema_period": (2, 10_000),
            "slow_ema_period": (3, 20_000),
            "atr_period": (2, 10_000),
            "volatility_baseline_period": (5, 50_000),
            "efficiency_period": (2, 10_000),
            "slope_lookback": (1, 10_000),
            "regime_confirmation_bars": (1, 100),
            "volatility_confirmation_bars": (1, 100),
        }
        for field_name, (minimum, maximum) in integer_ranges.items():
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise MarketRegimeConfigurationError(
                    f"{field_name} must be an integer in [{minimum}, {maximum}]"
                )

        if self.fast_ema_period >= self.slow_ema_period:
            raise MarketRegimeConfigurationError(
                "fast_ema_period must be less than slow_ema_period"
            )
        if not Decimal("0") <= self.trend_spread_threshold <= Decimal("1"):
            raise MarketRegimeConfigurationError("trend_spread_threshold must be in [0, 1]")
        if not Decimal("0") <= self.trend_slope_threshold <= Decimal("1"):
            raise MarketRegimeConfigurationError("trend_slope_threshold must be in [0, 1]")
        if not (
            Decimal("0")
            <= self.range_efficiency_threshold
            < self.trend_efficiency_threshold
            <= Decimal("1")
        ):
            raise MarketRegimeConfigurationError(
                "efficiency thresholds must satisfy 0 <= range < trend <= 1"
            )
        if not Decimal("0") < self.low_volatility_ratio < Decimal("1"):
            raise MarketRegimeConfigurationError("low_volatility_ratio must be in (0, 1)")
        if not Decimal("1") < self.high_volatility_ratio <= Decimal("1000"):
            raise MarketRegimeConfigurationError("high_volatility_ratio must be in (1, 1000]")
        if not isinstance(self.publish_diagnostics, bool):
            raise MarketRegimeConfigurationError("publish_diagnostics must be boolean")

    @property
    def target_seconds(self) -> int:
        return timeframe_seconds(self.timeframe)

    @property
    def indicator_ready_observations(self) -> int:
        """Exact bars required before all raw indicator metrics are available."""
        return max(
            self.slow_ema_period,
            self.slope_lookback + 1,
            self.atr_period + self.volatility_baseline_period,
            self.efficiency_period + 1,
        )

    @property
    def minimum_observations(self) -> int:
        return (
            self.indicator_ready_observations
            + max(
                self.regime_confirmation_bars,
                self.volatility_confirmation_bars,
            )
            - 1
        )

    def as_parameters(self) -> Mapping[str, object]:
        values = self._parameter_dict(include_diagnostics=True)
        return MappingProxyType(values)

    def semantic_parameters(self) -> Mapping[str, object]:
        """Parameters that can change features or state transitions."""
        return MappingProxyType(self._parameter_dict(include_diagnostics=False))

    def _parameter_dict(self, *, include_diagnostics: bool) -> dict[str, object]:
        values: dict[str, object] = {
            "timeframe": self.timeframe,
            "fast_ema_period": self.fast_ema_period,
            "slow_ema_period": self.slow_ema_period,
            "atr_period": self.atr_period,
            "volatility_baseline_period": self.volatility_baseline_period,
            "efficiency_period": self.efficiency_period,
            "slope_lookback": self.slope_lookback,
            "trend_spread_threshold": canonical_decimal(self.trend_spread_threshold),
            "trend_slope_threshold": canonical_decimal(self.trend_slope_threshold),
            "trend_efficiency_threshold": canonical_decimal(self.trend_efficiency_threshold),
            "range_efficiency_threshold": canonical_decimal(self.range_efficiency_threshold),
            "low_volatility_ratio": canonical_decimal(self.low_volatility_ratio),
            "high_volatility_ratio": canonical_decimal(self.high_volatility_ratio),
            "regime_confirmation_bars": self.regime_confirmation_bars,
            "volatility_confirmation_bars": self.volatility_confirmation_bars,
        }
        if include_diagnostics:
            values["publish_diagnostics"] = self.publish_diagnostics
        return values

    @staticmethod
    def _hash_parameters(parameters: Mapping[str, object]) -> str:
        encoded = json.dumps(dict(parameters), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def signature(self) -> str:
        return self._hash_parameters(self.semantic_parameters())

    @property
    def legacy_signature(self) -> str:
        """v1.0/v1.1 signature for the current diagnostics setting."""
        return self._hash_parameters(self.as_parameters())

    @property
    def compatible_signatures(self) -> frozenset[str]:
        """Current semantic hash plus both legacy diagnostics variants."""
        legacy_true = self._parameter_dict(include_diagnostics=False)
        legacy_true["publish_diagnostics"] = True
        legacy_false = self._parameter_dict(include_diagnostics=False)
        legacy_false["publish_diagnostics"] = False
        return frozenset(
            {
                self.signature,
                self._hash_parameters(legacy_true),
                self._hash_parameters(legacy_false),
            }
        )
