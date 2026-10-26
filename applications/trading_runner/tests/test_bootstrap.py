from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from modular_trading_runner.bootstrap import (
    TradingRunnerBootstrapComponents,
    TradingRunnerBootstrapFactory,
)
from modular_trading_runner.config import TradingRunnerConfig
from modular_trading_runner.errors import BootstrapError
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.persistence import TradingRunnerDurableStores


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


class _Planner:
    def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
        assert base_timeframe == "5m"
        return SimpleNamespace(modules=tuple(modules))


class _Toggle:
    def recover(self) -> bool:
        return True

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


def _mapping() -> dict[str, object]:
    return {
        "schema_version": "1",
        "runtime_id": "stage1-runtime",
        "portfolio_id": "stage1-portfolio",
        "strategy_id": "stage1-strategy",
        "execution_mode": "paper",
        "base_timeframe": "5m",
        "market": "usdt_perpetual",
        "instruments": ["BTCUSDT"],
        "market_data_sources": ["registered-market-data"],
        "modules": [],
    }


def _durable_owner(root: Path) -> tuple[Path, Path, Path, TradingRunnerConfig]:
    config = TradingRunnerConfig.from_mapping(_mapping())
    config_path = root / "trading.yaml"
    config_path.write_text(yaml.safe_dump(_mapping(), sort_keys=True), encoding="utf-8")
    owner_root = root / "owner"
    stores = TradingRunnerDurableStores.open(owner_root)
    stores.runtime_state_store.save(
        config.runtime_id,
        {
            "schema_version": "1",
            "configuration_hash": config.canonical_hash,
            "processed_plan_hashes": {},
            "kill_switch": {
                "active": False,
                "reason": "",
                "scope": "global",
                "activated_at": None,
                "cancellation_pending": False,
            },
            "lifecycle_state": "running",
        },
    )
    stores.checkpoint_repository.save(
        "trading-runner-checkpoint:stage1-owner",
        {"schema_version": "1", "checkpoint": "durable"},
    )
    journal = RuntimeEventJournal(run_id="stage1-owner", store=stores.event_store)
    journal.append(
        timestamp=_Clock().now(),
        event_type="runtime.started",
        aggregate_id=config.runtime_id,
        payload={"state": "running"},
    )
    identity_path = root / "trading-identity.json"
    identity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": config.runtime_id,
                "portfolio_id": config.portfolio_id,
                "strategy_id": config.strategy_id,
                "configuration_hash": config.canonical_hash,
                "journal_run_id": "stage1-owner",
                "checkpoint_run_id": "stage1-owner",
                "execution_mode": "paper",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config_path, owner_root, identity_path, config


def _components(*args: object) -> TradingRunnerBootstrapComponents:
    return TradingRunnerBootstrapComponents(
        modules=(),
        planner=_Planner(),
        clock=_Clock(),
        recovery=_Toggle(),
        reconciliation=_Toggle(),
        health=_Health(),
        market_data=_MarketData(),
        plan_execution=_Execution(),
        checkpoint=_Checkpoint(),
    )


def test_recovers_single_authoritative_owner_with_same_identity(tmp_path: Path) -> None:
    config_path, owner_root, identity_path, config = _durable_owner(tmp_path)

    recovered = TradingRunnerBootstrapFactory(
        config_path=config_path,
        owner_root=owner_root,
        identity_path=identity_path,
        components_factory=_components,
    ).load()

    assert recovered.lifecycle.runner is recovered.runner
    assert recovered.runner.config.runtime_id == config.runtime_id
    assert recovered.runner.config.portfolio_id == config.portfolio_id
    assert recovered.runner.config.strategy_id == config.strategy_id
    assert recovered.runner.config.canonical_hash == config.canonical_hash
    assert recovered.journal.run_id == "stage1-owner"


def test_missing_runtime_state_fails_closed(tmp_path: Path) -> None:
    config_path, owner_root, identity_path, config = _durable_owner(tmp_path)
    (owner_root / "state" / f"{config.runtime_id}.json").unlink()

    with pytest.raises(BootstrapError, match="runtime state is missing"):
        TradingRunnerBootstrapFactory(
            config_path=config_path,
            owner_root=owner_root,
            identity_path=identity_path,
            components_factory=_components,
        ).load()


def test_missing_checkpoint_state_fails_closed(tmp_path: Path) -> None:
    config_path, owner_root, identity_path, _ = _durable_owner(tmp_path)
    (owner_root / "state" / "trading-runner-checkpoint:stage1-owner.json").unlink()

    with pytest.raises(BootstrapError, match="checkpoint state is missing"):
        TradingRunnerBootstrapFactory(
            config_path=config_path,
            owner_root=owner_root,
            identity_path=identity_path,
            components_factory=_components,
        ).load()


@pytest.mark.parametrize(
    "field", ["runtime_id", "portfolio_id", "strategy_id", "configuration_hash"]
)
def test_trading_identity_mismatch_fails_closed(tmp_path: Path, field: str) -> None:
    config_path, owner_root, identity_path, _ = _durable_owner(tmp_path)
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity[field] = "f" * 64 if field == "configuration_hash" else "different-owner"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    with pytest.raises(BootstrapError, match="does not match configuration"):
        TradingRunnerBootstrapFactory(
            config_path=config_path,
            owner_root=owner_root,
            identity_path=identity_path,
            components_factory=_components,
        ).load()


def test_live_configuration_fails_closed(tmp_path: Path) -> None:
    config_path, owner_root, identity_path, _ = _durable_owner(tmp_path)
    mapping = _mapping()
    mapping["execution_mode"] = "live"
    config_path.write_text(yaml.safe_dump(mapping), encoding="utf-8")

    with pytest.raises(BootstrapError, match="configured identity is unavailable"):
        TradingRunnerBootstrapFactory(
            config_path=config_path,
            owner_root=owner_root,
            identity_path=identity_path,
            components_factory=_components,
        ).load()


def test_corrupt_journal_fails_closed_without_leaking_payload(tmp_path: Path) -> None:
    config_path, owner_root, identity_path, _ = _durable_owner(tmp_path)
    secret = "private-token-should-never-escape"
    (owner_root / "journal" / "events.jsonl").write_text(secret, encoding="utf-8")

    with pytest.raises(BootstrapError, match="durable recovery failed") as captured:
        TradingRunnerBootstrapFactory(
            config_path=config_path,
            owner_root=owner_root,
            identity_path=identity_path,
            components_factory=_components,
        ).load()

    assert secret not in str(captured.value)
