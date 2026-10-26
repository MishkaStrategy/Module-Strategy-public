from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from strategy_factory.contracts import (
    DatasetDescriptor,
    FactoryMode,
    FactoryStage,
    FactoryStatus,
    MarketType,
    ResourceBudget,
    StrategyFactoryRequest,
    TimeRange,
)
from strategy_factory.errors import CheckpointError, IdempotencyConflictError
from strategy_factory.idempotency import IdempotencyLedger
from strategy_factory.run import StrategyFactoryRun

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "8" * 40
_RELEASE_ID = "strategy-factory-owner-restore"


def _request() -> StrategyFactoryRequest:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    descriptor = DatasetDescriptor(
        dataset_id="dataset-recovery-ethusdt-5m",
        source="recovery-fixture",
        symbol="ETHUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", "15m", "1h"),
        period=TimeRange(start, start + timedelta(days=30)),
        timezone="UTC",
        file_format="parquet",
        uri="registered://dataset-recovery-ethusdt-5m",
        sha256="a" * 64,
        row_count=8_640,
        received_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    budget = ResourceBudget(
        maximum_experiments=100,
        maximum_architectures=20,
        maximum_finalists=3,
        maximum_duration_seconds=3600,
        maximum_memory_bytes=1_000_000_000,
        maximum_cpu_workers=4,
        maximum_parallel_tasks=2,
        stage_experiment_limits={
            FactoryStage.STRUCTURAL_SCREENING: 30,
            FactoryStage.COARSE_SEARCH: 40,
            FactoryStage.LOCAL_REFINEMENT: 30,
        },
    )
    return StrategyFactoryRequest(
        request_id="request-recovery-create",
        mode=FactoryMode.CREATE,
        symbol="ETHUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        dataset=descriptor,
        allowed_timeframes=("5m", "15m"),
        initial_capital=Decimal("10000"),
        research_risk_fraction=Decimal("0.005"),
        maximum_drawdown_fraction=Decimal("0.15"),
        minimum_trade_count=30,
        objectives={"profit_factor": "maximize", "max_drawdown": "minimize"},
        budget=budget,
        random_seed=42,
        allowed_strategy_families=frozenset({"fvg", "breakout"}),
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _artifact_relative_path(run_id: str, artifact_id: str) -> str:
    artifact_type, digest = artifact_id.rsplit("-", 1)
    return f"{run_id}/artifacts/{artifact_type}/{digest}.json"


def _build_owner_state(source_root: Path) -> tuple[str, str | None, tuple[str, ...]]:
    run = StrategyFactoryRun(
        request=_request(),
        commit_sha="b" * 40,
        state_root=source_root,
        module_versions={"modular_core": "0.4.2"},
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    checkpoint = run.initialize()
    assert checkpoint.status is FactoryStatus.CREATED
    assert run.register_batch(batch_id="batch-recovery-001", batch_fingerprint="c" * 64)
    assert run.process_event("event-recovery-001")
    paused = run.pause(reason="RECOVERY_PROOF")
    assert paused.status is FactoryStatus.PAUSED
    assert paused.decision_chain_head is not None

    ledger = IdempotencyLedger(source_root / "idempotency.sqlite3")
    try:
        assert ledger.claim(
            scope="external-submit",
            operation_id="operation-recovery-001",
            content_hash="d" * 64,
        )
        ledger.bind_result(
            scope="external-submit",
            operation_id="operation-recovery-001",
            result_reference="experiment-recovery-001",
        )
    finally:
        ledger.close()

    request_artifact = paused.completed_artifacts["request"]
    manifest_artifact = paused.completed_artifacts["manifest"]
    paths = (
        f"{run.run_id}/checkpoints/{run.run_id}.json",
        f"{run.run_id}/decisions.jsonl",
        _artifact_relative_path(run.run_id, request_artifact),
        _artifact_relative_path(run.run_id, manifest_artifact),
        "idempotency.sqlite3",
    )
    return run.run_id, paused.decision_chain_head, paths


def _restore_owner_state(
    *,
    source_root: Path,
    backup_root: Path,
    clean_root: Path,
    paths: tuple[str, ...],
) -> RecoveryManifest:
    specs = (
        SnapshotSpec(
            logical_id="strategy-factory.checkpoint",
            owner="strategy-factory",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=paths[0],
            restore_target=paths[0],
        ),
        SnapshotSpec(
            logical_id="strategy-factory.decision-log",
            owner="strategy-factory",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=paths[1],
            restore_target=paths[1],
        ),
        SnapshotSpec(
            logical_id="strategy-factory.idempotency-ledger",
            owner="strategy-factory",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source=paths[4],
            restore_target=paths[4],
        ),
        SnapshotSpec(
            logical_id="strategy-factory.manifest-artifact",
            owner="strategy-factory",
            recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=paths[3],
            restore_target=paths[3],
        ),
        SnapshotSpec(
            logical_id="strategy-factory.request-artifact",
            owner="strategy-factory",
            recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=paths[2],
            restore_target=paths[2],
        ),
    )
    entries = tuple(
        sorted(
            (
                snapshot_entry(source_root=source_root, backup_root=backup_root, spec=spec)
                for spec in specs
            ),
            key=lambda entry: entry.logical_id,
        )
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=entries,
    )
    for entry in entries:
        restore_snapshot_entry(
            backup_root=backup_root,
            restore_root=clean_root,
            manifest=manifest,
            logical_id=entry.logical_id,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )
    return manifest


def test_strategy_factory_clean_host_restore_preserves_resume_and_replay_fences(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()

    run_id, chain_head, paths = _build_owner_state(source_root)
    _restore_owner_state(
        source_root=source_root,
        backup_root=backup_root,
        clean_root=clean_root,
        paths=paths,
    )

    restored = StrategyFactoryRun.restore_existing(state_root=clean_root, run_id=run_id)
    checkpoint = restored.restore()
    assert checkpoint.status is FactoryStatus.PAUSED
    assert checkpoint.decision_chain_head == chain_head
    assert checkpoint.submitted_batches == {"batch-recovery-001": "c" * 64}
    assert checkpoint.processed_event_ids == frozenset({"event-recovery-001"})

    assert not restored.register_batch(
        batch_id="batch-recovery-001",
        batch_fingerprint="c" * 64,
    )
    assert not restored.process_event("event-recovery-001")
    resumed = restored.resume()
    assert resumed.status is FactoryStatus.RUNNING
    assert resumed.decision_chain_head is not None
    assert resumed.decision_chain_head != chain_head

    cold_again = StrategyFactoryRun.restore_existing(state_root=clean_root, run_id=run_id)
    continued = cold_again.restore()
    assert continued.status is FactoryStatus.RUNNING
    assert continued.decision_chain_head == resumed.decision_chain_head

    ledger = IdempotencyLedger(clean_root / "idempotency.sqlite3")
    try:
        assert not ledger.claim(
            scope="external-submit",
            operation_id="operation-recovery-001",
            content_hash="d" * 64,
        )
        assert (
            ledger.result(
                scope="external-submit",
                operation_id="operation-recovery-001",
            )
            == "experiment-recovery-001"
        )
        with pytest.raises(IdempotencyConflictError):
            ledger.claim(
                scope="external-submit",
                operation_id="operation-recovery-001",
                content_hash="e" * 64,
            )
    finally:
        ledger.close()


def test_strategy_factory_tampered_decision_log_fails_closed(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()

    run_id, _, paths = _build_owner_state(source_root)
    _restore_owner_state(
        source_root=source_root,
        backup_root=backup_root,
        clean_root=clean_root,
        paths=paths,
    )
    decision_log = clean_root / paths[1]
    decision_log.write_text("{}\n", encoding="utf-8")

    restored = StrategyFactoryRun.restore_existing(state_root=clean_root, run_id=run_id)
    with pytest.raises(CheckpointError):
        restored.restore()
