from __future__ import annotations

from collections.abc import Mapping

from .contracts import AcceptanceErrorCode, AcceptanceOwnerError, AcceptancePhase, SOAK_PHASES
from .executors import CheckpointedSoakExecutor
from .owner import AcceptanceExecutorRegistry, AcceptancePhaseExecutor

_NON_SOAK_PHASES = frozenset(phase for phase in AcceptancePhase if phase not in SOAK_PHASES)


def build_complete_acceptance_registry(
    non_soak: Mapping[AcceptancePhase, AcceptancePhaseExecutor],
    *,
    soak: CheckpointedSoakExecutor,
) -> AcceptanceExecutorRegistry:
    """Build a production registry only when every required owner phase is explicit."""
    if not isinstance(non_soak, Mapping):
        raise TypeError("non_soak must be a mapping")
    if not isinstance(soak, CheckpointedSoakExecutor):
        raise TypeError("soak must be CheckpointedSoakExecutor")
    supplied = set(non_soak)
    if supplied != set(_NON_SOAK_PHASES):
        missing = sorted(phase.value for phase in _NON_SOAK_PHASES - supplied)
        unexpected = sorted(phase.value for phase in supplied - _NON_SOAK_PHASES)
        detail = "incomplete owner phase composition"
        if missing:
            detail += "; missing=" + ",".join(missing)
        if unexpected:
            detail += "; unexpected=" + ",".join(unexpected)
        raise AcceptanceOwnerError(AcceptanceErrorCode.PHASE_BLOCKED, detail)
    executors: dict[AcceptancePhase, AcceptancePhaseExecutor] = dict(non_soak)
    for phase in SOAK_PHASES:
        executors[phase] = soak
    return AcceptanceExecutorRegistry(executors)


def required_non_soak_phases() -> tuple[AcceptancePhase, ...]:
    """Stable phase order for deployment/configuration diagnostics."""
    return tuple(phase for phase in AcceptancePhase if phase in _NON_SOAK_PHASES)
