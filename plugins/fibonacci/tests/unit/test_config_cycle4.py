from __future__ import annotations

from decimal import Decimal

import pytest

from modular_strategy_fibonacci.config import FibonacciConfig
from modular_strategy_fibonacci.exceptions import ConfigurationError


@pytest.mark.parametrize(
    "kwargs",
    [
        {"short_reaction_lookback": 0},
        {"short_reaction_minimum_samples": 0},
        {"short_reaction_minimum_streak": 0},
        {"short_regime_window_size": 0},
        {"short_regime_minimum_window_samples": 0},
        {"short_regime_minimum_level_samples": 0},
        {"short_reaction_lookback": 4, "short_reaction_minimum_samples": 5},
        {"short_reaction_lookback": 4, "short_reaction_minimum_streak": 5},
        {"short_regime_window_size": 4, "short_regime_minimum_window_samples": 5},
        {"short_regime_window_size": 4, "short_regime_minimum_level_samples": 5},
        {"short_reaction_minimum_success_rate": Decimal("-0.01")},
        {"short_reaction_minimum_success_rate": Decimal("1.01")},
        {"short_regime_minimum_success_rate": Decimal("-0.01")},
        {"short_regime_minimum_success_rate": Decimal("1.01")},
        {"short_regime_minimum_level_success_rate": Decimal("-0.01")},
        {"short_regime_minimum_level_success_rate": Decimal("1.01")},
        {"short_regime_change_threshold": Decimal("-0.01")},
        {"short_regime_change_threshold": Decimal("1.01")},
    ],
)
def test_cycle4_config_rejects_invalid_thresholds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        FibonacciConfig(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("short_reaction_minimum_success_rate", Decimal("0")),
        ("short_reaction_minimum_success_rate", Decimal("1")),
        ("short_regime_minimum_success_rate", Decimal("0")),
        ("short_regime_minimum_success_rate", Decimal("1")),
        ("short_regime_minimum_level_success_rate", Decimal("0")),
        ("short_regime_minimum_level_success_rate", Decimal("1")),
        ("short_regime_change_threshold", Decimal("0")),
        ("short_regime_change_threshold", Decimal("1")),
    ],
)
def test_cycle4_config_accepts_closed_probability_bounds(field: str, value: Decimal) -> None:
    config = FibonacciConfig(**{field: value})
    assert getattr(config, field) == value


def test_cycle4_config_hash_changes_for_each_versioned_threshold() -> None:
    baseline = FibonacciConfig().stable_hash()
    variants = (
        FibonacciConfig(short_reaction_lookback=41),
        FibonacciConfig(short_reaction_minimum_samples=9),
        FibonacciConfig(short_reaction_minimum_success_rate=Decimal("0.66")),
        FibonacciConfig(short_reaction_minimum_streak=4),
        FibonacciConfig(short_regime_window_size=13),
        FibonacciConfig(short_regime_minimum_window_samples=9),
        FibonacciConfig(short_regime_minimum_success_rate=Decimal("0.66")),
        FibonacciConfig(short_regime_minimum_level_samples=5),
        FibonacciConfig(short_regime_minimum_level_success_rate=Decimal("0.66")),
        FibonacciConfig(short_regime_change_threshold=Decimal("0.16")),
    )
    assert all(item.stable_hash() != baseline for item in variants)
    assert len({item.stable_hash() for item in variants}) == len(variants)


def test_cycle4_yaml_parses_decimal_thresholds(tmp_path) -> None:
    path = tmp_path / "fibonacci.yaml"
    path.write_text(
        "\n".join(
            [
                "short_reaction_minimum_success_rate: 0.70",
                "short_regime_minimum_success_rate: 0.71",
                "short_regime_minimum_level_success_rate: 0.72",
                "short_regime_change_threshold: 0.12",
            ]
        ),
        encoding="utf-8",
    )
    config = FibonacciConfig.from_yaml(path)
    assert config.short_reaction_minimum_success_rate == Decimal("0.7")
    assert config.short_regime_minimum_success_rate == Decimal("0.71")
    assert config.short_regime_minimum_level_success_rate == Decimal("0.72")
    assert config.short_regime_change_threshold == Decimal("0.12")
