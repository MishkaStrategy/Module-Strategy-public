from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .bootstrap import (
    TradingRunnerBootstrapComponents,
    TradingRunnerBootstrapFactory,
    TradingRunnerComponentsFactory,
)
from .checkpoint import RuntimeCheckpointCoordinator
from .config import TradingRunnerConfig
from .errors import BootstrapError
from .journal import RuntimeEventJournal
from .orchestration import RuntimeRecoveryCoordinator
from .persistence import TradingRunnerDurableStores
from .runtime import TradingRunner


@dataclass(frozen=True, slots=True)
class TradingRunnerProvisioningReceipt:
    """Sanitized identity of one successfully provisioned PAPER owner."""

    runtime_id: str
    portfolio_id: str
    strategy_id: str
    configuration_hash: str
    run_id: str
    execution_mode: str = "paper"


class TradingRunnerProvisioningFactory:
    """Create the first durable state set later consumed by restart bootstrap.

    The trusted deployment fixes every path, run identity, and concrete component
    factory in code. Provisioning is intentionally one-shot: existing or partial owner
    state is rejected instead of being merged with or overwritten.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        owner_root: Path,
        identity_path: Path,
        run_id: str,
        components_factory: TradingRunnerComponentsFactory,
    ) -> None:
        self._config_path = _absolute_path(config_path, "config_path")
        self._owner_root = _absolute_path(owner_root, "owner_root")
        self._identity_path = _absolute_path(identity_path, "identity_path")
        if self._identity_path.parent != self._owner_root:
            raise ValueError("identity_path must be inside the Trading Runner owner root")
        if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
            raise ValueError("run_id must be a non-blank trimmed string")
        if len(run_id) > 256:
            raise ValueError("run_id exceeds maximum length")
        if not callable(components_factory):
            raise TypeError("components_factory must be callable")
        self._run_id = run_id
        self._components_factory = components_factory

    async def provision(self) -> TradingRunnerProvisioningReceipt:
        """Provision one stopped durable PAPER owner and publish identity last."""

        config = self._load_config_before_mutation()
        self._require_unused_targets()

        first_runner: TradingRunner | None = None
        recovered_runner: TradingRunner | None = None
        temporary_identity: Path | None = None
        try:
            stores = TradingRunnerDurableStores.open(self._owner_root)
            journal = RuntimeEventJournal(run_id=self._run_id, store=stores.event_store)
            components, checkpoint = self._load_components(config, stores, journal)
            first_runner = _build_runner(config, stores, journal, components)

            await _run_owner_lifecycle(
                first_runner,
                failure_message="Trading Runner initial provisioning failed",
            )
            self._verify_durable_state(
                config=config,
                stores=stores,
                checkpoint=checkpoint,
                journal=journal,
            )

            temporary_identity = self._write_identity_candidate(config)
            recovered = TradingRunnerBootstrapFactory(
                config_path=self._config_path,
                owner_root=self._owner_root,
                identity_path=temporary_identity,
                components_factory=self._components_factory,
            ).load()
            recovered_runner = recovered.runner
            self._verify_restart_identity(recovered_runner, recovered.journal, config)

            # Exercise the real restart path before publishing the authoritative identity.
            # RuntimeRecoveryCoordinator must restore the checkpoint produced above.
            await _run_owner_lifecycle(
                recovered_runner,
                failure_message="Trading Runner restart verification failed",
            )
            recovered_checkpoint = self._require_real_owner_bindings(
                recovered_runner.checkpoint,
                recovered_runner.recovery,
                config=config,
                stores=recovered.stores,
                journal=recovered.journal,
            )
            self._verify_durable_state(
                config=config,
                stores=recovered.stores,
                checkpoint=recovered_checkpoint,
                journal=recovered.journal,
            )

            self._publish_identity(temporary_identity)
            temporary_identity = None
        except BootstrapError:
            await _best_effort_stop(recovered_runner)
            await _best_effort_stop(first_runner)
            raise
        except Exception as exc:
            await _best_effort_stop(recovered_runner)
            await _best_effort_stop(first_runner)
            raise BootstrapError("Trading Runner initial provisioning failed") from exc
        finally:
            if temporary_identity is not None:
                _best_effort_unlink(temporary_identity)

        return TradingRunnerProvisioningReceipt(
            runtime_id=config.runtime_id,
            portfolio_id=config.portfolio_id,
            strategy_id=config.strategy_id,
            configuration_hash=config.canonical_hash,
            run_id=self._run_id,
        )

    def _load_config_before_mutation(self) -> TradingRunnerConfig:
        try:
            config = TradingRunnerConfig.load_yaml(self._config_path)
        except Exception as exc:
            raise BootstrapError(
                "Trading Runner provisioning configuration is unavailable"
            ) from exc
        if config.execution_mode.value != "paper":
            raise BootstrapError("Trading Runner provisioning must be PAPER-only")
        return config

    def _require_unused_targets(self) -> None:
        if self._owner_root.is_symlink() or self._owner_root.exists():
            raise BootstrapError("Trading Runner owner root already exists")
        if self._identity_path.is_symlink() or self._identity_path.exists():
            raise BootstrapError("Trading Runner durable identity already exists")
        parent = self._owner_root.parent
        if parent.is_symlink() or not parent.is_dir():
            raise BootstrapError("Trading Runner owner parent is unavailable")

    def _load_components(
        self,
        config: TradingRunnerConfig,
        stores: TradingRunnerDurableStores,
        journal: RuntimeEventJournal,
    ) -> tuple[TradingRunnerBootstrapComponents, RuntimeCheckpointCoordinator]:
        components = self._components_factory(config, stores, journal)
        if not isinstance(components, TradingRunnerBootstrapComponents):
            raise BootstrapError("Trading Runner provisioning components are invalid")
        checkpoint = self._require_real_owner_bindings(
            components.checkpoint,
            components.recovery,
            config=config,
            stores=stores,
            journal=journal,
        )
        return components, checkpoint

    def _require_real_owner_bindings(
        self,
        checkpoint: object,
        recovery: object,
        *,
        config: TradingRunnerConfig,
        stores: TradingRunnerDurableStores,
        journal: RuntimeEventJournal,
    ) -> RuntimeCheckpointCoordinator:
        if not isinstance(checkpoint, RuntimeCheckpointCoordinator):
            raise BootstrapError(
                "Trading Runner provisioning requires RuntimeCheckpointCoordinator"
            )
        if checkpoint.run_id != self._run_id:
            raise BootstrapError("Trading Runner checkpoint run identity differs")
        if checkpoint.config_hash != config.canonical_hash:
            raise BootstrapError("Trading Runner checkpoint configuration differs")
        if checkpoint.repository is not stores.checkpoint_repository:
            raise BootstrapError("Trading Runner checkpoint repository differs from owner store")
        if checkpoint.journal is not journal:
            raise BootstrapError("Trading Runner checkpoint journal differs from owner journal")
        if not isinstance(recovery, RuntimeRecoveryCoordinator):
            raise BootstrapError("Trading Runner provisioning requires RuntimeRecoveryCoordinator")
        if recovery.checkpoint is not checkpoint or recovery.journal is not journal:
            raise BootstrapError(
                "Trading Runner recovery is not bound to the owner checkpoint/journal"
            )
        return checkpoint

    def _verify_durable_state(
        self,
        *,
        config: TradingRunnerConfig,
        stores: TradingRunnerDurableStores,
        checkpoint: RuntimeCheckpointCoordinator,
        journal: RuntimeEventJournal,
    ) -> None:
        state = stores.runtime_state_store.load(config.runtime_id)
        if not isinstance(state, Mapping):
            raise BootstrapError("Trading Runner provisioning runtime state is missing")
        if state.get("configuration_hash") != config.canonical_hash:
            raise BootstrapError("Trading Runner provisioning runtime state identity differs")
        if state.get("lifecycle_state") != "stopped":
            raise BootstrapError("Trading Runner provisioning did not stop durably")

        payload = stores.checkpoint_repository.load(checkpoint.state_key)
        if not isinstance(payload, Mapping):
            raise BootstrapError("Trading Runner provisioning checkpoint is missing")
        if payload.get("run_id") != self._run_id:
            raise BootstrapError("Trading Runner provisioning checkpoint run identity differs")
        if payload.get("config_hash") != config.canonical_hash:
            raise BootstrapError("Trading Runner provisioning checkpoint config identity differs")
        if not isinstance(payload.get("integrity_hash"), str):
            raise BootstrapError("Trading Runner provisioning checkpoint integrity is missing")
        checkpoint_journal = payload.get("journal")
        if not isinstance(checkpoint_journal, Mapping):
            raise BootstrapError("Trading Runner provisioning checkpoint journal is missing")
        if checkpoint_journal.get("run_id") != self._run_id:
            raise BootstrapError("Trading Runner provisioning checkpoint journal identity differs")
        if not journal.verify_chain():
            raise BootstrapError("Trading Runner provisioning journal chain is invalid")
        if journal.run_id != self._run_id:
            raise BootstrapError("Trading Runner provisioning journal identity differs")

    def _write_identity_candidate(self, config: TradingRunnerConfig) -> Path:
        payload = {
            "schema_version": 1,
            "runtime_id": config.runtime_id,
            "portfolio_id": config.portfolio_id,
            "strategy_id": config.strategy_id,
            "configuration_hash": config.canonical_hash,
            "journal_run_id": self._run_id,
            "checkpoint_run_id": self._run_id,
            "execution_mode": "paper",
        }
        rendered = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        )
        temporary = self._owner_root / (
            f".{self._identity_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            _best_effort_unlink(temporary)
            raise
        return temporary

    def _publish_identity(self, temporary: Path) -> None:
        if self._identity_path.is_symlink() or self._identity_path.exists():
            raise BootstrapError("Trading Runner durable identity appeared during provisioning")
        linked = False
        try:
            # The candidate is already fsync'ed and mode 0600. A hard link publishes
            # without overwrite; both names point to the same inode and permissions.
            os.link(temporary, self._identity_path)
            linked = True
            temporary.unlink()
            _fsync_directory(self._owner_root)
        except FileExistsError as exc:
            raise BootstrapError("Trading Runner durable identity already exists") from exc
        except OSError as exc:
            if linked:
                _best_effort_unlink(self._identity_path)
                _best_effort_fsync_directory(self._owner_root)
            raise BootstrapError("Trading Runner durable identity publication failed") from exc

    def _verify_restart_identity(
        self,
        runner: TradingRunner,
        journal: RuntimeEventJournal,
        config: TradingRunnerConfig,
    ) -> None:
        if (
            runner.config.runtime_id != config.runtime_id
            or runner.config.portfolio_id != config.portfolio_id
            or runner.config.strategy_id != config.strategy_id
            or runner.config.canonical_hash != config.canonical_hash
            or runner.config.execution_mode.value != "paper"
            or journal.run_id != self._run_id
        ):
            raise BootstrapError("Trading Runner restart-load identity differs after provisioning")


def _build_runner(
    config: TradingRunnerConfig,
    stores: TradingRunnerDurableStores,
    journal: RuntimeEventJournal,
    components: TradingRunnerBootstrapComponents,
) -> TradingRunner:
    return TradingRunner(
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


async def _run_owner_lifecycle(
    runner: TradingRunner,
    *,
    failure_message: str,
) -> None:
    try:
        await runner.start()
        await runner.stop()
    except Exception as exc:
        raise BootstrapError(failure_message) from exc


def _absolute_path(path: Path, name: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{name} must be Path")
    normalized = path.expanduser()
    if not normalized.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return normalized


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _best_effort_fsync_directory(path: Path) -> None:
    try:
        _fsync_directory(path)
    except OSError:
        pass


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _best_effort_stop(runner: TradingRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.stop()
    except Exception:
        pass


__all__ = [
    "TradingRunnerProvisioningFactory",
    "TradingRunnerProvisioningReceipt",
]
