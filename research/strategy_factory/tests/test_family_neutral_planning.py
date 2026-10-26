from __future__ import annotations

from dataclasses import replace

import pytest

from strategy_factory.errors import ContractError
from strategy_factory.stage_handlers import (
    _family_architecture_limits,
    _family_balanced_architecture_allocations,
    _family_balanced_plan_allocations,
)


def _architecture(base, *, family: str, suffix: str):
    return replace(
        base,
        architecture_id=f"arch-{suffix}",
        signal=replace(
            base.signal,
            module_id=family,
            import_path=f"fixture:{family}",
        ),
    )


def _summary(allocations):
    return {
        architecture_id: (
            item.family_id,
            item.family_capacity,
            item.architecture_capacity,
        )
        for architecture_id, item in allocations.items()
    }


def test_family_budget_is_equal_across_three_strategy_families(architecture) -> None:
    architectures = (
        _architecture(architecture, family="breakout_return", suffix="breakout"),
        _architecture(architecture, family="liquidity_sweep", suffix="sweep"),
        _architecture(architecture, family="fvg_rt", suffix="fvg"),
    )

    allocations, families, unused = _family_balanced_architecture_allocations(
        architectures,
        10,
    )

    assert _summary(allocations) == {
        "arch-breakout": ("breakout_return", 3, 3),
        "arch-sweep": ("liquidity_sweep", 3, 3),
        "arch-fvg": ("fvg_rt", 3, 3),
    }
    assert tuple(item["family_id"] for item in families) == (
        "breakout_return",
        "fvg_rt",
        "liquidity_sweep",
    )
    assert all(item["family_capacity"] == 3 for item in families)
    assert unused == 1


def test_family_budget_is_divided_inside_family_without_cross_family_bias(architecture) -> None:
    architectures = (
        _architecture(architecture, family="breakout_return", suffix="breakout-a"),
        _architecture(architecture, family="breakout_return", suffix="breakout-b"),
        _architecture(architecture, family="liquidity_sweep", suffix="sweep"),
    )

    allocations, families, unused = _family_balanced_architecture_allocations(
        architectures,
        12,
    )

    assert _summary(allocations) == {
        "arch-breakout-a": ("breakout_return", 6, 3),
        "arch-breakout-b": ("breakout_return", 6, 3),
        "arch-sweep": ("liquidity_sweep", 6, 6),
    }
    assert families == (
        {
            "family_id": "breakout_return",
            "family_capacity": 6,
            "architecture_count": 2,
            "architecture_capacity": 3,
            "allocated_capacity": 6,
            "unused_capacity": 0,
        },
        {
            "family_id": "liquidity_sweep",
            "family_capacity": 6,
            "architecture_count": 1,
            "architecture_capacity": 6,
            "allocated_capacity": 6,
            "unused_capacity": 0,
        },
    )
    assert unused == 0


def test_family_budget_fails_closed_when_not_every_family_can_receive_one_point(
    architecture,
) -> None:
    architectures = (
        _architecture(architecture, family="breakout_return", suffix="breakout"),
        _architecture(architecture, family="liquidity_sweep", suffix="sweep"),
        _architecture(architecture, family="fvg_rt", suffix="fvg"),
    )

    with pytest.raises(ContractError, match="every strategy family"):
        _family_balanced_architecture_allocations(architectures, 2)


def test_family_budget_fails_closed_when_family_capacity_cannot_cover_architectures(
    architecture,
) -> None:
    architectures = (
        _architecture(architecture, family="breakout_return", suffix="breakout-a"),
        _architecture(architecture, family="breakout_return", suffix="breakout-b"),
        _architecture(architecture, family="breakout_return", suffix="breakout-c"),
        _architecture(architecture, family="liquidity_sweep", suffix="sweep"),
    )

    with pytest.raises(ContractError, match="every architecture in family breakout_return"):
        _family_balanced_architecture_allocations(architectures, 4)


def test_family_budget_is_independent_of_input_architecture_order(architecture) -> None:
    architectures = (
        _architecture(architecture, family="liquidity_sweep", suffix="sweep"),
        _architecture(architecture, family="breakout_return", suffix="breakout-b"),
        _architecture(architecture, family="fvg_rt", suffix="fvg"),
        _architecture(architecture, family="breakout_return", suffix="breakout-a"),
    )

    forward = _family_balanced_architecture_allocations(architectures, 13)
    reverse = _family_balanced_architecture_allocations(tuple(reversed(architectures)), 13)

    assert _summary(forward[0]) == _summary(reverse[0])
    assert forward[1:] == reverse[1:]


def test_single_family_retains_bounded_architecture_planning(architecture) -> None:
    architectures = (
        _architecture(architecture, family="fvg_rt", suffix="fvg-a"),
        _architecture(architecture, family="fvg_rt", suffix="fvg-b"),
    )

    allocations, families, unused = _family_balanced_architecture_allocations(
        architectures,
        5,
    )

    assert _summary(allocations) == {
        "arch-fvg-a": ("fvg_rt", 5, 2),
        "arch-fvg-b": ("fvg_rt", 5, 2),
    }
    assert families[0]["allocated_capacity"] == 4
    assert families[0]["unused_capacity"] == 1
    assert unused == 1


def test_architecture_slots_are_equal_across_three_families() -> None:
    limits, unused = _family_architecture_limits(
        frozenset({"breakout_return", "liquidity_sweep", "fvg_rt"}),
        12,
    )

    assert limits == {
        "breakout_return": 4,
        "fvg_rt": 4,
        "liquidity_sweep": 4,
    }
    assert unused == 0


def test_architecture_slots_fail_closed_when_one_family_would_be_omitted() -> None:
    with pytest.raises(ContractError, match="every strategy family"):
        _family_architecture_limits(
            frozenset({"breakout_return", "liquidity_sweep", "fvg_rt"}),
            2,
        )


def test_promotion_budget_is_global_and_equal_across_families() -> None:
    plans = (
        ("plan-breakout-a", "breakout_return", 8),
        ("plan-breakout-b", "breakout_return", 8),
        ("plan-fvg-a", "fvg_rt", 8),
        ("plan-fvg-b", "fvg_rt", 8),
        ("plan-sweep-a", "liquidity_sweep", 8),
        ("plan-sweep-b", "liquidity_sweep", 8),
    )

    allocations, families, unused = _family_balanced_plan_allocations(plans, 12)

    assert {key: value.plan_capacity for key, value in allocations.items()} == {
        "plan-breakout-a": 2,
        "plan-breakout-b": 2,
        "plan-fvg-a": 2,
        "plan-fvg-b": 2,
        "plan-sweep-a": 2,
        "plan-sweep-b": 2,
    }
    assert all(item["family_capacity"] == 4 for item in families)
    assert sum(value.plan_capacity for value in allocations.values()) == 12
    assert unused == 0


def test_promotion_budget_does_not_redistribute_small_plan_capacity() -> None:
    plans = (
        ("plan-breakout", "breakout_return", 1),
        ("plan-fvg", "fvg_rt", 8),
        ("plan-sweep", "liquidity_sweep", 8),
    )

    allocations, families, unused = _family_balanced_plan_allocations(plans, 12)

    assert allocations["plan-breakout"].plan_capacity == 1
    assert allocations["plan-fvg"].plan_capacity == 4
    assert allocations["plan-sweep"].plan_capacity == 4
    assert (
        next(item for item in families if item["family_id"] == "breakout_return")["unused_capacity"]
        == 3
    )
    assert unused == 3


def test_promotion_budget_fails_closed_when_family_cannot_cover_every_plan() -> None:
    plans = (
        ("plan-breakout-a", "breakout_return", 8),
        ("plan-breakout-b", "breakout_return", 8),
        ("plan-fvg", "fvg_rt", 8),
        ("plan-sweep", "liquidity_sweep", 8),
    )

    with pytest.raises(ContractError, match="every plan in family breakout_return"):
        _family_balanced_plan_allocations(plans, 3)
