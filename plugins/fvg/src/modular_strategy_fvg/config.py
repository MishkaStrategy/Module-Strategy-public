from __future__ import annotations

import re
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence, TypeVar

from .decimal_utils import canonical_decimal_text, deterministic_decimal
from .timeframes import timeframe_seconds

_TIMEFRAME_RE = re.compile(r"^[1-9][0-9]*[smhdw]$")
_DEFAULT_MAX_ACTIVE_ZONES = 4096
_DEFAULT_MAX_STATE_BYTES = 8 * 1024 * 1024


class FvgDirectionFilter(str, Enum):
    BOTH = "both"
    BULLISH = "bullish"
    BEARISH = "bearish"


class FvgInvalidationLevel(str, Enum):
    FIRST_TOUCH = "first_touch"
    MIDPOINT = "midpoint"
    FULL_FILL = "full_fill"


class FvgInvalidationBasis(str, Enum):
    WICK = "wick"
    CLOSE = "close"


class FvgOrderViolationPolicy(str, Enum):
    RAISE = "raise"
    RESET_AND_SKIP = "reset_and_skip"


class FvgGapSizeMode(str, Enum):
    LEGACY = "legacy"
    ABSOLUTE = "absolute"
    FRACTION = "fraction"
    BPS = "bps"
    TICKS = "ticks"
    ATR = "atr"
    B_RANGE = "b_range"
    AVERAGE_RANGE = "average_range"


class FvgFilterTarget(str, Enum):
    CONFIRMED = "confirmed"
    PRE_FVG = "pre_fvg"
    BOTH = "both"


class FvgPriceFilterMode(str, Enum):
    DISABLED = "disabled"
    ABSOLUTE = "absolute"
    CURRENT_FRACTION = "current_fraction"
    ANCHOR_FRACTION = "anchor_fraction"


class FvgPriceMatchPolicy(str, Enum):
    MIDPOINT = "midpoint"
    OVERLAP = "overlap"
    CONTAINED = "contained"


class FvgVolumeFilterMode(str, Enum):
    DISABLED = "disabled"
    B_TO_SMA = "b_to_sma"
    B_TO_EMA = "b_to_ema"
    B_TO_MEDIAN = "b_to_median"
    ABC_TO_SMA = "abc_to_sma"
    PERCENTILE = "percentile"


class FvgMissingDataPolicy(str, Enum):
    REJECT = "reject"
    ALLOW = "allow"
    ERROR = "error"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _strict_enum(value: object, enum_type: type[_EnumT], name: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if type(value) is not str:
        raise TypeError(f"{name} must be a {enum_type.__name__} or canonical string")
    return enum_type(value)


def _decimal(value: str | int | float | Decimal, name: str) -> Decimal:
    if type(value) not in (str, int, float, Decimal):
        raise TypeError(f"{name} must be a canonical decimal-compatible value")
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal-compatible value") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _optional_decimal(value: str | int | float | Decimal | None, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else canonical_decimal_text(value)


def _strict_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool")
    return value


def _bounded_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _normalize_timeframes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("timeframes must be a sequence of timeframe strings, not a string")
    if not isinstance(values, SequenceABC):
        raise TypeError("timeframes must be an ordered sequence of timeframe strings")
    if any(type(value) is not str for value in values):
        raise TypeError("timeframes must contain only canonical strings")
    result = tuple(value.strip().lower() for value in values)
    if not result:
        raise ValueError("at least one timeframe is required")
    if len(set(result)) != len(result):
        raise ValueError("timeframes must be unique")
    invalid = [value for value in result if not _TIMEFRAME_RE.fullmatch(value)]
    if invalid:
        raise ValueError(f"unsupported timeframe(s): {', '.join(invalid)}")
    for value in result:
        timeframe_seconds(value)
    return result


@dataclass(frozen=True, slots=True)
class FvgConfig:
    timeframes: tuple[str, ...] = ("15m", "4h")
    direction_filter: FvgDirectionFilter = FvgDirectionFilter.BOTH
    min_gap_absolute: Decimal = Decimal("0")
    min_gap_fraction: Decimal = Decimal("0")
    invalidation_level: FvgInvalidationLevel = FvgInvalidationLevel.FULL_FILL
    invalidation_basis: FvgInvalidationBasis = FvgInvalidationBasis.WICK
    require_consecutive_bars: bool = True
    order_violation_policy: FvgOrderViolationPolicy = FvgOrderViolationPolicy.RAISE
    publish_lifecycle_events: bool = True
    max_active_zones_per_timeframe: int = _DEFAULT_MAX_ACTIVE_ZONES
    max_state_bytes: int = _DEFAULT_MAX_STATE_BYTES

    gap_size_mode: FvgGapSizeMode = FvgGapSizeMode.LEGACY
    min_gap_size: Decimal = Decimal("0")
    max_gap_size: Decimal | None = None
    tick_size: Decimal | None = None
    atr_period: int = 14
    average_range_period: int = 20

    price_filter_mode: FvgPriceFilterMode = FvgPriceFilterMode.DISABLED
    price_filter_min: Decimal | None = None
    price_filter_max: Decimal | None = None
    price_filter_anchor: Decimal | None = None
    price_filter_lower_fraction: Decimal = Decimal("0")
    price_filter_upper_fraction: Decimal = Decimal("0")
    price_filter_match_policy: FvgPriceMatchPolicy = FvgPriceMatchPolicy.MIDPOINT
    price_filter_target: FvgFilterTarget = FvgFilterTarget.BOTH

    volume_filter_mode: FvgVolumeFilterMode = FvgVolumeFilterMode.DISABLED
    volume_lookback: int = 20
    volume_min_ratio: Decimal = Decimal("0")
    volume_max_ratio: Decimal | None = None
    volume_missing_policy: FvgMissingDataPolicy = FvgMissingDataPolicy.REJECT
    volume_filter_target: FvgFilterTarget = FvgFilterTarget.BOTH

    pre_fvg_enabled: bool = False
    pre_fvg_evaluation_offset: str = "3m"
    publish_evaluation_events: bool = False
    max_zone_age_bars: int = 0
    source_name: str = "market_view"
    timeframe_overrides: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    @deterministic_decimal
    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframes", _normalize_timeframes(self.timeframes))
        for name, enum_type in (
            ("direction_filter", FvgDirectionFilter),
            ("invalidation_level", FvgInvalidationLevel),
            ("invalidation_basis", FvgInvalidationBasis),
            ("order_violation_policy", FvgOrderViolationPolicy),
            ("gap_size_mode", FvgGapSizeMode),
            ("price_filter_mode", FvgPriceFilterMode),
            ("price_filter_match_policy", FvgPriceMatchPolicy),
            ("price_filter_target", FvgFilterTarget),
            ("volume_filter_mode", FvgVolumeFilterMode),
            ("volume_missing_policy", FvgMissingDataPolicy),
            ("volume_filter_target", FvgFilterTarget),
        ):
            object.__setattr__(self, name, _strict_enum(getattr(self, name), enum_type, name))

        for name in (
            "min_gap_absolute",
            "min_gap_fraction",
            "min_gap_size",
            "price_filter_lower_fraction",
            "price_filter_upper_fraction",
            "volume_min_ratio",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in (
            "max_gap_size",
            "tick_size",
            "price_filter_min",
            "price_filter_max",
            "price_filter_anchor",
            "volume_max_ratio",
        ):
            object.__setattr__(self, name, _optional_decimal(getattr(self, name), name))

        for name in (
            "require_consecutive_bars",
            "publish_lifecycle_events",
            "pre_fvg_enabled",
            "publish_evaluation_events",
        ):
            object.__setattr__(self, name, _strict_bool(getattr(self, name), name))
        for name, minimum, maximum in (
            ("max_active_zones_per_timeframe", 1, 100_000),
            ("max_state_bytes", 1024, 64 * 1024 * 1024),
            ("atr_period", 1, 100_000),
            ("average_range_period", 1, 100_000),
            ("volume_lookback", 1, 100_000),
            ("max_zone_age_bars", 0, 10_000_000),
        ):
            object.__setattr__(self, name, _bounded_int(getattr(self, name), name, minimum=minimum, maximum=maximum))

        if type(self.pre_fvg_evaluation_offset) is not str:
            raise TypeError("pre_fvg_evaluation_offset must be a canonical string")
        offset = self.pre_fvg_evaluation_offset.strip().lower()
        timeframe_seconds(offset)
        object.__setattr__(self, "pre_fvg_evaluation_offset", offset)
        if type(self.source_name) is not str:
            raise TypeError("source_name must be a canonical string")
        if not self.source_name.strip():
            raise ValueError("source_name must be a non-empty string")
        object.__setattr__(self, "source_name", self.source_name.strip())
        self._validate_numeric_contracts()
        self._normalize_timeframe_overrides()

    def _validate_numeric_contracts(self) -> None:
        if self.min_gap_absolute < 0:
            raise ValueError("min_gap_absolute must be non-negative")
        if not 0 <= self.min_gap_fraction < 1:
            raise ValueError("min_gap_fraction must be in [0, 1)")
        if self.min_gap_size < 0:
            raise ValueError("min_gap_size must be non-negative")
        if self.max_gap_size is not None and self.max_gap_size < self.min_gap_size:
            raise ValueError("max_gap_size must be greater than or equal to min_gap_size")
        if self.tick_size is not None and self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if self.gap_size_mode is FvgGapSizeMode.TICKS and self.tick_size is None:
            raise ValueError("tick_size is required for ticks gap_size_mode")
        for name in ("price_filter_lower_fraction", "price_filter_upper_fraction"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.price_filter_lower_fraction > 1:
            raise ValueError("price_filter_lower_fraction cannot exceed 1")
        for name in ("price_filter_min", "price_filter_max", "price_filter_anchor"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if (
            self.price_filter_min is not None
            and self.price_filter_max is not None
            and self.price_filter_min > self.price_filter_max
        ):
            raise ValueError("price_filter_min cannot exceed price_filter_max")
        if self.price_filter_mode is FvgPriceFilterMode.ABSOLUTE and (
            self.price_filter_min is None and self.price_filter_max is None
        ):
            raise ValueError("absolute price filter requires price_filter_min or price_filter_max")
        if self.price_filter_mode is FvgPriceFilterMode.ANCHOR_FRACTION and self.price_filter_anchor is None:
            raise ValueError("anchor_fraction price filter requires price_filter_anchor")
        if self.volume_min_ratio < 0:
            raise ValueError("volume_min_ratio must be non-negative")
        if self.volume_max_ratio is not None and self.volume_max_ratio < self.volume_min_ratio:
            raise ValueError("volume_max_ratio must be greater than or equal to volume_min_ratio")
        if self.volume_filter_mode is FvgVolumeFilterMode.PERCENTILE:
            if self.volume_min_ratio > 1:
                raise ValueError("percentile volume_min_ratio must be in [0, 1]")
            if self.volume_max_ratio is not None and self.volume_max_ratio > 1:
                raise ValueError("percentile volume_max_ratio must be in [0, 1]")
        if self.pre_fvg_enabled:
            offset_seconds = timeframe_seconds(self.pre_fvg_evaluation_offset)
            if any(offset_seconds >= timeframe_seconds(timeframe) for timeframe in self.timeframes):
                raise ValueError("pre_fvg_evaluation_offset must be shorter than every configured timeframe")

    def _normalize_timeframe_overrides(self) -> None:
        if not isinstance(self.timeframe_overrides, Mapping):
            raise TypeError("timeframe_overrides must be a mapping")
        allowed = {
            "gap_size_mode",
            "min_gap_size",
            "max_gap_size",
            "tick_size",
            "atr_period",
            "average_range_period",
            "price_filter_mode",
            "price_filter_min",
            "price_filter_max",
            "price_filter_anchor",
            "price_filter_lower_fraction",
            "price_filter_upper_fraction",
            "price_filter_match_policy",
            "price_filter_target",
            "volume_filter_mode",
            "volume_lookback",
            "volume_min_ratio",
            "volume_max_ratio",
            "volume_missing_policy",
            "volume_filter_target",
        }
        normalized: dict[str, Mapping[str, object]] = {}
        for raw_timeframe, raw_values in self.timeframe_overrides.items():
            if type(raw_timeframe) is not str:
                raise TypeError("timeframe override keys must be canonical strings")
            timeframe = raw_timeframe.strip().lower()
            if timeframe not in self.timeframes:
                raise ValueError(f"timeframe override uses unconfigured timeframe: {timeframe}")
            if timeframe in normalized:
                raise ValueError(f"duplicate timeframe override after normalization: {timeframe}")
            if not isinstance(raw_values, Mapping):
                raise TypeError("each timeframe override must be a mapping")
            if any(type(name) is not str for name in raw_values):
                raise TypeError("timeframe override field names must be canonical strings")
            unknown = set(raw_values) - allowed
            if unknown:
                raise ValueError(f"unsupported timeframe override fields: {sorted(unknown)}")
            effective = replace(self, timeframe_overrides={}, **dict(raw_values))
            effective_parameters = effective.as_parameters()
            canonical = {name: effective_parameters[name] for name in sorted(raw_values)}
            normalized[timeframe] = MappingProxyType(canonical)
        object.__setattr__(self, "timeframe_overrides", MappingProxyType(dict(sorted(normalized.items()))))

    def for_timeframe(self, timeframe: str) -> "FvgConfig":
        if type(timeframe) is not str:
            raise TypeError("timeframe must be a canonical string")
        canonical = timeframe.strip().lower()
        if canonical not in self.timeframes:
            raise ValueError(f"unconfigured timeframe: {canonical}")
        overrides = self.timeframe_overrides.get(canonical)
        return self if overrides is None else replace(self, timeframe_overrides={}, **dict(overrides))

    @property
    def requires_confirmed_evaluation(self) -> bool:
        """Return whether confirmed windows need the rich point-in-time evaluator."""
        if self.publish_evaluation_events or self.pre_fvg_enabled:
            return True
        if self.gap_size_mode is not FvgGapSizeMode.LEGACY:
            return True
        if self.price_filter_mode is not FvgPriceFilterMode.DISABLED and self.price_filter_target in {
            FvgFilterTarget.BOTH,
            FvgFilterTarget.CONFIRMED,
        }:
            return True
        return self.volume_filter_mode is not FvgVolumeFilterMode.DISABLED and self.volume_filter_target in {
            FvgFilterTarget.BOTH,
            FvgFilterTarget.CONFIRMED,
        }

    @classmethod
    def from_parameters(cls, **parameters: object) -> "FvgConfig":
        return cls(**parameters)  # type: ignore[arg-type]

    def as_parameters(self) -> Mapping[str, object]:
        values: dict[str, object] = {
            "timeframes": self.timeframes,
            "direction_filter": self.direction_filter.value,
            "min_gap_absolute": canonical_decimal_text(self.min_gap_absolute),
            "min_gap_fraction": canonical_decimal_text(self.min_gap_fraction),
            "invalidation_level": self.invalidation_level.value,
            "invalidation_basis": self.invalidation_basis.value,
            "require_consecutive_bars": self.require_consecutive_bars,
            "order_violation_policy": self.order_violation_policy.value,
            "publish_lifecycle_events": self.publish_lifecycle_events,
            "max_active_zones_per_timeframe": self.max_active_zones_per_timeframe,
            "max_state_bytes": self.max_state_bytes,
            "gap_size_mode": self.gap_size_mode.value,
            "min_gap_size": canonical_decimal_text(self.min_gap_size),
            "max_gap_size": _optional_decimal_text(self.max_gap_size),
            "tick_size": _optional_decimal_text(self.tick_size),
            "atr_period": self.atr_period,
            "average_range_period": self.average_range_period,
            "price_filter_mode": self.price_filter_mode.value,
            "price_filter_min": _optional_decimal_text(self.price_filter_min),
            "price_filter_max": _optional_decimal_text(self.price_filter_max),
            "price_filter_anchor": _optional_decimal_text(self.price_filter_anchor),
            "price_filter_lower_fraction": canonical_decimal_text(self.price_filter_lower_fraction),
            "price_filter_upper_fraction": canonical_decimal_text(self.price_filter_upper_fraction),
            "price_filter_match_policy": self.price_filter_match_policy.value,
            "price_filter_target": self.price_filter_target.value,
            "volume_filter_mode": self.volume_filter_mode.value,
            "volume_lookback": self.volume_lookback,
            "volume_min_ratio": canonical_decimal_text(self.volume_min_ratio),
            "volume_max_ratio": _optional_decimal_text(self.volume_max_ratio),
            "volume_missing_policy": self.volume_missing_policy.value,
            "volume_filter_target": self.volume_filter_target.value,
            "pre_fvg_enabled": self.pre_fvg_enabled,
            "pre_fvg_evaluation_offset": self.pre_fvg_evaluation_offset,
            "publish_evaluation_events": self.publish_evaluation_events,
            "max_zone_age_bars": self.max_zone_age_bars,
            "source_name": self.source_name,
            "timeframe_overrides": MappingProxyType(
                {timeframe: MappingProxyType(dict(values)) for timeframe, values in self.timeframe_overrides.items()}
            ),
        }
        return MappingProxyType(values)

    def v3_parameters(self) -> Mapping[str, object]:
        """Return the exact v0.2.1 parameter shape for schema-3 migration."""
        return MappingProxyType(
            {
                "timeframes": self.timeframes,
                "direction_filter": self.direction_filter.value,
                "min_gap_absolute": canonical_decimal_text(self.min_gap_absolute),
                "min_gap_fraction": canonical_decimal_text(self.min_gap_fraction),
                "invalidation_level": self.invalidation_level.value,
                "invalidation_basis": self.invalidation_basis.value,
                "require_consecutive_bars": self.require_consecutive_bars,
                "order_violation_policy": self.order_violation_policy.value,
                "publish_lifecycle_events": self.publish_lifecycle_events,
                "max_active_zones_per_timeframe": self.max_active_zones_per_timeframe,
                "max_state_bytes": self.max_state_bytes,
            }
        )

    def v2_parameters(self) -> Mapping[str, object]:
        """Return the exact local v0.2.0 parameter shape for checkpoint migration."""
        return MappingProxyType(
            {
                "timeframes": self.timeframes,
                "direction_filter": self.direction_filter.value,
                "min_gap_absolute": str(self.min_gap_absolute),
                "min_gap_fraction": str(self.min_gap_fraction),
                "invalidation_level": self.invalidation_level.value,
                "invalidation_basis": self.invalidation_basis.value,
                "require_consecutive_bars": self.require_consecutive_bars,
                "order_violation_policy": self.order_violation_policy.value,
                "publish_lifecycle_events": self.publish_lifecycle_events,
                "max_active_zones_per_timeframe": self.max_active_zones_per_timeframe,
                "max_state_bytes": self.max_state_bytes,
            }
        )

    def legacy_v1_parameters(self) -> Mapping[str, object]:
        """Return the exact v0.1.0 parameter shape for checkpoint migration."""
        return MappingProxyType(
            {
                "timeframes": list(self.timeframes),
                "direction_filter": self.direction_filter.value,
                "min_gap_absolute": str(self.min_gap_absolute),
                "min_gap_fraction": str(self.min_gap_fraction),
                "invalidation_level": self.invalidation_level.value,
                "invalidation_basis": self.invalidation_basis.value,
                "require_consecutive_bars": self.require_consecutive_bars,
                "order_violation_policy": self.order_violation_policy.value,
                "publish_lifecycle_events": self.publish_lifecycle_events,
            }
        )
