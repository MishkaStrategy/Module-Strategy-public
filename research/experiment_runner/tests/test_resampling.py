from __future__ import annotations

import pytest

from modular_research_runner.models import SpecificationError
from modular_research_runner.resampling import (
    BootstrapMethod,
    BootstrapPlanner,
    MonteCarloMethod,
    MonteCarloPathPlan,
    MonteCarloPlanner,
    ResampleIndexPlan,
    SequenceResampler,
    SignalSkipPlan,
    SignalSkipPlanner,
)


def test_iid_and_block_bootstrap_are_deterministic() -> None:
    planner = BootstrapPlanner()
    iid = planner.generate(population_size=8, sample_count=3, root_seed=7)
    assert iid == planner.generate(population_size=8, sample_count=3, root_seed=7)
    assert len({item.sample_id for item in iid}) == 3
    assert all(len(item.indices) == 8 for item in iid)
    assert all(item.method is BootstrapMethod.IID for item in iid)

    block = planner.generate(
        population_size=8,
        sample_count=2,
        root_seed=7,
        method=BootstrapMethod.MOVING_BLOCK,
        block_size=3,
    )
    assert all(item.block_size == 3 for item in block)
    assert all(len(item.indices) == 8 for item in block)
    assert block != iid[:2]


def test_sequence_resampler_applies_only_precomputed_indices() -> None:
    plan = BootstrapPlanner().generate(population_size=4, sample_count=1, root_seed=11)[0]
    result = SequenceResampler[str].apply(("a", "b", "c", "d"), plan)
    assert result == tuple(("a", "b", "c", "d")[index] for index in plan.indices)
    with pytest.raises(SpecificationError, match="empty"):
        SequenceResampler.apply((), plan)
    invalid = ResampleIndexPlan("bad", 1, (4,), BootstrapMethod.IID)
    with pytest.raises(SpecificationError, match="out-of-range"):
        SequenceResampler.apply((1, 2, 3, 4), invalid)


def test_monte_carlo_path_plans_are_deterministic_permutations() -> None:
    planner = MonteCarloPlanner()
    plans = planner.generate(population_size=12, scenario_count=4, root_seed=19)
    assert plans == planner.generate(population_size=12, scenario_count=4, root_seed=19)
    assert len({plan.scenario_id for plan in plans}) == 4
    assert len({plan.seed for plan in plans}) == 4
    assert all(plan.method is MonteCarloMethod.PERMUTATION for plan in plans)
    assert all(sorted(plan.indices) == list(range(12)) for plan in plans)


def test_monte_carlo_block_permutation_preserves_intra_block_order() -> None:
    plans = MonteCarloPlanner().generate(
        population_size=10,
        scenario_count=3,
        root_seed=23,
        method=MonteCarloMethod.BLOCK_PERMUTATION,
        block_size=3,
    )
    expected_blocks = {(0, 1, 2), (3, 4, 5), (6, 7, 8), (9,)}
    for plan in plans:
        assert plan.block_size == 3
        observed = []
        cursor = 0
        while cursor < len(plan.indices):
            start = plan.indices[cursor]
            size = 1 if start == 9 else 3
            observed.append(plan.indices[cursor : cursor + size])
            cursor += size
        assert set(observed) == expected_blocks


def test_sequence_resampler_accepts_monte_carlo_plan() -> None:
    plan = MonteCarloPlanner().generate(population_size=4, scenario_count=1, root_seed=29)[0]
    values = ("a", "b", "c", "d")
    assert SequenceResampler.apply(values, plan) == tuple(values[index] for index in plan.indices)


def test_signal_skip_plans_are_reproducible_and_never_empty() -> None:
    planner = SignalSkipPlanner()
    plans = planner.generate(
        signal_count=10,
        scenario_count=5,
        skip_probability=0.99,
        root_seed=42,
    )
    assert plans == planner.generate(
        signal_count=10,
        scenario_count=5,
        skip_probability=0.99,
        root_seed=42,
    )
    assert all(any(item.keep_mask) for item in plans)
    assert len({item.scenario_id for item in plans}) == 5


@pytest.mark.parametrize(
    "call, message",
    [
        (
            lambda: BootstrapPlanner().generate(population_size=0, sample_count=1, root_seed=1),
            "population",
        ),
        (
            lambda: BootstrapPlanner().generate(population_size=1, sample_count=0, root_seed=1),
            "sample_count",
        ),
        (
            lambda: BootstrapPlanner().generate(
                population_size=4,
                sample_count=1,
                root_seed=1,
                method=BootstrapMethod.MOVING_BLOCK,
                block_size=5,
            ),
            "block_size",
        ),
        (
            lambda: BootstrapPlanner().generate(
                population_size=4,
                sample_count=1,
                root_seed=1,
                method=BootstrapMethod.IID,
                block_size=2,
            ),
            "cannot define",
        ),
        (
            lambda: MonteCarloPlanner().generate(population_size=0, scenario_count=1, root_seed=1),
            "population",
        ),
        (
            lambda: MonteCarloPlanner().generate(
                population_size=4,
                scenario_count=1,
                root_seed=1,
                method=MonteCarloMethod.BLOCK_PERMUTATION,
                block_size=5,
            ),
            "block_size",
        ),
        (
            lambda: MonteCarloPlanner().generate(
                population_size=4,
                scenario_count=1,
                root_seed=1,
                method=MonteCarloMethod.PERMUTATION,
                block_size=2,
            ),
            "cannot define",
        ),
        (
            lambda: SignalSkipPlanner().generate(
                signal_count=0, scenario_count=1, skip_probability=0.1, root_seed=1
            ),
            "positive",
        ),
        (
            lambda: SignalSkipPlanner().generate(
                signal_count=1, scenario_count=1, skip_probability=True, root_seed=1
            ),
            "numeric",
        ),
        (
            lambda: SignalSkipPlanner().generate(
                signal_count=1, scenario_count=1, skip_probability=1.0, root_seed=1
            ),
            r"\[0, 1\)",
        ),
    ],
)
def test_resampling_validation_edges(call, message: str) -> None:
    with pytest.raises(SpecificationError, match=message):
        call()


def test_resampling_planners_enforce_preallocation_resource_limits() -> None:
    for planner in (
        BootstrapPlanner(maximum_total_indices=8),
        MonteCarloPlanner(maximum_total_indices=8),
        SignalSkipPlanner(maximum_total_indices=8),
    ):
        assert planner.maximum_total_indices == 8
    with pytest.raises(SpecificationError, match="maximum_total_indices"):
        BootstrapPlanner(maximum_total_indices=0)
    with pytest.raises(SpecificationError, match="maximum_total_indices"):
        BootstrapPlanner(maximum_total_indices=8).generate(
            population_size=3,
            sample_count=3,
            root_seed=1,
        )
    with pytest.raises(SpecificationError, match="maximum_total_indices"):
        MonteCarloPlanner(maximum_total_indices=8).generate(
            population_size=3,
            scenario_count=3,
            root_seed=1,
        )
    with pytest.raises(SpecificationError, match="maximum_total_indices"):
        SignalSkipPlanner(maximum_total_indices=8).generate(
            signal_count=3,
            scenario_count=3,
            skip_probability=0.1,
            root_seed=1,
        )


def test_resampling_planners_reject_boolean_counts_and_negative_seeds() -> None:
    with pytest.raises(SpecificationError, match="population_size"):
        BootstrapPlanner().generate(population_size=True, sample_count=1, root_seed=1)
    with pytest.raises(SpecificationError, match="root_seed"):
        MonteCarloPlanner().generate(population_size=1, scenario_count=1, root_seed=-1)
    with pytest.raises(SpecificationError, match="signal_count"):
        SignalSkipPlanner().generate(
            signal_count=True,
            scenario_count=1,
            skip_probability=0.1,
            root_seed=1,
        )


def test_plan_dataclasses_reject_inconsistent_values() -> None:
    with pytest.raises(SpecificationError, match="non-empty"):
        ResampleIndexPlan("", 1, (0,), BootstrapMethod.IID)
    with pytest.raises(SpecificationError, match="contain indices"):
        ResampleIndexPlan("x", 1, (), BootstrapMethod.IID)
    with pytest.raises(SpecificationError, match="non-negative"):
        ResampleIndexPlan("x", 1, (-1,), BootstrapMethod.IID)
    with pytest.raises(SpecificationError, match="requires"):
        ResampleIndexPlan("x", 1, (0,), BootstrapMethod.MOVING_BLOCK)
    with pytest.raises(SpecificationError, match="cannot define"):
        ResampleIndexPlan("x", 1, (0,), BootstrapMethod.IID, 1)
    with pytest.raises(SpecificationError, match="non-empty"):
        MonteCarloPathPlan("", 1, (0,), MonteCarloMethod.PERMUTATION)
    with pytest.raises(SpecificationError, match="complete zero-based"):
        MonteCarloPathPlan("x", 1, (0, 2), MonteCarloMethod.PERMUTATION)
    with pytest.raises(SpecificationError, match="requires block_size"):
        MonteCarloPathPlan("x", 1, (0,), MonteCarloMethod.BLOCK_PERMUTATION)
    with pytest.raises(SpecificationError, match="cannot define"):
        MonteCarloPathPlan("x", 1, (0,), MonteCarloMethod.PERMUTATION, 1)
    with pytest.raises(SpecificationError, match="retain"):
        SignalSkipPlan("x", 1, (False,), 0.5)
    with pytest.raises(SpecificationError, match="seed"):
        ResampleIndexPlan("x", -1, (0,), BootstrapMethod.IID)
    with pytest.raises(SpecificationError, match="integer tuple"):
        ResampleIndexPlan("x", 1, (True,), BootstrapMethod.IID)
    with pytest.raises(SpecificationError, match="BootstrapMethod"):
        ResampleIndexPlan("x", 1, (0,), "iid")  # type: ignore[arg-type]
    with pytest.raises(SpecificationError, match="seed"):
        MonteCarloPathPlan("x", -1, (0,), MonteCarloMethod.PERMUTATION)
    with pytest.raises(SpecificationError, match="integer tuple"):
        MonteCarloPathPlan("x", 1, (True,), MonteCarloMethod.PERMUTATION)
    with pytest.raises(SpecificationError, match="MonteCarloMethod"):
        MonteCarloPathPlan("x", 1, (0,), "permutation")  # type: ignore[arg-type]
    with pytest.raises(SpecificationError, match="scenario_id"):
        SignalSkipPlan("", 1, (True,), 0.5)
    with pytest.raises(SpecificationError, match="seed"):
        SignalSkipPlan("x", -1, (True,), 0.5)
    with pytest.raises(SpecificationError, match="boolean tuple"):
        SignalSkipPlan("x", 1, (1,), 0.5)  # type: ignore[arg-type]
