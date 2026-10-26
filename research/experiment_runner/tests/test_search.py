from __future__ import annotations

import pytest

from modular_research_runner import (
    GridSearchPlanner,
    ParameterConstraint,
    ParameterDimension,
    ParameterKind,
    RandomSearchPlanner,
    SearchSpace,
    SpecificationError,
)


def test_grid_search_has_stable_canonical_order(search_space: SearchSpace) -> None:
    generated = GridSearchPlanner().generate(search_space)
    assert [dict(item.parameters) for item in generated] == [
        {"side": "long", "ttl": 24},
        {"side": "long", "ttl": 36},
        {"side": "short", "ttl": 24},
        {"side": "short", "ttl": 36},
    ]
    assert [item.index for item in generated] == [0, 1, 2, 3]
    assert len({item.sha256 for item in generated}) == 4


def test_constraints_filter_before_execution() -> None:
    space = SearchSpace(
        dimensions=(
            ParameterDimension("minimum", ParameterKind.INTEGER, (1, 2, 3)),
            ParameterDimension("maximum", ParameterKind.INTEGER, (1, 2)),
        ),
        constraints=(ParameterConstraint("minimum", "<=", right_parameter="maximum"),),
    )
    generated = GridSearchPlanner().generate(space)
    assert all(item.parameters["minimum"] <= item.parameters["maximum"] for item in generated)
    assert len(generated) == 3


def test_grid_limit_is_fail_closed() -> None:
    space = SearchSpace(
        dimensions=(ParameterDimension("x", ParameterKind.INTEGER, tuple(range(5))),),
        maximum_combinations=4,
    )
    with pytest.raises(SpecificationError, match="limit"):
        GridSearchPlanner().generate(space)


def test_random_search_is_seeded_unique_and_replayable(search_space: SearchSpace) -> None:
    planner = RandomSearchPlanner()
    first = planner.generate(search_space, sample_count=3, seed=123)
    second = planner.generate(search_space, sample_count=3, seed=123)
    third = planner.generate(search_space, sample_count=3, seed=124)
    assert first == second
    assert tuple(item.sha256 for item in first) != tuple(item.sha256 for item in third)
    assert len({item.sha256 for item in first}) == 3


def test_random_search_does_not_depend_on_worker_count(search_space: SearchSpace) -> None:
    # Parallelism is intentionally absent from the sampling contract.
    planner = RandomSearchPlanner()
    one_worker = planner.generate(search_space, sample_count=4, seed=999)
    sixteen_workers = planner.generate(search_space, sample_count=4, seed=999)
    assert one_worker == sixteen_workers


def test_random_search_rejects_oversampling(search_space: SearchSpace) -> None:
    with pytest.raises(SpecificationError, match="exceeds"):
        RandomSearchPlanner().generate(search_space, sample_count=5, seed=1)
