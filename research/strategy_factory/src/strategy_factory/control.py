"""Run inspection and administrative control without reconstructing a request."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .checkpoint import AtomicCheckpointStore
from .contracts import (
    DecisionAction,
    FactoryDecision,
    FactoryStatus,
    StrategyFactoryCheckpoint,
)
from .decision_log import DecisionLog
from .errors import CompatibilityError


class RunControl:
    def __init__(self, state_root: str | Path, run_id: str) -> None:
        self._run_id = run_id
        root = Path(state_root) / run_id
        self._store = AtomicCheckpointStore(root / "checkpoints")
        self._decisions = DecisionLog(root / "decisions.jsonl")

    def status(self) -> StrategyFactoryCheckpoint:
        return self._store.load(self._run_id)

    def cancel(self) -> StrategyFactoryCheckpoint:
        checkpoint = self.status()
        if checkpoint.status in {
            FactoryStatus.COMPLETED,
            FactoryStatus.CANCELED,
            FactoryStatus.DATASET_REJECTED,
            FactoryStatus.FAILED,
            FactoryStatus.CORRUPTED,
        }:
            return checkpoint
        return self._transition(
            checkpoint,
            status=FactoryStatus.CANCELED,
            action=DecisionAction.CANCEL,
            reason="USER_CANCELED",
        )

    def resume(self) -> StrategyFactoryCheckpoint:
        checkpoint = self.status()
        if checkpoint.status not in {FactoryStatus.PAUSED, FactoryStatus.BLOCKED}:
            raise CompatibilityError("only a paused or blocked run can be resumed")
        return self._transition(
            checkpoint,
            status=FactoryStatus.RUNNING,
            action=DecisionAction.RESUME,
            reason="RESUME_REQUESTED",
        )

    def _transition(
        self,
        checkpoint: StrategyFactoryCheckpoint,
        *,
        status: FactoryStatus,
        action: DecisionAction,
        reason: str,
    ) -> StrategyFactoryCheckpoint:
        if checkpoint.decision_chain_head != self._decisions.chain_head:
            raise CompatibilityError("checkpoint and decision log are inconsistent")
        sequence = len(self._decisions.read_all()) + 1
        decision = FactoryDecision(
            decision_id=f"decision-{self._run_id}-{sequence:06d}",
            run_id=self._run_id,
            stage=checkpoint.stage,
            action=action,
            subject_id=self._run_id,
            reason_codes=(reason,),
            evidence={},
            alternatives=(),
            budget_snapshot=dict(checkpoint.budget_state),
            decided_at=datetime.now(UTC),
        )
        entry = self._decisions.append(decision)
        updated = replace(
            checkpoint,
            status=status,
            decision_chain_head=entry.entry_hash,
            updated_at=datetime.now(UTC),
        )
        self._store.save(updated)
        return updated
