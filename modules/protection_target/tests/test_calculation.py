from __future__ import annotations

from decimal import Decimal, getcontext

import pytest
from conftest import make_entry_feature, make_structural_feature, make_view
from modular_core import Candle

from modular_strategy_protection_target import ProtectionTargetModule
from modular_strategy_protection_target.decimal_utils import decimal_text


def plan(decision: object) -> dict[str, object]:
    values = [f for f in decision.features if f.key == "protection_target.plan"]
    assert len(values) == 1
    return dict(values[0].value)


def reason(decision: object) -> str:
    values = [f for f in decision.features if f.key == "protection_target.rejected"]
    assert len(values) == 1
    return str(values[0].value["reason"])


@pytest.mark.parametrize(
    ("side", "invalidation", "reward", "stop", "target"),
    [
        ("buy", "99", "1", "99", "101"),
        ("buy", "99", "3", "99", "103"),
        ("buy", "98", "4", "98", "108"),
        ("sell", "101", "1", "101", "99"),
        ("sell", "101", "3", "101", "97"),
        ("sell", "102", "4", "102", "92"),
    ],
)
def test_fixed_r_geometry(
    bar: Candle, side: str, invalidation: str, reward: str, stop: str, target: str
) -> None:
    entry = make_entry_feature(bar, side=side, invalidation_price=invalidation)
    value = plan(ProtectionTargetModule(reward_r=reward).on_bar(make_view(bar, entry)))
    assert value["stop_loss"] == stop
    assert value["take_profit"] == target
    assert value["reward_r"] == reward


@pytest.mark.parametrize(
    ("side", "invalidation", "buffer", "expected"),
    [
        ("buy", "99", "100", "98.01"),
        ("buy", "99", "50", "98.505"),
        ("sell", "101", "100", "102.01"),
        ("sell", "101", "50", "101.505"),
    ],
)
def test_stop_buffer_is_outward(
    bar: Candle, side: str, invalidation: str, buffer: str, expected: str
) -> None:
    entry = make_entry_feature(bar, side=side, invalidation_price=invalidation)
    value = plan(
        ProtectionTargetModule(stop_buffer_bps=buffer, reward_r=1).on_bar(make_view(bar, entry))
    )
    assert value["stop_loss"] == expected


@pytest.mark.parametrize(
    ("side", "percent", "expected_invalid", "expected_target"),
    [
        ("buy", "0.001", "99.9", "100.2"),
        ("buy", "0.02", "98", "104"),
        ("sell", "0.001", "100.1", "99.8"),
        ("sell", "0.02", "102", "96"),
    ],
)
def test_fixed_percent_invalidation(
    bar: Candle, side: str, percent: str, expected_invalid: str, expected_target: str
) -> None:
    entry = make_entry_feature(bar, side=side, invalidation_price=None)
    value = plan(
        ProtectionTargetModule(
            invalidation_mode="fixed_percent", fixed_stop_percent=percent, reward_r=2
        ).on_bar(make_view(bar, entry))
    )
    assert value["invalidation_price"] == expected_invalid
    assert value["take_profit"] == expected_target


def test_nested_entry_invalidation_and_target(bar: Candle) -> None:
    entry = make_entry_feature(
        bar,
        invalidation_price=None,
        target_price=None,
        extra={"levels": {"stop": "98.5", "target": "104.5"}},
    )
    value = plan(
        ProtectionTargetModule(
            invalidation_field="levels.stop",
            target_mode="structural",
            target_field="levels.target",
        ).on_bar(make_view(bar, entry))
    )
    assert value["invalidation_price"] == "98.5"
    assert value["take_profit"] == "104.5"
    assert value["reward_r"] == "3"


def test_structural_feature_invalidation_and_target(bar: Candle) -> None:
    entry = make_entry_feature(bar, invalidation_price=None, target_price=None)
    stop = make_structural_feature(
        bar,
        key="structure.stop",
        price="98.75",
        feature_id="stop-1",
        setup_id="setup-1",
    )
    target = make_structural_feature(
        bar,
        key="structure.target",
        price="105",
        feature_id="target-1",
        setup_id="setup-1",
    )
    value = plan(
        ProtectionTargetModule(
            invalidation_mode="feature",
            invalidation_feature_key="structure.stop",
            target_mode="structural",
            structural_target_source="feature",
            target_feature_key="structure.target",
            require_structural_setup_match=True,
        ).on_bar(make_view(bar, entry, stop, target))
    )
    assert value["source_invalidation_feature_id"] == "stop-1"
    assert value["source_target_feature_id"] == "target-1"
    assert value["take_profit"] == "105"


@pytest.mark.parametrize(
    ("side", "invalidation"),
    [("buy", "100"), ("buy", "101"), ("sell", "100"), ("sell", "99")],
)
def test_wrong_side_invalidation_rejected(bar: Candle, side: str, invalidation: str) -> None:
    entry = make_entry_feature(bar, side=side, invalidation_price=invalidation)
    assert reason(ProtectionTargetModule().on_bar(make_view(bar, entry))) == (
        "protection_target.invalid_invalidation"
    )


@pytest.mark.parametrize(
    ("side", "target"),
    [("buy", "100"), ("buy", "99"), ("sell", "100"), ("sell", "101")],
)
def test_wrong_side_structural_target_rejected(bar: Candle, side: str, target: str) -> None:
    invalidation = "99" if side == "buy" else "101"
    entry = make_entry_feature(bar, side=side, invalidation_price=invalidation, target_price=target)
    assert (
        reason(ProtectionTargetModule(target_mode="structural").on_bar(make_view(bar, entry)))
        == "protection_target.invalid_structural_target"
    )


def test_stop_distance_boundaries(bar: Candle) -> None:
    entry = make_entry_feature(bar, invalidation_price="99")
    assert (
        plan(ProtectionTargetModule(min_stop_distance_bps=100).on_bar(make_view(bar, entry)))[
            "stop_distance_bps"
        ]
        == "100"
    )
    assert (
        reason(ProtectionTargetModule(min_stop_distance_bps=101).on_bar(make_view(bar, entry)))
        == "protection_target.stop_distance_below_minimum"
    )
    assert (
        plan(ProtectionTargetModule(max_stop_distance_bps=100).on_bar(make_view(bar, entry)))[
            "stop_distance_bps"
        ]
        == "100"
    )
    assert (
        reason(ProtectionTargetModule(max_stop_distance_bps=99).on_bar(make_view(bar, entry)))
        == "protection_target.stop_distance_above_maximum"
    )


def test_structural_reward_r_is_bounded_for_repeating_ratio(bar: Candle) -> None:
    entry = make_entry_feature(
        bar,
        entry_price="100.1",
        invalidation_price="99.8",
        target_price="101.44999999999999",
    )

    value = plan(
        ProtectionTargetModule(
            target_mode="structural",
            min_stop_distance_bps=0,
            max_stop_distance_bps=9999,
        ).on_bar(make_view(bar, entry))
    )

    assert value["take_profit"] == "101.44999999999999"
    assert len(Decimal(str(value["reward_r"])).as_tuple().digits) <= 128
    assert len(str(value["reward_r"])) <= 256


def test_structural_reward_boundaries(bar: Candle) -> None:
    minimum = make_entry_feature(bar, invalidation_price="99", target_price="100.5")
    assert (
        plan(
            ProtectionTargetModule(target_mode="structural", min_reward_r="0.5").on_bar(
                make_view(bar, minimum)
            )
        )["reward_r"]
        == "0.5"
    )
    low = make_entry_feature(bar, invalidation_price="99", target_price="100.49")
    assert (
        reason(
            ProtectionTargetModule(target_mode="structural", min_reward_r="0.5").on_bar(
                make_view(bar, low)
            )
        )
        == "protection_target.reward_r_below_minimum"
    )
    high = make_entry_feature(bar, invalidation_price="99", target_price="121")
    assert (
        reason(
            ProtectionTargetModule(target_mode="structural", max_reward_r="20").on_bar(
                make_view(bar, high)
            )
        )
        == "protection_target.reward_r_above_maximum"
    )


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("0"), "0"),
        (Decimal("-0"), "0"),
        (Decimal("100.000"), "100"),
        (Decimal("0.0012300"), "0.00123"),
        (Decimal("1E+8"), "100000000"),
        (Decimal("-1.200E-7"), "-0.00000012"),
    ],
)
def test_decimal_text_is_canonical(value: Decimal, text: str) -> None:
    assert decimal_text(value) == text


def test_decimal_text_rejects_nonfinite() -> None:
    with pytest.raises(ValueError):
        decimal_text(Decimal("NaN"))


def test_financial_math_is_independent_of_ambient_context(bar: Candle) -> None:
    entry = make_entry_feature(
        bar,
        entry_price="100.123456789123456789123456789",
        invalidation_price="99.987654321987654321987654321",
    )
    baseline = plan(ProtectionTargetModule(reward_r="2.5").on_bar(make_view(bar, entry)))
    original = getcontext().copy()
    try:
        getcontext().prec = 6
        low_precision = plan(ProtectionTargetModule(reward_r="2.5").on_bar(make_view(bar, entry)))
    finally:
        getcontext().prec = original.prec
    assert low_precision == baseline


def test_extreme_small_and_large_finite_values() -> None:
    from conftest import make_bar

    bar = make_bar()
    tiny = make_entry_feature(
        bar,
        entry_price="1E-1000",
        invalidation_price="9E-1001",
    )
    tiny_value = plan(
        ProtectionTargetModule(min_stop_distance_bps=0, max_stop_distance_bps=9999).on_bar(
            make_view(bar, tiny)
        )
    )
    assert Decimal(str(tiny_value["take_profit"])) > Decimal("1E-1000")
    huge = make_entry_feature(
        bar,
        plan_id="huge",
        setup_id="huge",
        entry_price="1E+1000",
        invalidation_price="9.9E+999",
    )
    huge_value = plan(
        ProtectionTargetModule(min_stop_distance_bps=0, max_stop_distance_bps=9999).on_bar(
            make_view(bar, huge)
        )
    )
    assert Decimal(str(huge_value["take_profit"])) > Decimal("1E+1000")


def test_maximum_supported_significant_digits_are_calculated_without_rounding(bar: Candle) -> None:
    entry_price = "9" * 128
    invalidation = str(int(entry_price) - 1)
    entry = make_entry_feature(
        bar,
        entry_price=entry_price,
        invalidation_price=invalidation,
    )
    value = plan(
        ProtectionTargetModule(
            min_stop_distance_bps=0,
            max_stop_distance_bps=5000,
            reward_r=3,
        ).on_bar(make_view(bar, entry))
    )
    assert value["risk_distance"] == "1"
    assert value["take_profit"] == str(int(entry_price) + 3)


@pytest.mark.parametrize(
    ("entry", "distance", "reward"),
    [
        ("10", "0.1", "1"),
        ("10", "0.5", "2.5"),
        ("100", "1", "3"),
        ("1000", "7.5", "4"),
        ("0.01", "0.0001", "2"),
        ("1E+1000", "1E+990", "1.5"),
    ],
)
def test_buy_sell_fixed_r_symmetry_property(
    bar: Candle, entry: str, distance: str, reward: str
) -> None:
    entry_decimal = Decimal(entry)
    distance_decimal = Decimal(distance)
    buy = make_entry_feature(
        bar,
        plan_id="sym-buy",
        setup_id="sym-buy",
        side="buy",
        entry_price=entry,
        invalidation_price=str(entry_decimal - distance_decimal),
    )
    sell = make_entry_feature(
        bar,
        plan_id="sym-sell",
        setup_id="sym-sell",
        side="sell",
        entry_price=entry,
        invalidation_price=str(entry_decimal + distance_decimal),
    )
    module_kwargs = {
        "reward_r": reward,
        "min_stop_distance_bps": 0,
        "max_stop_distance_bps": 9999,
    }
    buy_value = plan(ProtectionTargetModule(**module_kwargs).on_bar(make_view(bar, buy)))
    sell_value = plan(ProtectionTargetModule(**module_kwargs).on_bar(make_view(bar, sell)))
    assert Decimal(str(buy_value["risk_distance"])) == Decimal(str(sell_value["risk_distance"]))
    assert Decimal(str(buy_value["reward_distance"])) == Decimal(str(sell_value["reward_distance"]))
    assert Decimal(str(buy_value["take_profit"])) - entry_decimal == (
        entry_decimal - Decimal(str(sell_value["take_profit"]))
    )
