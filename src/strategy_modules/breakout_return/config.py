from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Mapping

MAX_LOOKBACK_BARS = 100_000
MAX_RETURN_BARS = 100_000
MAX_REFERENCE_AGE_SECONDS = 10_000_000_000


class ReferenceMode(str, Enum):
    ROLLING = "rolling"
    FEATURE = "feature"


class DirectionMode(str, Enum):
    BOTH = "both"
    ABOVE = "above"
    BELOW = "below"


class BreakoutConfirmation(str, Enum):
    CLOSE = "close"
    BODY = "body"


class ReturnConfirmation(str, Enum):
    CLOSE = "close"
    BODY = "body"


class RangeChangePolicy(str, Enum):
    INVALIDATE = "invalidate"
    KEEP_SNAPSHOT = "keep_snapshot"


Numeric = int | float | Decimal


@dataclass(frozen=True, slots=True)
class BreakoutReturnConfig:
    """Validated configuration for :class:`BreakoutReturnModule`.

    ``rolling`` mode derives a range from the previous ``lookback_bars``.
    ``feature`` mode consumes a namespaced feature whose value is a mapping with
    positive ``high`` and ``low`` fields and, optionally, ``range_id``.

    Optional feature filters are deliberately disabled by default to preserve
    the v1.0 public behaviour. When configured, they make the upstream contract
    explicit without introducing a dependency on a concrete producer module.
    """

    reference_mode: ReferenceMode = ReferenceMode.ROLLING
    reference_feature_key: str = "range.current"
    reference_feature_timeframe: str | None = None
    reference_feature_schema_version: str | None = None
    max_reference_age_seconds: int | None = None
    lookback_bars: int = 20
    direction: DirectionMode = DirectionMode.BOTH
    breakout_confirmation: BreakoutConfirmation = BreakoutConfirmation.BODY
    return_confirmation: ReturnConfirmation = ReturnConfirmation.BODY
    breakout_buffer_bps: Numeric = 0.0
    min_return_depth: Numeric = 0.02
    max_return_depth: Numeric = 0.20
    max_return_bars: int = 36
    range_change_policy: RangeChangePolicy = RangeChangePolicy.INVALIDATE

    def __post_init__(self) -> None:
        self._require_enum(self.reference_mode, ReferenceMode, "reference_mode")
        self._require_enum(self.direction, DirectionMode, "direction")
        self._require_enum(
            self.breakout_confirmation,
            BreakoutConfirmation,
            "breakout_confirmation",
        )
        self._require_enum(
            self.return_confirmation,
            ReturnConfirmation,
            "return_confirmation",
        )
        self._require_enum(
            self.range_change_policy,
            RangeChangePolicy,
            "range_change_policy",
        )
        self._require_namespaced_key(self.reference_feature_key)
        self._require_optional_nonblank(
            self.reference_feature_timeframe,
            "reference_feature_timeframe",
        )
        self._require_optional_nonblank(
            self.reference_feature_schema_version,
            "reference_feature_schema_version",
        )
        self._require_int_range(
            self.lookback_bars,
            "lookback_bars",
            minimum=2,
            maximum=MAX_LOOKBACK_BARS,
        )
        self._require_int_range(
            self.max_return_bars,
            "max_return_bars",
            minimum=1,
            maximum=MAX_RETURN_BARS,
        )
        if self.max_reference_age_seconds is not None:
            self._require_int_range(
                self.max_reference_age_seconds,
                "max_reference_age_seconds",
                minimum=1,
                maximum=MAX_REFERENCE_AGE_SECONDS,
            )
        feature_only_values = {
            "reference_feature_timeframe": self.reference_feature_timeframe,
            "reference_feature_schema_version": self.reference_feature_schema_version,
            "max_reference_age_seconds": self.max_reference_age_seconds,
        }
        if self.reference_mode is not ReferenceMode.FEATURE:
            enabled = sorted(
                name for name, value in feature_only_values.items() if value is not None
            )
            if enabled:
                raise ValueError(
                    "feature-only parameters require reference_mode='feature': "
                    + ", ".join(enabled)
                )

        breakout_buffer = self._decimal(
            self.breakout_buffer_bps,
            "breakout_buffer_bps",
        )
        min_depth = self._decimal(self.min_return_depth, "min_return_depth")
        max_depth = self._decimal(self.max_return_depth, "max_return_depth")
        if not Decimal("0") <= breakout_buffer <= Decimal("5000"):
            raise ValueError("breakout_buffer_bps must be in [0, 5000]")
        if not Decimal("0") <= min_depth <= max_depth <= Decimal("1"):
            raise ValueError("return depth must satisfy 0 <= min <= max <= 1")
        self._require_parameter_roundtrip(
            breakout_buffer,
            "breakout_buffer_bps",
        )
        self._require_parameter_roundtrip(min_depth, "min_return_depth")
        self._require_parameter_roundtrip(max_depth, "max_return_depth")

    @staticmethod
    def _require_enum(value: object, expected: type[Enum], name: str) -> None:
        if not isinstance(value, expected):
            raise ValueError(f"{name} must be {expected.__name__}")

    @staticmethod
    def _require_parameter_roundtrip(value: Decimal, name: str) -> None:
        if value == value.to_integral_value():
            return
        float_value = float(value)
        if Decimal(str(float_value)) != value:
            raise ValueError(f"{name} cannot be represented exactly in public module parameters")

    @staticmethod
    def _require_namespaced_key(value: object) -> None:
        if not isinstance(value, str) or value != value.strip():
            raise ValueError("reference_feature_key must be a nonblank namespaced string")
        parts = value.split(".")
        if len(parts) < 2 or any(
            not part or any(character.isspace() for character in part) for part in parts
        ):
            raise ValueError("reference_feature_key must be namespaced, e.g. 'range.current'")

    @staticmethod
    def _require_optional_nonblank(value: object, name: str) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be a nonblank trimmed string or None")

    @staticmethod
    def _require_int_range(
        value: object,
        name: str,
        *,
        minimum: int,
        maximum: int,
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be in [{minimum}, {maximum}]")

    @staticmethod
    def _decimal(value: object, name: str) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError(f"{name} must be a finite number")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not result.is_finite():
            raise ValueError(f"{name} must be a finite number")
        return result

    def as_parameters(self) -> Mapping[str, object]:
        parameters: dict[str, object] = {
            "reference_mode": self.reference_mode.value,
            "reference_feature_key": self.reference_feature_key,
            "lookback_bars": self.lookback_bars,
            "direction": self.direction.value,
            "breakout_confirmation": self.breakout_confirmation.value,
            "return_confirmation": self.return_confirmation.value,
            "breakout_buffer_bps": self._parameter_number(self.breakout_buffer_bps),
            "min_return_depth": self._parameter_number(self.min_return_depth),
            "max_return_depth": self._parameter_number(self.max_return_depth),
            "max_return_bars": self.max_return_bars,
            "range_change_policy": self.range_change_policy.value,
        }
        if self.reference_feature_timeframe is not None:
            parameters["reference_feature_timeframe"] = self.reference_feature_timeframe
        if self.reference_feature_schema_version is not None:
            parameters["reference_feature_schema_version"] = self.reference_feature_schema_version
        if self.max_reference_age_seconds is not None:
            parameters["max_reference_age_seconds"] = self.max_reference_age_seconds
        return MappingProxyType(parameters)

    def fingerprint_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "reference_mode": self.reference_mode.value,
                "reference_feature_key": self.reference_feature_key,
                "reference_feature_timeframe": self.reference_feature_timeframe,
                "reference_feature_schema_version": (self.reference_feature_schema_version),
                "max_reference_age_seconds": self.max_reference_age_seconds,
                "lookback_bars": self.lookback_bars,
                "direction": self.direction.value,
                "breakout_confirmation": self.breakout_confirmation.value,
                "return_confirmation": self.return_confirmation.value,
                "breakout_buffer_bps": self._canonical_decimal(self.breakout_buffer_bps),
                "min_return_depth": self._canonical_decimal(self.min_return_depth),
                "max_return_depth": self._canonical_decimal(self.max_return_depth),
                "max_return_bars": self.max_return_bars,
                "range_change_policy": self.range_change_policy.value,
            }
        )

    @classmethod
    def _parameter_number(cls, value: Numeric) -> int | float:
        decimal_value = cls._decimal(value, "parameter")
        if decimal_value == decimal_value.to_integral_value():
            return int(decimal_value)
        return float(decimal_value)

    @classmethod
    def _canonical_decimal(cls, value: Numeric) -> str:
        decimal_value = cls._decimal(value, "parameter")
        if decimal_value == 0:
            return "0"
        return format(decimal_value.normalize(), "f")
