from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from modular_core import (
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)

from modular_trading_runner.bootstrap import (
    RecoveredTradingRunnerOwner,
    TradingRunnerBootstrapComponents,
    TradingRunnerBootstrapFactory,
)
from modular_trading_runner.checkpoint import RuntimeCheckpointCoordinator
from modular_trading_runner.config import TradingRunnerConfig
from modular_trading_runner.errors import BootstrapError
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.market_data import (
    BoundedCriticalQueue,
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventValidator,
)
from modular_trading_runner.orchestration import RuntimeRecoveryCoordinator
from modular_trading_runner.persistence import TradingRunnerDurableStores
from modular_trading_runner.pipeline import CoreMarketPipeline
from modular_trading_runner.provisioning import TradingRunnerProvisioningFactory


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 25, 3, 55, tzinfo=UTC)


class _StatefulModule(StrategyModule):
    name = "provisioning-module"
    manifest = ModuleManifest(name=name, produced_features=("provisioning.value",))

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision(
            features=(
                Feature(
                    key="provisioning.value",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value=self.calls,
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                ),
            )
        )

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
        raise AssertionError("provisioning checkpoint must not fabricate market bars")


class _FactualRecovery:
    def recover(self) -> None:
        return None


class _Reconciliation:
    def reconcile(self) -> bool:
        return True


class _Health:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready


class _MarketData:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _Execution:
    def accept(self, envelope: object, **kwargs: object) -> object:
        return envelope

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        return True


def _mapping(*, execution_mode: str = "paper") -> dict[str, object]:
    return {
        "schema_version": "1",
        "runtime_id": "stage1-runtime",
        "portfolio_id": "stage1-portfolio",
        "strategy_id": "stage1-strategy",
        "execution_mode": execution_mode,
        "base_timeframe": "5m",
        "market": "usdt_perpetual",
        "instruments": ["BTCUSDT"],
        "market_data_sources": ["registered-market-data"],
        "modules": [],
    }


def _write_config(root: Path, *, execution_mode: str = "paper") -> Path:
    path = root / "trading.yaml"
    path.write_text(
        yaml.safe_dump(_mapping(execution_mode=execution_mode), sort_keys=True),
        encoding="utf-8",
    )
    return path


def _components(
    config: TradingRunnerConfig,
    stores: TradingRunnerDurableStores,
    journal: RuntimeEventJournal,
    *,
    ready: bool = True,
) -> TradingRunnerBootstrapComponents:
    clock = _Clock()
    module = _StatefulModule()
    registry = ModuleRegistry()
    dependency_plan = registry.validate_and_plan((module,), base_timeframe=config.base_timeframe)
    pipeline = CoreMarketPipeline(
        dependency_plan=dependency_plan,
        view_factory=_ViewFactory(),
        max_history_bars=16,
    )
    policy = MarketDataPolicy(
        market=config.market,
        instruments=config.instruments,
        sources=config.market_data_sources,
        timeframes=(config.base_timeframe,),
        max_event_age_seconds=30.0,
        max_future_skew_seconds=1.0,
        max_tracked_events=16,
    )
    checkpoint = RuntimeCheckpointCoordinator(
        run_id="stage1-owner",
        config_hash=config.canonical_hash,
        module_graph_hash="b" * 64,
        repository=stores.checkpoint_repository,
        clock=clock,
        journal=journal,
        market_validator=MarketEventValidator(policy),
        market_queue=BoundedCriticalQueue[MarketEventEnvelope](4),
        pipeline=pipeline,
    )
    recovery = RuntimeRecoveryCoordinator(
        checkpoint=checkpoint,
        factual_recovery=_FactualRecovery(),
        journal=journal,
        clock=clock,
        aggregate_id=config.runtime_id,
    )
    return TradingRunnerBootstrapComponents(
        modules=(module,),
        planner=registry,
        clock=clock,
        recovery=recovery,
        reconciliation=_Reconciliation(),
        health=_Health(ready=ready),
        market_data=_MarketData(),
        plan_execution=_Execution(),
        checkpoint=checkpoint,
    )


def _factory(
    config_path: Path,
    owner_root: Path,
    identity_path: Path,
    *,
    ready: bool = True,
) -> TradingRunnerProvisioningFactory:
    def components(
        config: TradingRunnerConfig,
        stores: TradingRunnerDurableStores,
        journal: RuntimeEventJournal,
    ) -> TradingRunnerBootstrapComponents:
        return _components(config, stores, journal, ready=ready)

    return TradingRunnerProvisioningFactory(
        config_path=config_path,
        owner_root=owner_root,
        identity_path=identity_path,
        run_id="stage1-owner",
        components_factory=components,
    )


def _bootstrap(
    config_path: Path,
    owner_root: Path,
    identity_path: Path,
) -> RecoveredTradingRunnerOwner:
    return TradingRunnerBootstrapFactory(
        config_path=config_path,
        owner_root=owner_root,
        identity_path=identity_path,
        components_factory=lambda config, durable, journal: _components(config, durable, journal),
    ).load()


def test_provisioning_creates_real_checkpoint_and_exercises_restart(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    owner_root = tmp_path / "owner"
    identity_path = owner_root / "identity.json"

    receipt = asyncio.run(_factory(config_path, owner_root, identity_path).provision())

    assert receipt.runtime_id == "stage1-runtime"
    assert receipt.portfolio_id == "stage1-portfolio"
    assert receipt.strategy_id == "stage1-strategy"
    assert receipt.run_id == "stage1-owner"
    assert receipt.execution_mode == "paper"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity == {
        "schema_version": 1,
        "runtime_id": receipt.runtime_id,
        "portfolio_id": receipt.portfolio_id,
        "strategy_id": receipt.strategy_id,
        "configuration_hash": receipt.configuration_hash,
        "journal_run_id": receipt.run_id,
        "checkpoint_run_id": receipt.run_id,
        "execution_mode": "paper",
    }
    assert identity_path.stat().st_mode & 0o777 == 0o600

    stores = TradingRunnerDurableStores.open(owner_root)
    checkpoint = stores.checkpoint_repository.load("trading-runner-checkpoint:stage1-owner")
    assert checkpoint is not None
    assert checkpoint["run_id"] == "stage1-owner"
    assert checkpoint["config_hash"] == receipt.configuration_hash
    assert isinstance(checkpoint["integrity_hash"], str)
    assert isinstance(checkpoint["journal"], Mapping)

    final_journal = RuntimeEventJournal(run_id="stage1-owner", store=stores.event_store)
    assert final_journal.verify_chain() is True
    assert len(final_journal.read("runtime.recovery_completed")) == 2
    assert len(final_journal.read("runtime.configuring")) == 2
    assert len(final_journal.read("runtime.stopped")) == 2

    recovered = _bootstrap(config_path, owner_root, identity_path)
    assert recovered.runner.config.runtime_id == receipt.runtime_id
    assert recovered.runner.config.portfolio_id == receipt.portfolio_id
    assert recovered.runner.config.strategy_id == receipt.strategy_id
    assert recovered.runner.config.canonical_hash == receipt.configuration_hash
    assert recovered.journal.run_id == receipt.run_id


def test_duplicate_provisioning_fails_without_overwrite(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    owner_root = tmp_path / "owner"
    identity_path = owner_root / "identity.json"
    factory = _factory(config_path, owner_root, identity_path)
    asyncio.run(factory.provision())
    before = identity_path.read_bytes()

    with pytest.raises(BootstrapError, match="owner root already exists"):
        asyncio.run(factory.provision())

    assert identity_path.read_bytes() == before


def test_failed_owner_start_never_publishes_identity(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    owner_root = tmp_path / "owner"
    identity_path = owner_root / "identity.json"

    with pytest.raises(BootstrapError, match="initial provisioning failed"):
        asyncio.run(
            _factory(
                config_path,
                owner_root,
                identity_path,
                ready=False,
            ).provision()
        )

    assert not identity_path.exists()


def test_non_paper_config_fails_before_owner_mutation(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, execution_mode="live")
    owner_root = tmp_path / "owner"
    identity_path = owner_root / "identity.json"

    with pytest.raises(BootstrapError, match="configuration is unavailable"):
        asyncio.run(_factory(config_path, owner_root, identity_path).provision())

    assert not owner_root.exists()
    assert not identity_path.exists()


def test_identity_publication_failure_leaves_no_authoritative_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    owner_root = tmp_path / "owner"
    identity_path = owner_root / "identity.json"

    def fail_link(source: object, destination: object) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(BootstrapError, match="publication failed"):
        asyncio.run(_factory(config_path, owner_root, identity_path).provision())

    assert not identity_path.exists()
