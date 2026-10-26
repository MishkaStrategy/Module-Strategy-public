from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_research_runner.engine import BacktestRunReference
from modular_research_runner.models import SpecificationError
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.state import (
    FailureCategory,
    FailureRecord,
    RunState,
    RunStatus,
    StateTransitionError,
    ensure_transition,
)

NOW = datetime(2026, 2, 1, tzinfo=UTC)


def test_retry_policy_mapping_and_backoff() -> None:
    policy = RetryPolicy.from_mapping(
        {
            "max_attempts": 4,
            "initial_backoff_seconds": 2,
            "backoff_multiplier": 3,
            "maximum_backoff_seconds": 10,
            "retryable_categories": ["transient", "RESOURCE"],
        }
    )
    assert policy.can_retry(FailureCategory.TRANSIENT, 3)
    assert not policy.can_retry(FailureCategory.ENGINE, 1)
    assert not policy.can_retry(FailureCategory.RESOURCE, 4)
    assert policy.delay_seconds(1) == 2
    assert policy.delay_seconds(2) == 6
    assert policy.delay_seconds(3) == 10


@pytest.mark.parametrize(
    "mapping, message",
    [
        ({"unknown": 1}, "unknown"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"initial_backoff_seconds": -1}, "initial_backoff"),
        ({"backoff_multiplier": 0.5}, "backoff_multiplier"),
        ({"retryable_categories": "TRANSIENT"}, "sequence"),
        ({"retryable_categories": ["not-real"]}, "unknown failure"),
    ],
)
def test_retry_policy_rejects_invalid_mapping(mapping, message: str) -> None:
    with pytest.raises(SpecificationError, match=message):
        RetryPolicy.from_mapping(mapping)


def test_retry_policy_rejects_inconsistent_direct_values() -> None:
    with pytest.raises(SpecificationError, match="maximum backoff"):
        RetryPolicy(initial_backoff_seconds=2, maximum_backoff_seconds=1)
    with pytest.raises(SpecificationError, match="positive"):
        RetryPolicy().delay_seconds(0)


def test_resume_policy_mapping_and_validation() -> None:
    policy = ResumePolicy.from_mapping(
        {
            "lease_seconds": 120,
            "heartbeat_seconds": 20,
            "recover_expired_leases": False,
            "verify_success_artifacts": False,
        }
    )
    assert policy == ResumePolicy(120, 20, False, False)
    with pytest.raises(SpecificationError, match="unknown"):
        ResumePolicy.from_mapping({"x": True})
    with pytest.raises(SpecificationError, match="boolean"):
        ResumePolicy.from_mapping({"recover_expired_leases": 1})
    with pytest.raises(SpecificationError, match="below"):
        ResumePolicy(lease_seconds=10, heartbeat_seconds=10)


def test_transition_matrix_rejects_terminal_reentry() -> None:
    ensure_transition(RunStatus.PLANNED, RunStatus.QUEUED)
    ensure_transition(RunStatus.QUEUED, RunStatus.QUEUED)
    with pytest.raises(StateTransitionError, match="illegal"):
        ensure_transition(RunStatus.SUCCEEDED, RunStatus.QUEUED)


def test_failure_and_run_state_validate_invariants() -> None:
    with pytest.raises(SpecificationError, match="non-empty"):
        FailureRecord(FailureCategory.UNKNOWN, " ", NOW, False)
    with pytest.raises(SpecificationError, match="timezone"):
        FailureRecord(FailureCategory.UNKNOWN, "x", datetime(2026, 1, 1), False)
    failure = FailureRecord(FailureCategory.INPUT, "bad", NOW, False, "ValueError")
    with pytest.raises(SpecificationError, match="active lease"):
        RunState("run", "exp", RunStatus.RUNNING, 1, 1, NOW, NOW, NOW, 0, failure=failure)
    with pytest.raises(SpecificationError, match="only a running"):
        RunState(
            "run",
            "exp",
            RunStatus.QUEUED,
            0,
            1,
            NOW,
            NOW,
            NOW,
            0,
            lease_owner="w",
            lease_expires_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(SpecificationError, match="successful"):
        RunState("run", "exp", RunStatus.SUCCEEDED, 1, 1, NOW, NOW, NOW, 0)
    result = BacktestRunReference("run", "engine", Path("out"), 1, True)
    with pytest.raises(SpecificationError, match="only a successful"):
        RunState("run", "exp", RunStatus.FAILED, 1, 1, NOW, NOW, NOW, 0, result=result)
