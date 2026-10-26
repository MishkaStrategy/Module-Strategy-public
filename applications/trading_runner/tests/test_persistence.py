from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from modular_core import EventEnvelope

from modular_trading_runner import (
    RuntimeEventJournal,
    RuntimeState,
    TradingRunner,
    TradingRunnerConfig,
)
from modular_trading_runner.persistence import TradingRunnerDurableStores


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 11, 7, 0, tzinfo=UTC)


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
    def accept(self, envelope: object, **kwargs: object) -> object:
        return envelope

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        return True


class _Checkpoint:
    def capture(self) -> None:
        return None


class _FailSecondAudit:
    def __init__(self, delegate: RuntimeEventJournal) -> None:
        self.delegate = delegate
        self.calls = 0

    def append(self, **kwargs: Any) -> object:
        self.calls += 1
        if self.calls == 2:
            raise OSError("simulated audit storage failure")
        return self.delegate.append(**kwargs)


def _config() -> TradingRunnerConfig:
    return TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "runner-1",
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


def _event() -> EventEnvelope:
    return EventEnvelope(
        event_id="event-1",
        run_id="run-1",
        sequence=1,
        timestamp=datetime(2026, 8, 11, 7, 0, tzinfo=UTC),
        event_type="runtime.started",
        source="trading_runner",
        payload={"state": "running"},
        schema_version="1",
    )


def test_durable_stores_reuse_one_core_state_repository_and_reopen(tmp_path: Path) -> None:
    root = tmp_path / "trading-owner"
    stores = TradingRunnerDurableStores.open(root)

    assert stores.root == root.resolve()
    assert stores.runtime_state_store is stores.state_repository
    assert stores.checkpoint_repository is stores.state_repository
    assert stores.state_directory == root.resolve() / "state"
    assert stores.journal_path == root.resolve() / "journal" / "events.jsonl"

    stores.runtime_state_store.save(
        "runner-1",
        {"schema_version": "1", "state": "running"},
    )
    stores.checkpoint_repository.save(
        "trading-runner-checkpoint:run-1",
        {"schema_version": "1", "checkpoint": "captured"},
    )
    stores.event_store.append(_event())

    reopened = TradingRunnerDurableStores.open(root)
    assert reopened.runtime_state_store.load("runner-1") == {
        "schema_version": "1",
        "state": "running",
    }
    assert reopened.checkpoint_repository.load("trading-runner-checkpoint:run-1") == {
        "schema_version": "1",
        "checkpoint": "captured",
    }
    assert reopened.event_store.read() == (_event(),)


@pytest.mark.asyncio
async def test_successful_start_persists_runtime_state_before_any_plan_or_stop(
    tmp_path: Path,
) -> None:
    stores = TradingRunnerDurableStores.open(tmp_path / "trading-owner")
    journal = RuntimeEventJournal(run_id="run-1", store=stores.event_store)
    runner = TradingRunner(
        config=_config(),
        modules=(),
        planner=_Planner(),
        clock=_Clock(),
        journal=journal,
        state_store=stores.runtime_state_store,
        recovery=_Recovery(),
        reconciliation=_Reconciliation(),
        health=_Health(),
        market_data=_MarketData(),
        plan_execution=_Execution(),
        checkpoint=_Checkpoint(),
    )

    await runner.start()

    persisted = stores.runtime_state_store.load("runner-1")
    assert persisted is not None
    assert persisted["lifecycle_state"] == "running"
    assert persisted["configuration_hash"] == runner.config.canonical_hash
    assert persisted["processed_plan_hashes"] == {}
    assert (stores.state_directory / "runner-1.json").is_file()
    assert [event.event_type for event in journal.read()] == [
        "runtime.configuring",
        "runtime.started",
    ]


@pytest.mark.asyncio
async def test_started_audit_failure_compensates_durable_runtime_state_to_failed(
    tmp_path: Path,
) -> None:
    stores = TradingRunnerDurableStores.open(tmp_path / "trading-owner")
    durable_journal = RuntimeEventJournal(run_id="run-1", store=stores.event_store)
    journal = _FailSecondAudit(durable_journal)
    runner = TradingRunner(
        config=_config(),
        modules=(),
        planner=_Planner(),
        clock=_Clock(),
        journal=journal,
        state_store=stores.runtime_state_store,
        recovery=_Recovery(),
        reconciliation=_Reconciliation(),
        health=_Health(),
        market_data=_MarketData(),
        plan_execution=_Execution(),
        checkpoint=_Checkpoint(),
    )

    with pytest.raises(OSError, match="simulated audit storage failure"):
        await runner.start()

    persisted = stores.runtime_state_store.load("runner-1")
    assert persisted is not None
    assert persisted["lifecycle_state"] == RuntimeState.FAILED.value
    assert runner.state is RuntimeState.FAILED
    assert [event.event_type for event in durable_journal.read()] == [
        "runtime.configuring",
        "runtime.failed",
    ]


def test_durable_stores_accept_operator_lifecycle_colon_key(tmp_path: Path) -> None:
    stores = TradingRunnerDurableStores.open(tmp_path / "trading-owner")
    key = "runner-1:operator-lifecycle:v1"
    payload = {
        "schema_version": "1",
        "runtime_id": "runner-1",
        "config_hash": "a" * 64,
        "records": [],
    }

    stores.runtime_state_store.save(key, payload)

    reloaded = stores.runtime_state_store.load(key)
    assert reloaded is not None
    assert reloaded["schema_version"] == "1"
    assert reloaded["runtime_id"] == "runner-1"
    assert reloaded["config_hash"] == "a" * 64
    assert reloaded["records"] == ()
    assert (stores.state_directory / f"{key}.json").is_file()


def test_durable_stores_reject_symlinked_fixed_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "trading-owner"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="state directory must not be a symbolic link"):
        TradingRunnerDurableStores.open(root)

    assert not (outside / "runner-1.json").exists()


def test_durable_stores_reject_symlinked_journal_file(tmp_path: Path) -> None:
    root = tmp_path / "trading-owner"
    journal = root / "journal"
    outside = tmp_path / "outside-events.jsonl"
    journal.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    (journal / "events.jsonl").symlink_to(outside)

    with pytest.raises(ValueError, match="journal file must not be a symbolic link"):
        TradingRunnerDurableStores.open(root)
