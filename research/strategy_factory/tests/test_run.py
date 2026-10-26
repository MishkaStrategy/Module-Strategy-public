from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from strategy_factory.contracts import FactoryStage, FactoryStatus
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.errors import CompatibilityError
from strategy_factory.run import StrategyFactoryRun


def make_run(factory_request, tmp_path: Path, commit: str = "a" * 40) -> StrategyFactoryRun:
    return StrategyFactoryRun(
        request=factory_request,
        commit_sha=commit,
        state_root=tmp_path,
        module_versions={"fvg": "1.0.0"},
    )


def analyzer() -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal("0"),
        )
    )


def test_run_initialization_and_data_gate(factory_request, tmp_path: Path) -> None:
    run = make_run(factory_request, tmp_path)
    initial = run.initialize()
    assert initial.status is FactoryStatus.CREATED
    analysis = run.run_data_gate(analyzer())
    assert analysis.profile is not None
    assert run.checkpoint.stage is FactoryStage.REGIME_DISCOVERY
    assert "data_gate" in run.checkpoint.completed_artifacts
    restored = make_run(factory_request, tmp_path)
    restored.initialize()
    assert restored.restore() == run.checkpoint


def test_duplicate_batch_and_event_are_safe(factory_request, tmp_path: Path) -> None:
    run = make_run(factory_request, tmp_path)
    run.initialize()
    assert run.register_batch(batch_id="batch-1", batch_fingerprint="b" * 64)
    assert not run.register_batch(batch_id="batch-1", batch_fingerprint="b" * 64)
    with pytest.raises(CompatibilityError):
        run.register_batch(batch_id="batch-1", batch_fingerprint="c" * 64)
    assert run.process_event("event-1")
    assert not run.process_event("event-1")


def test_pause_resume_cancel(factory_request, tmp_path: Path) -> None:
    run = make_run(factory_request, tmp_path)
    run.initialize()
    assert run.pause(reason="TEST_PAUSE").status is FactoryStatus.PAUSED
    assert run.resume().status is FactoryStatus.RUNNING
    assert run.cancel().status is FactoryStatus.CANCELED
    assert run.cancel().status is FactoryStatus.CANCELED


def test_restore_rejects_commit_change(factory_request, tmp_path: Path) -> None:
    first = make_run(factory_request, tmp_path, "a" * 40)
    first.initialize()
    second = make_run(factory_request, tmp_path, "b" * 40)
    with pytest.raises(CompatibilityError, match="commit SHA"):
        second.initialize()


def test_restore_existing_reconstructs_request_and_manifest(
    factory_request, tmp_path: Path
) -> None:
    original = make_run(factory_request, tmp_path)
    original.initialize()
    original.run_data_gate(analyzer())

    restored = StrategyFactoryRun.restore_existing(
        state_root=tmp_path,
        run_id=original.run_id,
    )

    assert restored.request == factory_request
    assert restored.manifest == original.manifest
    assert restored.checkpoint == original.checkpoint


def test_restore_existing_fails_when_request_artifact_is_missing(
    factory_request, tmp_path: Path
) -> None:
    run = make_run(factory_request, tmp_path)
    checkpoint = run.initialize()
    from dataclasses import replace

    from strategy_factory.checkpoint import AtomicCheckpointStore

    root = tmp_path / run.run_id
    damaged = replace(
        checkpoint,
        completed_artifacts={
            key: value for key, value in checkpoint.completed_artifacts.items() if key != "request"
        },
    )
    AtomicCheckpointStore(root / "checkpoints").save(damaged)

    with pytest.raises(CompatibilityError, match="predates recoverable"):
        StrategyFactoryRun.restore_existing(state_root=tmp_path, run_id=run.run_id)
