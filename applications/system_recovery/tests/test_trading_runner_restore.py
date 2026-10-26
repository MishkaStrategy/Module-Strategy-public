from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from modular_core import MarketView, ModuleDecision, ModuleManifest, ModuleRegistry, StrategyModule
from modular_trading_runner import (
    BoundedCriticalQueue,
    CoreMarketPipeline,
    LifecycleMutationRequest,
    LifecycleOperation,
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventValidator,
    RuntimeCheckpointCoordinator,
    RuntimeEventJournal,
    RuntimeState,
    TradingRunner,
    TradingRunnerConfig,
    TradingRunnerDurableStores,
    TradingRunnerOperatorLifecycle,
    TrustedOperatorIdentity,
)
from modular_trading_runner.errors import JournalIntegrityError

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "7" * 40
_RELEASE_ID = "trading-runner-clean-host-rc"
_RUN_ID = "run-1"
_RUNTIME_ID = "runner-1"
_MODULE_GRAPH_HASH = "b" * 64
_AT = datetime(2026, 8, 11, 7, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.value = _AT

    def now(self) -> datetime:
        return self.value


class _Planner:
    def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
        assert base_timeframe == "5m"
        assert isinstance(modules, tuple)
        return SimpleNamespace(modules=modules)


class _Recovery:
    def recover(self) -> bool:
        return True


class _Reconciliation:
    def reconcile(self) -> bool:
        return True


class _Health:
    def is_ready(self) -> bool:
        return True


class _MarketData:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _Execution:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def accept(self, envelope: object, **kwargs: object) -> object:
        return envelope

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        self.cancel_calls += 1
        return True


class _CheckpointCapture:
    def capture(self) -> None:
        return None


class _StatefulModule(StrategyModule):
    name = "recovery-stateful"
    manifest = ModuleManifest(name=name)

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"calls": self.calls}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.calls = int(state["calls"])


class _ViewFactory:
    def build(
        self,
        *,
        base_bars: tuple[object, ...],
        index: int,
        data_gap: bool,
        features: object,
    ) -> MarketView:
        raise AssertionError("clean-host recovery proof does not process market bars")


def _config() -> TradingRunnerConfig:
    return TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": _RUNTIME_ID,
            "portfolio_id": "portfolio-1",
            "strategy_id": "strategy-1",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "usdt_perpetual",
            "instruments": ("BTCUSDT",),
            "market_data_sources": ("binance-usdtm",),
            "modules": (),
        }
    )


def _policy() -> MarketDataPolicy:
    return MarketDataPolicy(
        market="usdt_perpetual",
        instruments=("BTCUSDT",),
        sources=("binance-usdtm",),
        timeframes=("5m",),
        max_event_age_seconds=30.0,
        max_future_skew_seconds=1.0,
        max_tracked_events=16,
    )


def _pipeline() -> CoreMarketPipeline:
    module = _StatefulModule()
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    return CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=_ViewFactory(),
        max_history_bars=16,
    )


def _checkpoint(
    *,
    stores: TradingRunnerDurableStores,
    journal: RuntimeEventJournal,
    clock: _Clock,
    config_hash: str,
) -> RuntimeCheckpointCoordinator:
    queue: BoundedCriticalQueue[MarketEventEnvelope] = BoundedCriticalQueue(4)
    return RuntimeCheckpointCoordinator(
        run_id=_RUN_ID,
        config_hash=config_hash,
        module_graph_hash=_MODULE_GRAPH_HASH,
        repository=stores.checkpoint_repository,
        clock=clock,
        journal=journal,
        market_validator=MarketEventValidator(_policy()),
        market_queue=queue,
        pipeline=_pipeline(),
    )


def _runner(
    *,
    stores: TradingRunnerDurableStores,
    journal: RuntimeEventJournal,
    clock: _Clock,
    execution: _Execution,
) -> TradingRunner:
    return TradingRunner(
        config=_config(),
        modules=(),
        planner=_Planner(),
        clock=clock,
        journal=journal,
        state_store=stores.runtime_state_store,
        recovery=_Recovery(),
        reconciliation=_Reconciliation(),
        health=_Health(),
        market_data=_MarketData(),
        plan_execution=execution,
        checkpoint=_CheckpointCapture(),
    )


def _snapshot_manifest(*, source: Path, backup: Path) -> RecoveryManifest:
    specs = (
        SnapshotSpec(
            logical_id="trading.checkpoint",
            owner="trading-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=f"trading/state/trading-runner-checkpoint:{_RUN_ID}.json",
            restore_target=f"trading/state/trading-runner-checkpoint:{_RUN_ID}.json",
        ),
        SnapshotSpec(
            logical_id="trading.journal",
            owner="trading-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="trading/journal/events.jsonl",
            restore_target="trading/journal/events.jsonl",
        ),
        SnapshotSpec(
            logical_id="trading.operator",
            owner="trading-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=f"trading/state/{_RUNTIME_ID}:operator-lifecycle:v1.json",
            restore_target=f"trading/state/{_RUNTIME_ID}:operator-lifecycle:v1.json",
        ),
        SnapshotSpec(
            logical_id="trading.runtime",
            owner="trading-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=f"trading/state/{_RUNTIME_ID}.json",
            restore_target=f"trading/state/{_RUNTIME_ID}.json",
        ),
    )
    entries = tuple(
        snapshot_entry(source_root=source, backup_root=backup, spec=spec) for spec in specs
    )
    return RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=entries,
    )


def _restore_entry(
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


async def _prepare_backup(
    tmp_path: Path,
) -> tuple[Path, RecoveryManifest, LifecycleMutationRequest]:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    source.mkdir()
    backup.mkdir()
    stores = TradingRunnerDurableStores.open(source / "trading")
    clock = _Clock()
    journal = RuntimeEventJournal(run_id=_RUN_ID, store=stores.event_store)
    execution = _Execution()
    runner = _runner(stores=stores, journal=journal, clock=clock, execution=execution)
    await runner.start()

    request = LifecycleMutationRequest(
        request_id="operator-kill-1",
        operator=TrustedOperatorIdentity("control-plane:local-operator"),
        operation=LifecycleOperation.ACTIVATE_KILL_SWITCH,
    )
    first = await TradingRunnerOperatorLifecycle(runner).execute(request)
    assert not first.replayed
    assert runner.state is RuntimeState.KILL_SWITCH_ACTIVE
    assert execution.cancel_calls == 1

    checkpoint = _checkpoint(
        stores=stores,
        journal=journal,
        clock=clock,
        config_hash=runner.config.canonical_hash,
    )
    checkpoint.capture()

    expected_files = (
        stores.state_directory / f"{_RUNTIME_ID}.json",
        stores.state_directory / f"{_RUNTIME_ID}:operator-lifecycle:v1.json",
        stores.state_directory / f"trading-runner-checkpoint:{_RUN_ID}.json",
        stores.journal_path,
    )
    assert all(path.is_file() for path in expected_files)
    return backup, _snapshot_manifest(source=source, backup=backup), request


@pytest.mark.asyncio
async def test_trading_runner_complete_durable_set_restores_into_clean_host(tmp_path: Path) -> None:
    backup, manifest, request = await _prepare_backup(tmp_path)
    clean = tmp_path / "clean"
    clean.mkdir()
    for entry in manifest.entries:
        _restore_entry(
            backup=backup,
            clean=clean,
            manifest=manifest,
            logical_id=entry.logical_id,
        )

    stores = TradingRunnerDurableStores.open(clean / "trading")
    clock = _Clock()
    journal = RuntimeEventJournal(run_id=_RUN_ID, store=stores.event_store)
    assert journal.verify_chain()

    checkpoint = _checkpoint(
        stores=stores,
        journal=journal,
        clock=clock,
        config_hash=_config().canonical_hash,
    )
    assert checkpoint.restore() is not None

    execution = _Execution()
    runner = _runner(stores=stores, journal=journal, clock=clock, execution=execution)
    assert runner.kill_switch.active
    assert runner.state is RuntimeState.CREATED

    replay = await TradingRunnerOperatorLifecycle(runner).execute(request)
    assert replay.replayed
    assert replay.new_state is RuntimeState.KILL_SWITCH_ACTIVE
    assert execution.cancel_calls == 0

    await runner.start()
    assert runner.state is RuntimeState.KILL_SWITCH_ACTIVE
    assert runner.kill_switch.active
    assert execution.cancel_calls == 1


@pytest.mark.asyncio
async def test_trading_checkpoint_rejects_missing_restored_journal_before_state_apply(
    tmp_path: Path,
) -> None:
    backup, manifest, _request = await _prepare_backup(tmp_path)
    clean = tmp_path / "clean-missing-journal"
    clean.mkdir()
    for logical_id in ("trading.checkpoint", "trading.operator", "trading.runtime"):
        _restore_entry(
            backup=backup,
            clean=clean,
            manifest=manifest,
            logical_id=logical_id,
        )

    stores = TradingRunnerDurableStores.open(clean / "trading")
    clock = _Clock()
    journal = RuntimeEventJournal(run_id=_RUN_ID, store=stores.event_store)
    checkpoint = _checkpoint(
        stores=stores,
        journal=journal,
        clock=clock,
        config_hash=_config().canonical_hash,
    )

    with pytest.raises(JournalIntegrityError, match="journal attestation"):
        checkpoint.restore()
