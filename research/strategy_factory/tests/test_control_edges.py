from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from strategy_factory.checkpoint import AtomicCheckpointStore
from strategy_factory.contracts import (
    FactoryStage,
    FactoryStatus,
    StrategyFactoryCheckpoint,
)
from strategy_factory.control import RunControl
from strategy_factory.decision_log import DecisionLog
from strategy_factory.errors import CompatibilityError


def _checkpoint(
    *,
    run_id: str = "run-control",
    status: FactoryStatus = FactoryStatus.RUNNING,
    decision_chain_head: str | None = None,
) -> StrategyFactoryCheckpoint:
    return StrategyFactoryCheckpoint(
        run_id=run_id,
        request_fingerprint="a" * 64,
        status=status,
        stage=FactoryStage.COARSE_SEARCH,
        stage_sequence=5,
        dataset_fingerprint="b" * 64,
        commit_sha="c" * 40,
        manifest_id="manifest-control",
        completed_artifacts={"DATASET_GATE": "artifact-a"},
        submitted_batches={"COARSE_SEARCH": "batch-a"},
        processed_event_ids=frozenset({"event-a"}),
        budget_state={"experiments_used": 3},
        decision_chain_head=decision_chain_head,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _seed(root: Path, checkpoint: StrategyFactoryCheckpoint) -> RunControl:
    run_root = root / checkpoint.run_id
    AtomicCheckpointStore(run_root / "checkpoints").save(checkpoint)
    return RunControl(root, checkpoint.run_id)


def test_cancel_records_decision_and_is_idempotent(tmp_path: Path) -> None:
    control = _seed(tmp_path, _checkpoint())
    before = control.status()
    canceled = control.cancel()

    assert before.status is FactoryStatus.RUNNING
    assert canceled.status is FactoryStatus.CANCELED
    assert canceled.updated_at > before.updated_at
    assert canceled.decision_chain_head is not None

    entries = DecisionLog(tmp_path / "run-control" / "decisions.jsonl").read_all()
    assert len(entries) == 1
    assert entries[0].decision.reason_codes == ("USER_CANCELED",)
    assert entries[0].decision.action.value == "cancel"
    assert entries[0].decision.budget_snapshot == {"experiments_used": 3}

    repeated = control.cancel()
    assert repeated == canceled
    assert len(DecisionLog(tmp_path / "run-control" / "decisions.jsonl").read_all()) == 1


@pytest.mark.parametrize(
    "status",
    [
        FactoryStatus.COMPLETED,
        FactoryStatus.CANCELED,
        FactoryStatus.DATASET_REJECTED,
        FactoryStatus.FAILED,
        FactoryStatus.CORRUPTED,
    ],
)
def test_cancel_is_noop_for_terminal_statuses(tmp_path: Path, status: FactoryStatus) -> None:
    checkpoint = _checkpoint(run_id=f"run-{status.value.lower()}", status=status)
    control = _seed(tmp_path, checkpoint)
    assert control.cancel() == checkpoint
    assert not (tmp_path / checkpoint.run_id / "decisions.jsonl").exists()


@pytest.mark.parametrize("status", [FactoryStatus.PAUSED, FactoryStatus.BLOCKED])
def test_resume_records_decision_for_resumable_statuses(
    tmp_path: Path, status: FactoryStatus
) -> None:
    checkpoint = _checkpoint(run_id=f"run-{status.value.lower()}", status=status)
    control = _seed(tmp_path, checkpoint)
    resumed = control.resume()

    assert resumed.status is FactoryStatus.RUNNING
    assert resumed.stage is checkpoint.stage
    entries = DecisionLog(tmp_path / checkpoint.run_id / "decisions.jsonl").read_all()
    assert len(entries) == 1
    assert entries[0].decision.reason_codes == ("RESUME_REQUESTED",)
    assert entries[0].decision.action.value == "resume"


@pytest.mark.parametrize(
    "status",
    [FactoryStatus.RUNNING, FactoryStatus.COMPLETED, FactoryStatus.CANCELED],
)
def test_resume_rejects_non_resumable_statuses(tmp_path: Path, status: FactoryStatus) -> None:
    checkpoint = _checkpoint(run_id=f"run-{status.value.lower()}", status=status)
    control = _seed(tmp_path, checkpoint)
    with pytest.raises(CompatibilityError, match="paused or blocked"):
        control.resume()


def test_transition_rejects_checkpoint_decision_chain_mismatch(tmp_path: Path) -> None:
    checkpoint = _checkpoint(
        status=FactoryStatus.PAUSED,
        decision_chain_head="d" * 64,
    )
    control = _seed(tmp_path, checkpoint)
    with pytest.raises(CompatibilityError, match="inconsistent"):
        control.resume()


def test_sequential_transitions_advance_decision_identity(tmp_path: Path) -> None:
    control = _seed(tmp_path, _checkpoint(status=FactoryStatus.BLOCKED))
    resumed = control.resume()
    AtomicCheckpointStore(tmp_path / "run-control" / "checkpoints").save(
        replace(resumed, status=FactoryStatus.PAUSED)
    )
    resumed_again = control.resume()

    entries = DecisionLog(tmp_path / "run-control" / "decisions.jsonl").read_all()
    assert [entry.sequence for entry in entries] == [1, 2]
    assert entries[0].decision.decision_id.endswith("000001")
    assert entries[1].decision.decision_id.endswith("000002")
    assert resumed_again.decision_chain_head == entries[-1].entry_hash
