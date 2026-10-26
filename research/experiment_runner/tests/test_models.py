from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from modular_research_runner import (
    ChronologicalSplitPolicy,
    DatasetLineage,
    DatasetQualityStatus,
    ParameterConstraint,
    ParameterDimension,
    ParameterKind,
    SearchSpace,
    SpecificationError,
    SplitMode,
    StagedSearchPolicy,
    TimeRange,
)


def test_time_range_normalizes_and_is_immutable() -> None:
    value = TimeRange(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        value.start = value.end  # type: ignore[misc]


def test_invalid_time_range_is_rejected() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(SpecificationError, match="later"):
        TimeRange(now, now)


def test_dataset_lineage_validates_hash_timezone_and_quality(overall_range: TimeRange) -> None:
    with pytest.raises(SpecificationError, match="SHA-256"):
        DatasetLineage(
            dataset_id="dataset",
            source="binance",
            market_type="binance-usdt-m-perpetual",
            instrument="BTCUSDT",
            timeframe="5m",
            period=overall_range,
            row_count=1,
            timezone="UTC",
            file_format="csv",
            file_size=1,
            sha256="bad",
            preprocessing_version="v1",
            aggregation_version="v1",
            metadata_version="v1",
        )
    with pytest.raises(SpecificationError, match="invalid datasets"):
        DatasetLineage(
            dataset_id="dataset",
            source="binance",
            market_type="binance-usdt-m-perpetual",
            instrument="BTCUSDT",
            timeframe="5m",
            period=overall_range,
            row_count=1,
            timezone="UTC",
            file_format="csv",
            file_size=1,
            sha256="a" * 64,
            preprocessing_version="v1",
            aggregation_version="v1",
            metadata_version="v1",
            quality_status=DatasetQualityStatus.INVALID,
        )


def test_parameter_dimension_rejects_duplicates_and_wrong_types() -> None:
    with pytest.raises(SpecificationError, match="duplicate"):
        ParameterDimension("ttl", ParameterKind.INTEGER, (24, 24))
    with pytest.raises(SpecificationError, match="integer"):
        ParameterDimension("ttl", ParameterKind.INTEGER, (True,))


def test_search_space_is_order_independent() -> None:
    first = SearchSpace(
        dimensions=(
            ParameterDimension("z", ParameterKind.INTEGER, (1, 2)),
            ParameterDimension("a", ParameterKind.BOOLEAN, (False, True)),
        )
    )
    second = SearchSpace(dimensions=tuple(reversed(first.dimensions)))
    assert tuple(item.name for item in first.dimensions) == ("a", "z")
    assert first.fingerprint == second.fingerprint


def test_constraint_evaluation_and_unknown_parameter() -> None:
    constraint = ParameterConstraint("minimum", "<=", right_parameter="maximum")
    assert constraint.accepts({"minimum": 1, "maximum": 2})
    assert not constraint.accepts({"minimum": 3, "maximum": 2})
    with pytest.raises(SpecificationError, match="unknown"):
        constraint.accepts({"minimum": 1})


def test_fixed_split_requires_all_ranges(overall_range: TimeRange) -> None:
    with pytest.raises(SpecificationError, match="requires"):
        ChronologicalSplitPolicy(mode=SplitMode.FIXED, train=overall_range)


def test_percentage_split_requires_exact_total() -> None:
    with pytest.raises(SpecificationError, match="sum to one"):
        ChronologicalSplitPolicy(
            mode=SplitMode.PERCENTAGE,
            train_fraction=0.5,
            validation_fraction=0.2,
            test_fraction=0.2,
        )


def test_research_specification_rejects_negative_seed(specification) -> None:
    from dataclasses import replace

    with pytest.raises(SpecificationError, match="non-negative integers"):
        replace(specification, seeds=(-1,))


def test_staged_search_policy_is_strict_and_deterministic() -> None:
    policy = StagedSearchPolicy(
        train_fractions=(0.2, 0.5, 1),
        promotion_counts=(10, 2),
    )
    assert policy.train_fractions == (0.2, 0.5, 1.0)
    assert (
        policy.fingerprint
        == StagedSearchPolicy(
            train_fractions=(0.2, 0.5, 1.0),
            promotion_counts=(10, 2),
        ).fingerprint
    )
    invalid = (
        ((0.5,), (1,), "at least two"),
        ((0.5, 0.4, 1.0), (2, 1), "strictly increasing"),
        ((0.25, 0.75), (1,), "must equal 1.0"),
        ((0.25, 0.5, 1.0), (2, 2), "strictly decreasing"),
        ((0.25, 0.5, 1.0), (2,), "one limit"),
    )
    for fractions, counts, message in invalid:
        with pytest.raises(SpecificationError, match=message):
            StagedSearchPolicy(
                train_fractions=fractions,
                promotion_counts=counts,
            )
