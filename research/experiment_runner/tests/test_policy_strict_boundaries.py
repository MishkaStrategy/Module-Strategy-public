from __future__ import annotations

import math
from typing import Any

import pytest

from modular_research_runner.models import SpecificationError
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.state import FailureCategory


@pytest.mark.parametrize(
    "changes",
    [
        {"max_attempts": True},
        {"max_attempts": 1.5},
        {"max_attempts": 0},
    ],
)
def test_retry_policy_rejects_invalid_direct_attempt_counts(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(SpecificationError, match="max_attempts must be positive"):
        RetryPolicy(**changes)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_backoff_seconds", math.nan),
        ("initial_backoff_seconds", math.inf),
        ("backoff_multiplier", -math.inf),
        ("maximum_backoff_seconds", math.nan),
        ("maximum_backoff_seconds", True),
    ],
)
def test_retry_policy_rejects_nonfinite_or_boolean_direct_numbers(
    field: str,
    value: Any,
) -> None:
    with pytest.raises(SpecificationError):
        RetryPolicy(**{field: value})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_retry_policy_mapping_rejects_nonfinite_numbers(value: float) -> None:
    with pytest.raises(SpecificationError, match="must be finite"):
        RetryPolicy.from_mapping({"initial_backoff_seconds": value})


def test_retry_policy_normalizes_valid_numeric_and_category_inputs() -> None:
    policy = RetryPolicy(
        initial_backoff_seconds=1,
        backoff_multiplier=3,
        maximum_backoff_seconds=9,
        retryable_categories={FailureCategory.TRANSIENT},
    )

    assert policy.initial_backoff_seconds == 1.0
    assert policy.backoff_multiplier == 3.0
    assert policy.maximum_backoff_seconds == 9.0
    assert policy.retryable_categories == frozenset({FailureCategory.TRANSIENT})


@pytest.mark.parametrize(
    "categories",
    [
        {"TRANSIENT"},
        ["TRANSIENT"],
        frozenset({object()}),
    ],
)
def test_retry_policy_rejects_untyped_direct_categories(categories: Any) -> None:
    with pytest.raises(SpecificationError, match="FailureCategory"):
        RetryPolicy(retryable_categories=categories)


def test_retry_policy_public_methods_reject_boolean_and_untyped_inputs() -> None:
    policy = RetryPolicy(max_attempts=2)

    with pytest.raises(SpecificationError, match="FailureCategory"):
        policy.can_retry("TRANSIENT", 1)  # type: ignore[arg-type]
    with pytest.raises(SpecificationError, match="non-negative integer"):
        policy.can_retry(FailureCategory.TRANSIENT, True)
    with pytest.raises(SpecificationError, match="attempt_count must be positive"):
        policy.delay_seconds(True)


def test_retry_policy_caps_overflowing_backoff() -> None:
    policy = RetryPolicy(
        max_attempts=10_000,
        initial_backoff_seconds=1.0,
        backoff_multiplier=10.0,
        maximum_backoff_seconds=300.0,
    )

    assert policy.delay_seconds(10_000) == 300.0


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_seconds": True},
        {"lease_seconds": 900.0},
        {"heartbeat_seconds": True},
        {"heartbeat_seconds": 60.0},
        {"recover_expired_leases": 1},
        {"verify_success_artifacts": 0},
    ],
)
def test_resume_policy_rejects_implicit_direct_coercions(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(SpecificationError):
        ResumePolicy(**changes)
