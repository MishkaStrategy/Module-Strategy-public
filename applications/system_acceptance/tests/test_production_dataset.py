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
from modular_system_acceptance.production_dataset import TrustedDatasetPhaseAdapter
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64


@dataclass(frozen=True)
class FakeDescriptor:
    dataset_id: str = "dataset-v1"
    version: int = 7
    dataset_hash: str = DATASET_DIGEST


@dataclass(frozen=True)
class FakeSelection:
    descriptor: FakeDescriptor = FakeDescriptor()
    timeframe: str = "5m"
    row_count: int = 12_345


class Resolver:
    def __init__(
        self,
        selection: FakeSelection | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.selection = selection or FakeSelection()
        self.error = error
        self.calls = 0

    def __call__(self) -> FakeSelection:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.selection


def identity() -> TrustedAcceptanceIdentity:
    return TrustedAcceptanceIdentity(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
    )


def context(
    *,
    phase: AcceptancePhase = AcceptancePhase.DATASET,
    dataset_digest: str = DATASET_DIGEST,
) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-dataset-test",
        phase=phase,
        attempt_id="attempt-dataset-test",
        recovering=False,
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=dataset_digest,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
        active_test_time=0.0,
        checkpoint_token=None,
        checkpoint_digest=None,
    )


def adapter(resolver: Resolver) -> TrustedDatasetPhaseAdapter:
    return TrustedDatasetPhaseAdapter(
        identity=identity(),
        resolver=resolver,
        version=7,
        timeframe="5m",
    )


def test_registered_dataset_execute_and_recovery_are_read_only_reverification() -> None:
    resolver = Resolver()
    current = adapter(resolver)

    first = current.executor.execute(context())
    second = current.executor.recover(context())

    assert first.status is PhaseOutcomeStatus.SUCCEEDED
    assert second == first
    assert resolver.calls == 2
    assert len(first.evidence) == 1
    evidence = first.evidence[0]
    assert evidence.kind == "dataset.registered"
    assert evidence.reference.startswith("dataset:")
    assert "dataset-v1" not in evidence.reference


def test_candidate_dataset_digest_mismatch_blocks_before_resolver() -> None:
    resolver = Resolver()
    current = adapter(resolver)

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(dataset_digest="f" * 64))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert resolver.calls == 0


@pytest.mark.parametrize(
    "selection",
    [
        FakeSelection(descriptor=FakeDescriptor(dataset_id="dataset-other")),
        FakeSelection(descriptor=FakeDescriptor(dataset_hash="e" * 64)),
        FakeSelection(descriptor=FakeDescriptor(version=8)),
        FakeSelection(timeframe="15m"),
    ],
)
def test_resolved_selection_must_match_trusted_deployment_scope(selection: FakeSelection) -> None:
    resolver = Resolver(selection)
    current = adapter(resolver)

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert resolver.calls == 1


@pytest.mark.parametrize(
    "selection",
    [
        FakeSelection(row_count=0),
        FakeSelection(row_count=-1),
        FakeSelection(descriptor=FakeDescriptor(version=0)),
    ],
)
def test_invalid_registered_selection_requires_recovery(selection: FakeSelection) -> None:
    resolver = Resolver(selection)
    current = adapter(resolver)

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_wrong_phase_is_blocked_before_registered_dataset_read() -> None:
    resolver = Resolver()
    current = adapter(resolver)

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.execute(context(phase=AcceptancePhase.RESEARCH))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert resolver.calls == 0


def test_registered_dataset_owner_exception_is_sanitized() -> None:
    resolver = Resolver(error=RuntimeError("/Users/private/market-data token=TOPSECRET"))
    current = adapter(resolver)

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.OWNER_FAILURE
    text = str(raised.value).lower()
    assert "/users/" not in text
    assert "topsecret" not in text
    assert raised.value.__cause__ is None
