from __future__ import annotations

from dataclasses import dataclass

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcomeStatus,
)
from modular_system_acceptance.production_research import (
    TrustedAcceptanceIdentity,
    TrustedResearchPhaseAdapter,
)

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
RESEARCH_DIGEST = "e" * 64
EXPERIMENT_ID = "experiment-research-v1"


@dataclass(frozen=True)
class FakeSpecification:
    fingerprint: str = RESEARCH_DIGEST


@dataclass(frozen=True)
class FakeManifest:
    experiment_id: str = EXPERIMENT_ID


@dataclass(frozen=True)
class FakePrepared:
    specification: FakeSpecification = FakeSpecification()
    manifest: FakeManifest = FakeManifest()


@dataclass(frozen=True)
class FakeSummary:
    status: object


class FakeRepository:
    def __init__(self, status: object) -> None:
        self.status = status
        self.queries: list[str] = []

    def experiment_status(self, experiment_id: str) -> object:
        self.queries.append(experiment_id)
        return self.status


class FakeRunner:
    def __init__(
        self,
        repository: FakeRepository,
        *,
        final_status: object = "COMPLETED",
        error: Exception | None = None,
    ) -> None:
        self.repository = repository
        self.final_status = final_status
        self.error = error
        self.executions: list[str] = []

    def execute(self, experiment_id: str) -> FakeSummary:
        self.executions.append(experiment_id)
        if self.error is not None:
            raise self.error
        self.repository.status = self.final_status
        return FakeSummary(self.final_status)


def identity() -> TrustedAcceptanceIdentity:
    return TrustedAcceptanceIdentity(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
    )


def context(*, full_rc_sha: str = FULL_RC) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-research-test",
        phase=AcceptancePhase.RESEARCH,
        attempt_id="attempt-research-test",
        recovering=False,
        full_rc_sha=full_rc_sha,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
        active_test_time=0.0,
        checkpoint_token=None,
        checkpoint_digest=None,
    )


def adapter(
    status: object,
    *,
    final_status: object = "COMPLETED",
    error: Exception | None = None,
    prepared: FakePrepared | None = None,
) -> tuple[TrustedResearchPhaseAdapter, FakeRunner]:
    repository = FakeRepository(status)
    runner = FakeRunner(repository, final_status=final_status, error=error)
    current = TrustedResearchPhaseAdapter(
        identity=identity(),
        runner=runner,
        prepared=prepared or FakePrepared(),
        specification_fingerprint=RESEARCH_DIGEST,
    )
    return current, runner


def test_completed_research_replays_without_second_execute() -> None:
    current, runner = adapter("COMPLETED")

    first = current.executor.execute(context())
    second = current.executor.recover(context())

    assert first.status is PhaseOutcomeStatus.SUCCEEDED
    assert second == first
    assert runner.executions == []
    assert runner.repository.queries == [EXPERIMENT_ID, EXPERIMENT_ID]
    assert len(first.evidence) == 1
    evidence = first.evidence[0]
    assert evidence.kind == "research.result"
    assert evidence.reference.startswith("research:")
    assert EXPERIMENT_ID not in evidence.reference


def test_active_research_resumes_same_durable_experiment() -> None:
    current, runner = adapter("RUNNING", final_status="COMPLETED")

    outcome = current.executor.recover(context())

    assert outcome.status is PhaseOutcomeStatus.SUCCEEDED
    assert runner.executions == [EXPERIMENT_ID]
    assert runner.repository.queries == [EXPERIMENT_ID]


def test_initial_execution_uses_existing_prepared_experiment() -> None:
    current, runner = adapter("PLANNED", final_status="COMPLETED")

    outcome = current.executor.execute(context())

    assert outcome.status is PhaseOutcomeStatus.SUCCEEDED
    assert runner.executions == [EXPERIMENT_ID]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("BLOCKED", PhaseOutcomeStatus.BLOCKED),
        ("FAILED", PhaseOutcomeStatus.FAILED),
        ("COMPLETED_WITH_FAILURES", PhaseOutcomeStatus.FAILED),
        ("CORRUPTED", PhaseOutcomeStatus.FAILED),
        ("CANCELED", PhaseOutcomeStatus.CANCELLED),
    ],
)
def test_terminal_non_success_research_never_reexecutes(
    status: str,
    expected: PhaseOutcomeStatus,
) -> None:
    current, runner = adapter(status)

    outcome = current.executor.recover(context())

    assert outcome.status is expected
    assert runner.executions == []


def test_candidate_identity_mismatch_fails_closed_before_research_query() -> None:
    current, runner = adapter("PLANNED")

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(full_rc_sha="f" * 40))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert runner.repository.queries == []
    assert runner.executions == []


def test_prepared_specification_must_match_trusted_deployment_identity() -> None:
    prepared = FakePrepared(specification=FakeSpecification("f" * 64))
    repository = FakeRepository("PLANNED")
    runner = FakeRunner(repository)

    with pytest.raises(AcceptanceOwnerError) as raised:
        TrustedResearchPhaseAdapter(
            identity=identity(),
            runner=runner,
            prepared=prepared,
            specification_fingerprint=RESEARCH_DIGEST,
        )

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED


def test_research_owner_exception_is_sanitized_by_native_boundary() -> None:
    current, _ = adapter(
        "RUNNING",
        error=RuntimeError("/Users/private/research.db token=TOPSECRET"),
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.OWNER_FAILURE
    text = str(raised.value).lower()
    assert "/users/" not in text
    assert "topsecret" not in text
    assert raised.value.__cause__ is None


def test_unknown_or_nonterminal_summary_status_requires_recovery() -> None:
    current, runner = adapter("RUNNING", final_status="RUNNING")

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED
    assert runner.executions == [EXPERIMENT_ID]


def test_invalid_repository_status_requires_recovery_without_execute() -> None:
    current, runner = adapter(object())

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED
    assert runner.executions == []
