from decimal import Decimal

import pytest

from modular_strategy_fibonacci.levels.calculator import DEFAULT_LEVELS, calculate_levels
from modular_strategy_fibonacci.levels.validation import validate_orientation


def test_bullish_level_formula_and_negative_extensions() -> None:
    levels = calculate_levels(Decimal("100"), Decimal("200"), "bullish")
    by_ratio = {level.ratio: level.price for level in levels}
    assert tuple(level.ratio for level in levels) == DEFAULT_LEVELS
    assert by_ratio[Decimal("0")] == Decimal("200")
    assert by_ratio[Decimal("1")] == Decimal("100")
    assert by_ratio[Decimal("0.5")] == Decimal("150.0")
    assert by_ratio[Decimal("-0.18")] == Decimal("218.00")
    assert by_ratio[Decimal("-0.618")] == Decimal("261.800")
    validate_orientation(levels, "bullish")


def test_bearish_level_formula_and_negative_extensions() -> None:
    levels = calculate_levels(Decimal("200"), Decimal("100"), "bearish")
    by_ratio = {level.ratio: level.price for level in levels}
    assert by_ratio[Decimal("0")] == Decimal("100")
    assert by_ratio[Decimal("1")] == Decimal("200")
    assert by_ratio[Decimal("0.618")] == Decimal("161.800")
    assert by_ratio[Decimal("-0.18")] == Decimal("82.00")
    assert by_ratio[Decimal("-0.618")] == Decimal("38.200")
    validate_orientation(levels, "bearish")


def test_decimal_precision_is_preserved() -> None:
    levels = calculate_levels(
        Decimal("0.00000123"),
        Decimal("0.00000987"),
        "bullish",
        (Decimal("0.618"),),
    )
    assert levels[0].price == Decimal("0.00000453048")


@pytest.mark.parametrize(
    "start,end,direction",
    [
        (Decimal("1"), Decimal("1"), "bullish"),
        (Decimal("2"), Decimal("1"), "bullish"),
        (Decimal("1"), Decimal("2"), "bearish"),
    ],
)
def test_invalid_anchor_geometry_rejected(start, end, direction) -> None:
    with pytest.raises(ValueError):
        calculate_levels(start, end, direction)
