"""Versioned configuration for deterministic Fibonacci generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class FibonacciConfig:
    levels: tuple[Decimal, ...] = (
        Decimal("0"),
        Decimal("0.5"),
        Decimal("0.618"),
        Decimal("0.786"),
        Decimal("0.886"),
        Decimal("1"),
        Decimal("-0.18"),
        Decimal("-0.27"),
        Decimal("-0.5"),
        Decimal("-0.618"),
    )
    pivot_left: int = 3
    pivot_right: int = 3
    min_bars: int = 4
    max_bars: int = 160
    min_atr_multiple: Decimal = Decimal("1.5")
    max_candidates: int = 5
    correction_levels: tuple[Decimal, ...] = (
        Decimal("0.5"),
        Decimal("0.618"),
        Decimal("0.786"),
        Decimal("0.886"),
    )
    shallow_correction_tolerance: Decimal = Decimal("0.03")
    completion_targets: tuple[Decimal, ...] = (
        Decimal("-0.18"),
        Decimal("-0.27"),
    )
    extension_reanchor_levels: tuple[Decimal, ...] = (
        Decimal("-0.5"),
        Decimal("-0.618"),
    )
    correction_profile_lookback: int = 200
    short_reaction_lookback: int = 40
    short_reaction_minimum_samples: int = 8
    short_reaction_minimum_success_rate: Decimal = Decimal("0.65")
    short_reaction_minimum_streak: int = 3
    short_regime_window_size: int = 12
    short_regime_minimum_window_samples: int = 8
    short_regime_minimum_success_rate: Decimal = Decimal("0.65")
    short_regime_minimum_level_samples: int = 4
    short_regime_minimum_level_success_rate: Decimal = Decimal("0.65")
    short_regime_change_threshold: Decimal = Decimal("0.15")
    max_building_bars: int = 160
    max_wait_correction_bars: int = 160
    max_wait_extension_bars: int = 320
    orientation_version: str = "end-plus-ratio-start-minus-end/v1"
    workflow_version: str = "sequential-real-time/v2-full-endpoint"
    renderer_version: str = "matplotlib/v1"

    def __post_init__(self) -> None:
        if not self.levels or len(set(self.levels)) != len(self.levels):
            raise ConfigurationError("levels must be unique and non-empty")
        if self.pivot_left < 1 or self.pivot_right < 1:
            raise ConfigurationError("pivot widths must be positive")
        if self.min_bars < 1 or self.max_bars < self.min_bars:
            raise ConfigurationError("invalid candidate bar limits")
        if self.min_atr_multiple <= 0:
            raise ConfigurationError("min_atr_multiple must be positive")
        if not 1 <= self.max_candidates <= 5:
            raise ConfigurationError("max_candidates must be in [1, 5]")
        if tuple(sorted(self.correction_levels)) != self.correction_levels:
            raise ConfigurationError("correction_levels must be strictly ascending")
        if len(set(self.correction_levels)) != len(self.correction_levels):
            raise ConfigurationError("correction_levels must be unique")
        if any(level <= 0 or level >= 1 for level in self.correction_levels):
            raise ConfigurationError("correction levels must be between 0 and 1")
        if not Decimal("0") <= self.shallow_correction_tolerance < Decimal("0.5"):
            raise ConfigurationError("invalid shallow_correction_tolerance")
        if not self.completion_targets or any(level >= 0 for level in self.completion_targets):
            raise ConfigurationError("completion_targets must be negative")
        if not self.extension_reanchor_levels or any(level >= 0 for level in self.extension_reanchor_levels):
            raise ConfigurationError("extension_reanchor_levels must be negative")
        if self.correction_profile_lookback < 1:
            raise ConfigurationError("correction_profile_lookback must be positive")
        if (
            min(
                self.short_reaction_lookback,
                self.short_reaction_minimum_samples,
                self.short_reaction_minimum_streak,
                self.short_regime_window_size,
                self.short_regime_minimum_window_samples,
                self.short_regime_minimum_level_samples,
            )
            < 1
        ):
            raise ConfigurationError("short reaction limits must be positive")
        if self.short_reaction_minimum_samples > self.short_reaction_lookback:
            raise ConfigurationError("short reaction minimum samples exceed lookback")
        if self.short_reaction_minimum_streak > self.short_reaction_lookback:
            raise ConfigurationError("short reaction minimum streak exceeds lookback")
        if self.short_regime_minimum_window_samples > self.short_regime_window_size:
            raise ConfigurationError("short regime minimum samples exceed window")
        if self.short_regime_minimum_level_samples > self.short_regime_window_size:
            raise ConfigurationError("short regime minimum level samples exceed window")
        for name, value in (
            ("short_reaction_minimum_success_rate", self.short_reaction_minimum_success_rate),
            ("short_regime_minimum_success_rate", self.short_regime_minimum_success_rate),
            (
                "short_regime_minimum_level_success_rate",
                self.short_regime_minimum_level_success_rate,
            ),
            ("short_regime_change_threshold", self.short_regime_change_threshold),
        ):
            if not Decimal("0") <= value <= Decimal("1"):
                raise ConfigurationError(f"{name} must be in [0, 1]")
        if (
            min(
                self.max_building_bars,
                self.max_wait_correction_bars,
                self.max_wait_extension_bars,
            )
            < 1
        ):
            raise ConfigurationError("workflow bar limits must be positive")

    def stable_hash(self) -> str:
        payload = {
            key: [str(item) for item in value]
            if isinstance(value, tuple)
            else str(value)
            if isinstance(value, Decimal)
            else value
            for key, value in asdict(self).items()
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_yaml(cls, path: Path) -> "FibonacciConfig":
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if "levels" in raw:
            raw["levels"] = tuple(Decimal(str(value)) for value in raw["levels"])
        if "min_atr_multiple" in raw:
            raw["min_atr_multiple"] = Decimal(str(raw["min_atr_multiple"]))
        for field_name in (
            "correction_levels",
            "completion_targets",
            "extension_reanchor_levels",
        ):
            if field_name in raw:
                raw[field_name] = tuple(Decimal(str(value)) for value in raw[field_name])
        for field_name in (
            "shallow_correction_tolerance",
            "short_reaction_minimum_success_rate",
            "short_regime_minimum_success_rate",
            "short_regime_minimum_level_success_rate",
            "short_regime_change_threshold",
        ):
            if field_name in raw:
                raw[field_name] = Decimal(str(raw[field_name]))
        return cls(**raw)
