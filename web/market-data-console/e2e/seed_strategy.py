from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from strategy_factory.checkpoint import AtomicCheckpointStore
from strategy_factory.contracts import FactoryStage, FactoryStatus, StrategyFactoryCheckpoint

RUN_ID = "browser-strategy-accepted"


def seed(state_root: Path) -> str:
    checkpoint_root = state_root / RUN_ID / "checkpoints"
    checkpoint = StrategyFactoryCheckpoint(
        run_id=RUN_ID,
        request_fingerprint="1" * 64,
        status=FactoryStatus.COMPLETED,
        stage=FactoryStage.PACKAGE,
        stage_sequence=14,
        dataset_fingerprint="2" * 64,
        commit_sha="3" * 40,
        manifest_id="browser-strategy-manifest",
        completed_artifacts={"package": "browser-strategy-package"},
        submitted_batches={},
        processed_event_ids=frozenset(),
        budget_state={},
        decision_chain_head=None,
        updated_at=datetime(2026, 8, 7, 4, 15, tzinfo=UTC),
    )
    target = AtomicCheckpointStore(checkpoint_root).save(checkpoint)
    if not target.is_file():
        raise RuntimeError("Strategy Factory checkpoint was not published")
    loaded = AtomicCheckpointStore(checkpoint_root).load(RUN_ID)
    if loaded != checkpoint:
        raise RuntimeError("Strategy Factory checkpoint round-trip mismatch")
    return RUN_ID


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()

    state_root = args.state_root.expanduser().resolve()
    run_id = seed(state_root)
    print(f"strategy_factory_run_id={run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
