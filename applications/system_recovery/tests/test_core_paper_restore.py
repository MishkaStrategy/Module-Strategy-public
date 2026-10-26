from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from modular_core import (
    AtomicJsonStateRepository,
    EngineConfig,
    ExecutionConfig,
    Journal,
    OrderType,
    Side,
    TimeInForce,
    capture_broker_state,
    restore_broker_state_from_repository,
    save_broker_state,
)
from modular_core.checkpoints import (
    CheckpointManager,
    KernelCheckpoint,
    build_checkpoint,
    capture_module_states,
    checkpoint_digest,
    restore_module_states,
)
from modular_core.events import JsonLinesEventStore
from modular_core.execution import BrokerSimulator
from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PlanAction,
    ReconciliationBlocked,
    StateVersions,
    VenueMetadataSnapshot,
)

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "6" * 40
_RELEASE_ID = "core-paper-recovery-rc"
_RUN_ID = "core-paper-clean-host"
_CHECKPOINT_KEY = "paper-runtime"
_BROKER_STATE_KEY = "paper-broker"
_CHECKPOINT_LOGICAL_ID = "core.paper.checkpoint"
_BROKER_LOGICAL_ID = "core.paper.broker-state"
_JOURNAL_LOGICAL_ID = "paper.execution.journal"
_NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
_VERSIONS = StateVersions(portfolio=3, positions=4, orders=5)


class _StaticMetadataProvider:
    def __init__(self, metadata: VenueMetadataSnapshot) -> None:
        self.metadata = metadata

    def snapshot(self, instrument: str, timestamp: datetime) -> VenueMetadataSnapshot:
        assert instrument == self.metadata.instrument
        return self.metadata


def _metadata() -> VenueMetadataSnapshot:
    return VenueMetadataSnapshot(
        venue_id="paper-core",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="binance-usdtm-2026-01-01",
        effective_from=_NOW - timedelta(days=1),
        source_hash="a" * 64,
    )


def _broker(metadata: VenueMetadataSnapshot) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )


def _plan(config: PaperExecutionConfig) -> ExecutableTradePlan:
    intent = ExecutionIntent(
        intent_id="entry-restore-1",
        role=IntentRole.ENTRY,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.0129"),
        limit_price=Decimal("99999.97"),
    )
    return ExecutableTradePlan.create(
        plan_id="plan-core-paper-restore",
        created_at=_NOW - timedelta(seconds=1),
        valid_from=_NOW - timedelta(seconds=1),
        expires_at=_NOW + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="binance_usdtm",
        action=PlanAction.OPEN_POSITION,
        intents=(intent,),
        expected_versions=_VERSIONS,
        metadata_version="binance-usdtm-2026-01-01",
        configuration_hash=config.canonical_hash,
        input_hashes={"decision": "b" * 64},
    )


def _coordinator(
    *,
    metadata: VenueMetadataSnapshot,
    broker: BrokerSimulator,
    journal_path: Path,
) -> PaperExecutionCoordinator:
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=_StaticMetadataProvider(metadata),
        run_id=_RUN_ID,
        event_store=JsonLinesEventStore(journal_path, fsync=True),
    )


def _prepare_backup(
    tmp_path: Path,
) -> tuple[
    Path,
    RecoveryManifest,
    KernelCheckpoint,
    ExecutableTradePlan,
    object,
    VenueMetadataSnapshot,
]:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    source.mkdir()
    backup.mkdir()

    metadata = _metadata()
    broker = _broker(metadata)
    journal_path = source / "paper" / "execution.jsonl"
    original = _coordinator(metadata=metadata, broker=broker, journal_path=journal_path)
    plan = _plan(original.config)
    first_result = original.accept_plan(
        plan,
        now=_NOW,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=_VERSIONS,
    )
    assert first_result.accepted
    assert journal_path.is_file()
    assert original.journal.store.read()
    assert len(broker.orders) == 1
    assert len(broker.pending_orders) == 1
    assert broker.orders[0].client_order_id is not None

    broker_repository = AtomicJsonStateRepository(source / "core" / "broker")
    broker_snapshot = save_broker_state(broker_repository, _BROKER_STATE_KEY, broker)
    assert capture_broker_state(broker).to_state() == broker_snapshot.to_state()
    broker_state_path = source / "core" / "broker" / f"{_BROKER_STATE_KEY}.json"
    assert broker_state_path.is_file()

    checkpoint = build_checkpoint(
        run_id=_RUN_ID,
        kernel_version="0.4.2",
        created_at=_NOW + timedelta(seconds=1),
        last_bar_index=7,
        last_bar_time=_NOW,
        data_hash="c" * 64,
        config_hash=original.config.canonical_hash,
        module_hash="d" * 64,
        modules=(original,),
    )
    repository = AtomicJsonStateRepository(source / "core" / "checkpoints")
    CheckpointManager(repository).save(_CHECKPOINT_KEY, checkpoint)
    checkpoint_path = source / "core" / "checkpoints" / f"{_CHECKPOINT_KEY}.json"
    assert checkpoint_path.is_file()

    checkpoint_entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id=_CHECKPOINT_LOGICAL_ID,
            owner="modular-core",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=f"core/checkpoints/{_CHECKPOINT_KEY}.json",
            restore_target=f"core/checkpoints/{_CHECKPOINT_KEY}.json",
        ),
    )
    broker_entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id=_BROKER_LOGICAL_ID,
            owner="modular-core",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=f"core/broker/{_BROKER_STATE_KEY}.json",
            restore_target=f"core/broker/{_BROKER_STATE_KEY}.json",
        ),
    )
    journal_entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id=_JOURNAL_LOGICAL_ID,
            owner="paper-execution",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="paper/execution.jsonl",
            restore_target="paper/execution.jsonl",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(broker_entry, checkpoint_entry, journal_entry),
    )
    return backup, manifest, checkpoint, plan, first_result, metadata


def _restore(
    *,
    backup: Path,
    clean: Path,
    manifest: RecoveryManifest,
    logical_id: str,
) -> Path:
    return restore_snapshot_entry(
        backup_root=backup,
        restore_root=clean,
        manifest=manifest,
        logical_id=logical_id,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )


def _load_checkpoint(clean: Path) -> KernelCheckpoint:
    loaded = CheckpointManager(AtomicJsonStateRepository(clean / "core" / "checkpoints")).load(
        _CHECKPOINT_KEY
    )
    assert loaded is not None
    assert loaded.integrity_sha256 is not None
    assert checkpoint_digest(loaded) == loaded.integrity_sha256
    return loaded


def _restore_broker(clean: Path, metadata: VenueMetadataSnapshot) -> BrokerSimulator:
    broker = _broker(metadata)
    restore_broker_state_from_repository(
        AtomicJsonStateRepository(clean / "core" / "broker"),
        _BROKER_STATE_KEY,
        broker,
    )
    return broker


def test_core_oms_checkpoint_and_paper_journal_restore_into_clean_host(
    tmp_path: Path,
) -> None:
    backup, manifest, checkpoint, plan, first_result, metadata = _prepare_backup(tmp_path)
    clean = tmp_path / "clean"
    clean.mkdir()
    for logical_id in (
        _CHECKPOINT_LOGICAL_ID,
        _BROKER_LOGICAL_ID,
        _JOURNAL_LOGICAL_ID,
    ):
        _restore(
            backup=backup,
            clean=clean,
            manifest=manifest,
            logical_id=logical_id,
        )

    loaded = _load_checkpoint(clean)
    assert loaded.checkpoint_id == checkpoint.checkpoint_id
    assert loaded.run_id == _RUN_ID
    assert loaded.config_hash == PaperExecutionConfig().canonical_hash
    assert len(loaded.module_states) == 1
    assert loaded.module_states[0].module == PaperExecutionCoordinator.name

    clean_broker = _restore_broker(clean, metadata)
    assert len(clean_broker.orders) == 1
    assert len(clean_broker.pending_orders) == 1
    restored_order = clean_broker.orders[0]
    assert restored_order.client_order_id is not None

    recovered = _coordinator(
        metadata=metadata,
        broker=clean_broker,
        journal_path=clean / "paper" / "execution.jsonl",
    )
    restore_module_states((recovered,), loaded.module_states)
    assert recovered.recovery_pending
    assert capture_module_states((recovered,))[0] == loaded.module_states[0]

    repeated = recovered.accept_plan(
        plan,
        now=_NOW,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=_VERSIONS,
    )
    assert repeated == first_result
    assert len(clean_broker.orders) == 1
    assert clean_broker.orders[0].id == restored_order.id

    order_report = recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert not order_report.findings
    assert not order_report.blocked
    assert recovered.recovery_pending

    position_findings = recovered.reconcile_position_state(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        expected=None,
        observed=clean_broker.portfolio.position_snapshot(100000.0),
    )
    assert not position_findings
    assert not recovered.recovery_pending
    assert (plan.portfolio_id, plan.instrument) not in recovered.blocked_scopes


def test_core_paper_restore_without_broker_state_remains_fail_closed(
    tmp_path: Path,
) -> None:
    backup, manifest, _checkpoint, plan, first_result, metadata = _prepare_backup(tmp_path)
    clean = tmp_path / "clean-missing-broker"
    clean.mkdir()
    for logical_id in (_CHECKPOINT_LOGICAL_ID, _JOURNAL_LOGICAL_ID):
        _restore(
            backup=backup,
            clean=clean,
            manifest=manifest,
            logical_id=logical_id,
        )

    loaded = _load_checkpoint(clean)
    clean_broker = _broker(metadata)
    recovered = _coordinator(
        metadata=metadata,
        broker=clean_broker,
        journal_path=clean / "paper" / "execution.jsonl",
    )
    restore_module_states((recovered,), loaded.module_states)
    assert recovered.recovery_pending

    repeated = recovered.accept_plan(
        plan,
        now=_NOW,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=_VERSIONS,
    )
    assert repeated == first_result
    assert len(clean_broker.orders) == 0

    report = recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert report.blocked
    assert report.findings
    assert recovered.recovery_pending

    unseen = ExecutableTradePlan.create(
        plan_id="plan-unseen-after-clean-host-restore",
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=plan.intents,
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
        input_hashes=plan.input_hashes,
    )
    with pytest.raises(ReconciliationBlocked, match="recovery reconciliation"):
        recovered.accept_plan(
            unseen,
            now=_NOW,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=_VERSIONS,
        )


def test_core_paper_restore_rejects_incomplete_journal_set_before_module_mutation(
    tmp_path: Path,
) -> None:
    backup, manifest, _checkpoint, _plan_value, _first_result, metadata = _prepare_backup(tmp_path)
    clean = tmp_path / "clean-missing-journal"
    clean.mkdir()
    for logical_id in (_CHECKPOINT_LOGICAL_ID, _BROKER_LOGICAL_ID):
        _restore(
            backup=backup,
            clean=clean,
            manifest=manifest,
            logical_id=logical_id,
        )
    loaded = _load_checkpoint(clean)

    recovered = _coordinator(
        metadata=metadata,
        broker=_restore_broker(clean, metadata),
        journal_path=clean / "paper" / "execution.jsonl",
    )
    before = recovered.snapshot_state()
    with pytest.raises(ValueError, match="journal attestation"):
        restore_module_states((recovered,), loaded.module_states)
    assert recovered.snapshot_state() == before
    assert not recovered.recovery_pending


def test_core_checkpoint_integrity_rejects_tamper_before_paper_restore(tmp_path: Path) -> None:
    backup, manifest, _checkpoint, _plan_value, _first_result, _metadata_value = _prepare_backup(
        tmp_path
    )
    clean = tmp_path / "clean-tampered-checkpoint"
    clean.mkdir()
    checkpoint_path = _restore(
        backup=backup,
        clean=clean,
        manifest=manifest,
        logical_id=_CHECKPOINT_LOGICAL_ID,
    )
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["integrity_sha256"] = "0" * 64
    checkpoint_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    manager = CheckpointManager(AtomicJsonStateRepository(clean / "core" / "checkpoints"))
    with pytest.raises(ValueError, match="checkpoint integrity verification failed"):
        manager.load(_CHECKPOINT_KEY)
