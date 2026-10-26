from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
)
from modular_system_acceptance.composition import (
    build_complete_acceptance_registry,
    required_non_soak_phases,
)
from modular_system_acceptance.executors import CheckpointedSoakExecutor

NOW = datetime(2026, 8, 9, 0, 30, tzinfo=UTC)


class Executor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


def soak() -> CheckpointedSoakExecutor:
    return CheckpointedSoakExecutor(lambda _: True, clock=lambda: NOW)


def test_complete_registry_requires_every_non_soak_phase() -> None:
    required = required_non_soak_phases()
    assert AcceptancePhase.SOAK_2H not in required
    assert AcceptancePhase.SOAK_24H not in required
    assert AcceptancePhase.SOAK_72H not in required
    assert required[0] is AcceptancePhase.ENVIRONMENT
    assert required[-1] is AcceptancePhase.FINAL_VERIFICATION

    registry = build_complete_acceptance_registry(
        {phase: Executor() for phase in required},
        soak=soak(),
    )

    for phase in AcceptancePhase:
        assert registry.for_phase(phase) is not None


def test_missing_non_soak_phase_fails_closed() -> None:
    mapping = {phase: Executor() for phase in required_non_soak_phases()}
    mapping.pop(AcceptancePhase.RECOVERY)

    with pytest.raises(AcceptanceOwnerError) as raised:
        build_complete_acceptance_registry(mapping, soak=soak())

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert "RECOVERY" in str(raised.value)


def test_soak_phase_cannot_be_smuggled_into_non_soak_mapping() -> None:
    mapping = {phase: Executor() for phase in required_non_soak_phases()}
    mapping[AcceptancePhase.SOAK_2H] = Executor()

    with pytest.raises(AcceptanceOwnerError) as raised:
        build_complete_acceptance_registry(mapping, soak=soak())

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert "SOAK_2H" in str(raised.value)
