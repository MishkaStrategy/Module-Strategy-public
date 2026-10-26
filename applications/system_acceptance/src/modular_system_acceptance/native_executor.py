from __future__ import annotations

from collections.abc import Callable

from .contracts import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseOutcome,
)
from .owner import AcceptancePhaseExecutor, PhaseExecutionContext


class NativeOwnerPhaseExecutor(AcceptancePhaseExecutor):
    """Bind one acceptance phase to trusted owner-native execute/recover callables."""

    def __init__(
        self,
        phase: AcceptancePhase,
        *,
        execute: Callable[[PhaseExecutionContext], PhaseOutcome],
        recover: Callable[[PhaseExecutionContext], PhaseOutcome],
    ) -> None:
        if not isinstance(phase, AcceptancePhase):
            raise TypeError("phase must be AcceptancePhase")
        if not callable(execute) or not callable(recover):
            raise TypeError("execute and recover must be callable")
        self.phase = phase
        self._execute = execute
        self._recover = recover

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._validate_context(context)
        return self._result(self._execute, context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._validate_context(context)
        return self._result(self._recover, context)

    def _validate_context(self, context: PhaseExecutionContext) -> None:
        if not isinstance(context, PhaseExecutionContext) or context.phase is not self.phase:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "native acceptance phase received the wrong owner context",
            )

    @staticmethod
    def _result(
        callback: Callable[[PhaseExecutionContext], PhaseOutcome],
        context: PhaseExecutionContext,
    ) -> PhaseOutcome:
        try:
            outcome = callback(context)
        except AcceptanceOwnerError:
            raise
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.OWNER_FAILURE,
                "native acceptance phase owner failed",
            ) from None
        if not isinstance(outcome, PhaseOutcome):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_FAILURE,
                "native acceptance phase returned an invalid outcome",
            )
        return outcome
