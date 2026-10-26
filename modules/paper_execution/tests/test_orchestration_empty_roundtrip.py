from __future__ import annotations

from paper_execution.orchestration_state import ExecutionOrchestrationState


def test_empty_orchestration_state_round_trip() -> None:
    source = ExecutionOrchestrationState()
    snapshot = source.snapshot_state()
    restored = ExecutionOrchestrationState.from_state(snapshot)
    assert restored.snapshot_state() == snapshot
