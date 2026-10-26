"""Typed fail-closed restart boundary for the authoritative PAPER owner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from modular_core.serialization import strict_json_loads

from .config import TradingRunnerConfig
from .errors import BootstrapError
from .journal import RuntimeEventJournal
from .operator_lifecycle import TradingRunnerOperatorLifecycle
from .persistence import TradingRunnerDurableStores
from .ports import (
    CheckpointCoordinator,
    DependencyPlanner,
    HealthManager,
    MarketDataRuntime,
    PlanExecutionBoundary,
    ReconciliationCoordinator,
    RecoveryCoordinator,
    RuntimeClock,
)
from .runtime import TradingRunner

_IDENTITY_FIELDS = frozenset(
    {
        "schema_version",
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "configuration_hash",
        "journal_run_id",
        "checkpoint_run_id",
        "execution_mode",
    }
)


@dataclass(frozen=True, slots=True)
class TradingRunnerBootstrapComponents:
    """Concrete, code-owned dependencies for one fixed Trading Runner profile."""

    modules: Sequence[Any]
    planner: DependencyPlanner
    clock: RuntimeClock
    recovery: RecoveryCoordinator
    reconciliation: ReconciliationCoordinator
    health: HealthManager
    market_data: MarketDataRuntime
    plan_execution: PlanExecutionBoundary
    checkpoint: CheckpointCoordinator


class TradingRunnerComponentsFactory(Protocol):
    def __call__(
        self,
        config: TradingRunnerConfig,
        stores: TradingRunnerDurableStores,
        journal: RuntimeEventJournal,
    ) -> TradingRunnerBootstrapComponents: ...


@dataclass(frozen=True, slots=True)
class RecoveredTradingRunnerOwner:
    """The single reconstructed runner and its existing operator lifecycle owner."""

    runner: TradingRunner
    lifecycle: TradingRunnerOperatorLifecycle
    stores: TradingRunnerDurableStores
    journal: RuntimeEventJournal


class TradingRunnerBootstrapFactory:
    """Reconstruct one configured authoritative owner from existing durable state.

    Paths and expected identities are fixed at construction by trusted deployment
    code.  Persisted data can validate a concrete component graph, but it never names
    Python classes or modules and is never executed.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        owner_root: Path,
        identity_path: Path,
        components_factory: TradingRunnerComponentsFactory,
    ) -> None:
        self._config_path = _absolute_path(config_path, "config_path")
        self._owner_root = _absolute_path(owner_root, "owner_root")
        self._identity_path = _absolute_path(identity_path, "identity_path")
        if not callable(components_factory):
            raise TypeError("components_factory must be callable")
        self._components_factory = components_factory

    def load(self) -> RecoveredTradingRunnerOwner:
        if not self._owner_root.is_dir() or self._owner_root.is_symlink():
            raise BootstrapError("Trading Runner durable owner root is unavailable")
        identity = _load_identity(self._identity_path)
        try:
            config = TradingRunnerConfig.load_yaml(self._config_path)
        except Exception as exc:
            raise BootstrapError("Trading Runner configured identity is unavailable") from exc
        expected = {
            "runtime_id": config.runtime_id,
            "portfolio_id": config.portfolio_id,
            "strategy_id": config.strategy_id,
            "configuration_hash": config.canonical_hash,
            "execution_mode": config.execution_mode.value,
        }
        if any(identity[name] != value for name, value in expected.items()):
            raise BootstrapError("Trading Runner durable identity does not match configuration")
        journal_run_id = identity["journal_run_id"]
        if not isinstance(journal_run_id, str) or not journal_run_id.strip():
            raise BootstrapError("Trading Runner journal identity is invalid")
        checkpoint_run_id = identity["checkpoint_run_id"]
        if not isinstance(checkpoint_run_id, str) or not checkpoint_run_id.strip():
            raise BootstrapError("Trading Runner checkpoint identity is invalid")
        if checkpoint_run_id != journal_run_id:
            raise BootstrapError("Trading Runner checkpoint and journal identities differ")
        state_directory = self._owner_root / "state"
        journal_path = self._owner_root / "journal" / "events.jsonl"
        if (
            not state_directory.is_dir()
            or state_directory.is_symlink()
            or not journal_path.is_file()
            or journal_path.is_symlink()
        ):
            raise BootstrapError("Trading Runner durable state set is incomplete")
        try:
            stores = TradingRunnerDurableStores.open(self._owner_root)
            persisted = stores.runtime_state_store.load(config.runtime_id)
            if persisted is None:
                raise BootstrapError("Trading Runner runtime state is missing")
            checkpoint = stores.checkpoint_repository.load(
                f"trading-runner-checkpoint:{checkpoint_run_id}"
            )
            if checkpoint is None:
                raise BootstrapError("Trading Runner checkpoint state is missing")
            journal = RuntimeEventJournal(run_id=journal_run_id, store=stores.event_store)
            components = self._components_factory(config, stores, journal)
            if not isinstance(components, TradingRunnerBootstrapComponents):
                raise BootstrapError("Trading Runner bootstrap components are invalid")
            runner = TradingRunner(
                config=config,
                modules=components.modules,
                planner=components.planner,
                clock=components.clock,
                journal=journal,
                state_store=stores.runtime_state_store,
                recovery=components.recovery,
                reconciliation=components.reconciliation,
                health=components.health,
                market_data=components.market_data,
                plan_execution=components.plan_execution,
                checkpoint=components.checkpoint,
            )
            lifecycle = TradingRunnerOperatorLifecycle(
                runner,
                state_store=stores.runtime_state_store,
            )
        except BootstrapError:
            raise
        except Exception as exc:
            raise BootstrapError("Trading Runner durable recovery failed") from exc
        return RecoveredTradingRunnerOwner(
            runner=runner,
            lifecycle=lifecycle,
            stores=stores,
            journal=journal,
        )


def _load_identity(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("Trading Runner durable identity manifest is unavailable")
    try:
        payload = path.read_bytes()
        if len(payload) > 64 * 1024:
            raise BootstrapError("Trading Runner durable identity manifest is oversized")
        value = strict_json_loads(payload)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise BootstrapError("Trading Runner durable identity manifest is invalid") from exc
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        raise BootstrapError("Trading Runner durable identity manifest fields are invalid")
    if value["schema_version"] != 1:
        raise BootstrapError("Trading Runner durable identity manifest schema is unsupported")
    if value["execution_mode"] != "paper":
        raise BootstrapError("Trading Runner durable identity must be PAPER-only")
    return value


def _absolute_path(path: Path, name: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{name} must be Path")
    normalized = path.expanduser()
    if not normalized.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return normalized


__all__ = [
    "RecoveredTradingRunnerOwner",
    "TradingRunnerBootstrapComponents",
    "TradingRunnerBootstrapFactory",
    "TradingRunnerComponentsFactory",
]
