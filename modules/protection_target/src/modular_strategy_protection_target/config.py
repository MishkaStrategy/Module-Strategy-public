from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

from .constants import ENTRY_PLAN_FEATURE_KEY
from .decimal_utils import decimal_text
from .exceptions import ProtectionTargetConfigurationError

_MAX_DECIMAL_DIGITS = 256
_MAX_ABS_EXPONENT = 4_096
_MAX_CONFIG_STRING = 512


class InvalidationMode(StrEnum):
    ENTRY_PLAN = "entry_plan"
    FIXED_PERCENT = "fixed_percent"
    FEATURE = "feature"


class TargetMode(StrEnum):
    FIXED_R = "fixed_r"
    STRUCTURAL = "structural"


class StructuralTargetSource(StrEnum):
    ENTRY_PLAN = "entry_plan"
    FEATURE = "feature"


def to_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ProtectionTargetConfigurationError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProtectionTargetConfigurationError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ProtectionTargetConfigurationError(f"{name} must be finite")
    decimal_tuple = result.as_tuple()
    if len(decimal_tuple.digits) > _MAX_DECIMAL_DIGITS:
        raise ProtectionTargetConfigurationError(
            f"{name} exceeds {_MAX_DECIMAL_DIGITS} significant digits"
        )
    exponent = cast(int, decimal_tuple.exponent)
    if abs(exponent) > _MAX_ABS_EXPONENT:
        raise ProtectionTargetConfigurationError(f"{name} exponent exceeds {_MAX_ABS_EXPONENT}")
    return result


def _require_namespaced(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ProtectionTargetConfigurationError(f"{name} must be a namespaced feature key")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_CONFIG_STRING
        or "." not in normalized
        or any(ch.isspace() for ch in normalized)
    ):
        raise ProtectionTargetConfigurationError(
            f"{name} must be a namespaced feature key no longer than {_MAX_CONFIG_STRING} characters"
        )
    return normalized


def _require_field_path(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtectionTargetConfigurationError(f"{name} must be non-empty")
    normalized = value.strip()
    if len(normalized) > _MAX_CONFIG_STRING:
        raise ProtectionTargetConfigurationError(f"{name} exceeds {_MAX_CONFIG_STRING} characters")
    parts = normalized.split(".")
    if any(not part or any(ch.isspace() for ch in part) for part in parts):
        raise ProtectionTargetConfigurationError(f"{name} must be a dotted mapping path")
    return normalized


def _manifest_number(value: Decimal, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or (value != 0 and result == 0.0):
        raise ProtectionTargetConfigurationError(
            f"{name} cannot be represented as a finite Module API number"
        )
    return result


@dataclass(frozen=True, slots=True)
class ProtectionTargetConfig:
    entry_feature_key: str = ENTRY_PLAN_FEATURE_KEY
    invalidation_mode: InvalidationMode = InvalidationMode.ENTRY_PLAN
    invalidation_field: str = "invalidation_price"
    invalidation_feature_key: str | None = None
    invalidation_feature_field: str = "price"
    fixed_stop_percent: Decimal = Decimal("0.005")
    stop_buffer_bps: Decimal = Decimal("0")
    target_mode: TargetMode = TargetMode.FIXED_R
    structural_target_source: StructuralTargetSource = StructuralTargetSource.ENTRY_PLAN
    target_field: str = "target_price"
    target_feature_key: str | None = None
    target_feature_field: str = "price"
    max_holding_seconds_field: str | None = None
    no_progress_rule_field: str | None = None
    reward_r: Decimal = Decimal("3")
    min_reward_r: Decimal = Decimal("0.5")
    max_reward_r: Decimal = Decimal("20")
    min_stop_distance_bps: Decimal = Decimal("1")
    max_stop_distance_bps: Decimal = Decimal("5000")
    require_same_bar_entry: bool = True
    require_structural_setup_match: bool = False
    max_processed_plans: int = 4096
    max_diagnostic_results: int = 100
    emit_rejection_feature: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.invalidation_mode, InvalidationMode):
            raise ProtectionTargetConfigurationError("invalidation_mode must be InvalidationMode")
        if not isinstance(self.target_mode, TargetMode):
            raise ProtectionTargetConfigurationError("target_mode must be TargetMode")
        if not isinstance(self.structural_target_source, StructuralTargetSource):
            raise ProtectionTargetConfigurationError(
                "structural_target_source must be StructuralTargetSource"
            )
        object.__setattr__(
            self,
            "entry_feature_key",
            _require_namespaced(self.entry_feature_key, "entry_feature_key"),
        )
        object.__setattr__(
            self,
            "invalidation_field",
            _require_field_path(self.invalidation_field, "invalidation_field"),
        )
        object.__setattr__(
            self,
            "invalidation_feature_field",
            _require_field_path(self.invalidation_feature_field, "invalidation_feature_field"),
        )
        object.__setattr__(
            self, "target_field", _require_field_path(self.target_field, "target_field")
        )
        object.__setattr__(
            self,
            "target_feature_field",
            _require_field_path(self.target_feature_field, "target_feature_field"),
        )

        if self.max_holding_seconds_field is not None:
            object.__setattr__(
                self,
                "max_holding_seconds_field",
                _require_field_path(
                    self.max_holding_seconds_field,
                    "max_holding_seconds_field",
                ),
            )
        if self.no_progress_rule_field is not None:
            object.__setattr__(
                self,
                "no_progress_rule_field",
                _require_field_path(
                    self.no_progress_rule_field,
                    "no_progress_rule_field",
                ),
            )

        for field_name in (
            "fixed_stop_percent",
            "stop_buffer_bps",
            "reward_r",
            "min_reward_r",
            "max_reward_r",
            "min_stop_distance_bps",
            "max_stop_distance_bps",
        ):
            object.__setattr__(self, field_name, to_decimal(getattr(self, field_name), field_name))

        if self.invalidation_feature_key is not None:
            object.__setattr__(
                self,
                "invalidation_feature_key",
                _require_namespaced(self.invalidation_feature_key, "invalidation_feature_key"),
            )
        if self.target_feature_key is not None:
            object.__setattr__(
                self,
                "target_feature_key",
                _require_namespaced(self.target_feature_key, "target_feature_key"),
            )

        if (
            self.invalidation_mode is InvalidationMode.FEATURE
            and self.invalidation_feature_key is None
        ):
            raise ProtectionTargetConfigurationError(
                "invalidation_feature_key is required when invalidation_mode='feature'"
            )
        if (
            self.target_mode is TargetMode.STRUCTURAL
            and self.structural_target_source is StructuralTargetSource.FEATURE
            and self.target_feature_key is None
        ):
            raise ProtectionTargetConfigurationError(
                "target_feature_key is required for structural targets sourced from a feature"
            )
        if not Decimal("0") < self.fixed_stop_percent < Decimal("1"):
            raise ProtectionTargetConfigurationError("fixed_stop_percent must be in (0, 1)")
        if not Decimal("0") <= self.stop_buffer_bps < Decimal("10000"):
            raise ProtectionTargetConfigurationError("stop_buffer_bps must be in [0, 10000)")
        if self.reward_r <= 0:
            raise ProtectionTargetConfigurationError("reward_r must be positive")
        if self.min_reward_r <= 0 or self.max_reward_r < self.min_reward_r:
            raise ProtectionTargetConfigurationError(
                "reward-R bounds must satisfy 0 < min_reward_r <= max_reward_r"
            )
        if self.target_mode is TargetMode.FIXED_R and not (
            self.min_reward_r <= self.reward_r <= self.max_reward_r
        ):
            raise ProtectionTargetConfigurationError(
                "fixed reward_r must be within min_reward_r and max_reward_r"
            )
        if self.min_stop_distance_bps < 0:
            raise ProtectionTargetConfigurationError("min_stop_distance_bps must be non-negative")
        if (
            self.max_stop_distance_bps <= 0
            or self.max_stop_distance_bps < self.min_stop_distance_bps
        ):
            raise ProtectionTargetConfigurationError(
                "stop-distance bounds must satisfy 0 <= minimum <= maximum"
            )
        if isinstance(self.max_processed_plans, bool) or not isinstance(
            self.max_processed_plans, int
        ):
            raise ProtectionTargetConfigurationError("max_processed_plans must be an integer")
        if not 1 <= self.max_processed_plans <= 1_000_000:
            raise ProtectionTargetConfigurationError("max_processed_plans must be in [1, 1000000]")
        if not isinstance(self.require_same_bar_entry, bool):
            raise ProtectionTargetConfigurationError("require_same_bar_entry must be boolean")
        if not isinstance(self.require_structural_setup_match, bool):
            raise ProtectionTargetConfigurationError(
                "require_structural_setup_match must be boolean"
            )
        if isinstance(self.max_diagnostic_results, bool) or not isinstance(
            self.max_diagnostic_results, int
        ):
            raise ProtectionTargetConfigurationError("max_diagnostic_results must be an integer")
        if not 0 <= self.max_diagnostic_results <= 1000:
            raise ProtectionTargetConfigurationError("max_diagnostic_results must be in [0, 1000]")
        if not isinstance(self.emit_rejection_feature, bool):
            raise ProtectionTargetConfigurationError("emit_rejection_feature must be boolean")

    @property
    def required_feature_keys(self) -> tuple[str, ...]:
        keys: list[str] = [self.entry_feature_key]
        if self.invalidation_mode is InvalidationMode.FEATURE:
            if self.invalidation_feature_key is None:
                raise ProtectionTargetConfigurationError(
                    "invalidation_feature_key is required for feature invalidation"
                )
            keys.append(self.invalidation_feature_key)
        if (
            self.target_mode is TargetMode.STRUCTURAL
            and self.structural_target_source is StructuralTargetSource.FEATURE
        ):
            if self.target_feature_key is None:
                raise ProtectionTargetConfigurationError(
                    "target_feature_key is required for structural feature targets"
                )
            keys.append(self.target_feature_key)
        return tuple(dict.fromkeys(keys))

    def as_parameters(self) -> Mapping[str, object]:
        result: dict[str, object] = {
            "entry_feature_key": self.entry_feature_key,
            "invalidation_mode": self.invalidation_mode.value,
            "invalidation_field": self.invalidation_field,
            "invalidation_feature_field": self.invalidation_feature_field,
            "fixed_stop_percent": _manifest_number(self.fixed_stop_percent, "fixed_stop_percent"),
            "stop_buffer_bps": _manifest_number(self.stop_buffer_bps, "stop_buffer_bps"),
            "target_mode": self.target_mode.value,
            "structural_target_source": self.structural_target_source.value,
            "target_field": self.target_field,
            "target_feature_field": self.target_feature_field,
            "reward_r": _manifest_number(self.reward_r, "reward_r"),
            "min_reward_r": _manifest_number(self.min_reward_r, "min_reward_r"),
            "max_reward_r": _manifest_number(self.max_reward_r, "max_reward_r"),
            "min_stop_distance_bps": _manifest_number(
                self.min_stop_distance_bps, "min_stop_distance_bps"
            ),
            "max_stop_distance_bps": _manifest_number(
                self.max_stop_distance_bps, "max_stop_distance_bps"
            ),
            "require_same_bar_entry": self.require_same_bar_entry,
            "require_structural_setup_match": self.require_structural_setup_match,
            "max_processed_plans": self.max_processed_plans,
            "max_diagnostic_results": self.max_diagnostic_results,
            "emit_rejection_feature": self.emit_rejection_feature,
        }
        if self.invalidation_feature_key is not None:
            result["invalidation_feature_key"] = self.invalidation_feature_key
        if self.target_feature_key is not None:
            result["target_feature_key"] = self.target_feature_key
        if self.max_holding_seconds_field is not None:
            result["max_holding_seconds_field"] = self.max_holding_seconds_field
        if self.no_progress_rule_field is not None:
            result["no_progress_rule_field"] = self.no_progress_rule_field
        return result

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            **dict(self.as_parameters()),
            "fixed_stop_percent": decimal_text(self.fixed_stop_percent),
            "stop_buffer_bps": decimal_text(self.stop_buffer_bps),
            "reward_r": decimal_text(self.reward_r),
            "min_reward_r": decimal_text(self.min_reward_r),
            "max_reward_r": decimal_text(self.max_reward_r),
            "min_stop_distance_bps": decimal_text(self.min_stop_distance_bps),
            "max_stop_distance_bps": decimal_text(self.max_stop_distance_bps),
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self._fingerprint_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def legacy_fingerprint_v1(self) -> str:
        payload = self._fingerprint_payload()
        payload.pop("max_diagnostic_results", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
