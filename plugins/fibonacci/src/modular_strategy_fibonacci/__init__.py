"""Sequential full-endpoint Fibonacci feature producer for Modular Strategy Core."""

from __future__ import annotations

from typing import Any

__version__ = "0.4.0"

from .core_contracts import ACTIVE_PROJECTION_KEY, COMPLETED_WAVE_KEY, SHORT_SETUP_KEY
from .contracts import (
    Candle,
    FibonacciWaveSnapshot,
    FibonacciWorkflowSnapshot,
    WorkflowEventType,
    WorkflowPhase,
)
from .corrections.short_setup import (
    BearishContinuationSetup,
    BearishReactionObservation,
    BearishReactionProfile,
    BearishReactionRegimeProfile,
    active_bearish_continuation_setup,
    bearish_reaction_observations,
    build_bearish_reaction_profile,
    build_bearish_reaction_regime_profile,
)
from .workflow.state_machine import SequentialFibonacciEngine, replay_sequential_fibonacci

_CORE_EXPORTS = {
    "FibonacciCoreStateError",
    "FibonacciModule",
}


def __getattr__(name: str) -> Any:
    if name in _CORE_EXPORTS:
        from . import core_adapter_v4

        return getattr(core_adapter_v4, name)
    raise AttributeError(name)


__all__ = [
    "__version__",
    "ACTIVE_PROJECTION_KEY",
    "COMPLETED_WAVE_KEY",
    "SHORT_SETUP_KEY",
    "BearishContinuationSetup",
    "BearishReactionObservation",
    "BearishReactionProfile",
    "BearishReactionRegimeProfile",
    "Candle",
    "FibonacciCoreStateError",
    "FibonacciModule",
    "FibonacciWaveSnapshot",
    "FibonacciWorkflowSnapshot",
    "SequentialFibonacciEngine",
    "WorkflowEventType",
    "WorkflowPhase",
    "active_bearish_continuation_setup",
    "bearish_reaction_observations",
    "build_bearish_reaction_profile",
    "build_bearish_reaction_regime_profile",
    "replay_sequential_fibonacci",
]
