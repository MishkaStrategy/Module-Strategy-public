from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_research_runner import (
    ChronologicalSplitPolicy,
    SpecificationError,
    SplitMode,
    TimeRange,
    WalkForwardMode,
    WalkForwardPolicy,
    plan_chronological_split,
    plan_walk_forward,
)


def test_fixed_chronological_split_has_required_gaps(
    overall_range: TimeRange,
    split_policy: ChronologicalSplitPolicy,
) -> None:
    planned = plan_chronological_split(split_policy, overall_range)
    assert planned.train.end < planned.validation.start
    assert planned.validation.end < planned.test.start
    assert planned.split_id.startswith("split-")


def test_overlapping_fixed_split_is_blocked(overall_range: TimeRange) -> None:
    policy = ChronologicalSplitPolicy(
        mode=SplitMode.FIXED,
        train=TimeRange(
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2022, 1, 1, tzinfo=UTC),
        ),
        validation=TimeRange(
            datetime(2021, 12, 31, tzinfo=UTC),
            datetime(2023, 1, 1, tzinfo=UTC),
        ),
        test=TimeRange(
            datetime(2023, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        ),
    )
    with pytest.raises(SpecificationError, match="overlap"):
        plan_chronological_split(policy, overall_range)


def test_percentage_split_is_deterministic_and_contained(overall_range: TimeRange) -> None:
    policy = ChronologicalSplitPolicy(
        mode=SplitMode.PERCENTAGE,
        train_fraction=0.6,
        validation_fraction=0.2,
        test_fraction=0.2,
        gap_seconds=300,
    )
    first = plan_chronological_split(policy, overall_range)
    second = plan_chronological_split(policy, overall_range)
    assert first == second
    assert first.train.start == overall_range.start
    assert first.test.end == overall_range.end
    assert first.train.end < first.validation.start < first.test.start


def test_percentage_split_supports_final_untouched_test(overall_range: TimeRange) -> None:
    policy = ChronologicalSplitPolicy(
        mode=SplitMode.PERCENTAGE,
        train_fraction=0.5,
        validation_fraction=0.2,
        test_fraction=0.2,
        final_fraction=0.1,
        gap_seconds=60,
    )
    planned = plan_chronological_split(policy, overall_range)
    assert planned.final_untouched_test is not None
    assert planned.final_untouched_test.end == overall_range.end
    assert planned.test.end < planned.final_untouched_test.start


def test_rolling_walk_forward_is_chronological() -> None:
    overall = TimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 5, 1, tzinfo=UTC),
    )
    policy = WalkForwardPolicy(
        mode=WalkForwardMode.ROLLING,
        train_seconds=30 * 86400,
        validation_seconds=10 * 86400,
        oos_seconds=10 * 86400,
        step_seconds=10 * 86400,
        purge_seconds=86400,
        embargo_seconds=86400,
    )
    windows = plan_walk_forward(policy, overall)
    assert len(windows) > 1
    assert windows[0].train.start < windows[0].train.end < windows[0].validation.start
    assert windows[0].validation.end < windows[0].oos.start
    assert windows[1].train.start > windows[0].train.start


def test_anchored_walk_forward_expands_train() -> None:
    overall = TimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 5, 1, tzinfo=UTC),
    )
    policy = WalkForwardPolicy(
        mode=WalkForwardMode.ANCHORED,
        train_seconds=30 * 86400,
        validation_seconds=10 * 86400,
        oos_seconds=10 * 86400,
        step_seconds=10 * 86400,
    )
    windows = plan_walk_forward(policy, overall)
    assert windows[0].train.start == windows[1].train.start == overall.start
    assert windows[1].train.end > windows[0].train.end


def test_walk_forward_requires_complete_window() -> None:
    overall = TimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 10, tzinfo=UTC),
    )
    policy = WalkForwardPolicy(
        mode=WalkForwardMode.ROLLING,
        train_seconds=30 * 86400,
        validation_seconds=10 * 86400,
        oos_seconds=10 * 86400,
        step_seconds=10 * 86400,
    )
    with pytest.raises(SpecificationError, match="no complete"):
        plan_walk_forward(policy, overall)
