from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from modular_strategy_fibonacci.config import FibonacciConfig
from modular_strategy_fibonacci.exceptions import ConfigurationError


def test_config_hash_is_stable() -> None:
    assert FibonacciConfig().stable_hash() == FibonacciConfig().stable_hash()


def test_yaml_load_preserves_level_order(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("levels: [1, 0, -0.18]\nmin_atr_multiple: 2.0\n", encoding="utf-8")
    config = FibonacciConfig.from_yaml(path)
    assert config.levels == (Decimal("1"), Decimal("0"), Decimal("-0.18"))
    assert config.min_atr_multiple == Decimal("2.0")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"levels": ()}, "levels"),
        ({"levels": (Decimal("0"), Decimal("0"))}, "levels"),
        ({"pivot_left": 0}, "pivot"),
        ({"pivot_right": 0}, "pivot"),
        ({"min_bars": 0}, "bar limits"),
        ({"max_bars": 3, "min_bars": 4}, "bar limits"),
        ({"min_atr_multiple": Decimal("0")}, "min_atr"),
        ({"max_candidates": 0}, "max_candidates"),
        ({"max_candidates": 6}, "max_candidates"),
        ({"correction_levels": (Decimal("0.618"), Decimal("0.5"))}, "ascending"),
        ({"correction_levels": (Decimal("0.5"), Decimal("0.5"))}, "unique"),
        ({"correction_levels": (Decimal("0"), Decimal("0.5"))}, "between"),
        ({"shallow_correction_tolerance": Decimal("0.5")}, "tolerance"),
        ({"completion_targets": ()}, "completion_targets"),
        ({"completion_targets": (Decimal("0.18"),)}, "completion_targets"),
        ({"extension_reanchor_levels": (Decimal("0"),)}, "extension"),
        ({"correction_profile_lookback": 0}, "lookback"),
        ({"max_building_bars": 0}, "bar limits"),
    ],
)
def test_invalid_configurations_are_rejected(changes, message) -> None:
    with pytest.raises(ConfigurationError, match=message):
        replace(FibonacciConfig(), **changes)


def test_yaml_loads_all_decimal_workflow_fields(tmp_path: Path) -> None:
    path = tmp_path / "full.yaml"
    path.write_text(
        """
correction_levels: [0.5, 0.618, 0.786, 0.886]
completion_targets: [-0.18, -0.27]
extension_reanchor_levels: [-0.5, -0.618]
shallow_correction_tolerance: 0.02
""".strip(),
        encoding="utf-8",
    )
    config = FibonacciConfig.from_yaml(path)
    assert config.shallow_correction_tolerance == Decimal("0.02")
    assert config.completion_targets == (Decimal("-0.18"), Decimal("-0.27"))
