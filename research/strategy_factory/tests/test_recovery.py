from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from strategy_factory.budget import BudgetLedger
from strategy_factory.checkpoint import AtomicCheckpointStore
from strategy_factory.contracts import FactoryStage, FactoryStatus, StrategyFactoryCheckpoint
from strategy_factory.errors import BudgetExhaustedError, CheckpointError, IdempotencyConflictError
from strategy_factory.idempotency import IdempotencyLedger


def checkpoint(factory_request) -> StrategyFactoryCheckpoint:
    return StrategyFactoryCheckpoint(
        run_id="sf-run-recovery",
        request_fingerprint=factory_request.fingerprint,
        status=FactoryStatus.RUNNING,
        stage=FactoryStage.COARSE_SEARCH,
        stage_sequence=7,
        dataset_fingerprint=factory_request.dataset.fingerprint,
        commit_sha="b" * 40,
        manifest_id="manifest-recovery",
        completed_artifacts={"data_gate": "artifact-1"},
        submitted_batches={},
        processed_event_ids=frozenset(),
        budget_state={
            "experiments_reserved": 0,
            "architectures_generated": 0,
            "finalists_selected": 0,
            "stage_experiments": {},
            "elapsed_seconds": 0,
        },
        decision_chain_head=None,
        updated_at=datetime.now(UTC),
    )


def test_checkpoint_round_trip(factory_request, tmp_path: Path) -> None:
    store = AtomicCheckpointStore(tmp_path)
    original = checkpoint(factory_request)
    store.save(original)
    assert store.load(original.run_id) == original


def test_checkpoint_corruption_detected(factory_request, tmp_path: Path) -> None:
    store = AtomicCheckpointStore(tmp_path)
    original = checkpoint(factory_request)
    path = store.save(original)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace("RUNNING", "PAUSED"), encoding="utf-8")
    with pytest.raises(CheckpointError, match="hash mismatch"):
        store.load(original.run_id)


def test_idempotency_exact_replay_safe(tmp_path: Path) -> None:
    ledger = IdempotencyLedger(tmp_path / "state.sqlite3")
    try:
        assert ledger.claim(scope="batch", operation_id="one", content_hash="a" * 64)
        assert not ledger.claim(scope="batch", operation_id="one", content_hash="a" * 64)
    finally:
        ledger.close()


def test_idempotency_conflicting_content_rejected(tmp_path: Path) -> None:
    ledger = IdempotencyLedger(tmp_path / "state.sqlite3")
    try:
        ledger.claim(scope="batch", operation_id="one", content_hash="a" * 64)
        with pytest.raises(IdempotencyConflictError):
            ledger.claim(scope="batch", operation_id="one", content_hash="b" * 64)
    finally:
        ledger.close()


def test_budget_reservation_is_atomic(budget) -> None:
    ledger = BudgetLedger(budget)
    ledger.reserve_experiments(FactoryStage.STRUCTURAL_SCREENING, 30)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve_experiments(FactoryStage.STRUCTURAL_SCREENING, 1)
    assert ledger.experiments_reserved == 30


def test_budget_restore_rejects_inconsistent_total(budget) -> None:
    ledger = BudgetLedger(budget)
    with pytest.raises(Exception, match="does not match total"):
        ledger.restore(
            {
                "experiments_reserved": 2,
                "architectures_generated": 0,
                "finalists_selected": 0,
                "stage_experiments": {FactoryStage.COARSE_SEARCH.value: 1},
            }
        )
