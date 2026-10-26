"""Typed Mac-local lifecycle coordinator for reconstructed Stage-1 owners.

This module is deliberately not a generic launcher.  A deployment-owned entrypoint
constructs the fixed typed loaders and source factory in code, then calls ``run``.
No class, module, command, identity, path, or secret is accepted from argv/browser.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Protocol

from modular_research_runner import PreparedExperiment, ResearchRunner
from modular_research_runner.owner_http import (
    ResearchRunnerOwnerHttpClient,
    ResearchRunnerOwnerHttpServer,
)
from modular_trading_runner import (
    RecoveredTradingRunnerOwner,
    TradingRunnerLifecycleHttpServer,
)
from modular_trading_runner.acceptance_http import TradingRunnerAcceptanceHttpQuery
from strategy_factory import StrategyPackage

from scripts.system_acceptance_stage1_deployment import (
    Stage1DeploymentSources,
    materialize_stage1_deployment,
)
from scripts.system_acceptance_stage1_host import Stage1MacServiceConfig, Stage1MacServiceHost
from scripts.system_acceptance_stage1_lifecycle import Stage1Lifecycle

_LOOPBACK = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class ReconstructedStage1Owners:
    strategy_package: StrategyPackage
    research_runner: ResearchRunner
    research_prepared: PreparedExperiment
    trading: RecoveredTradingRunnerOwner


class Stage1OwnersLoader(Protocol):
    def __call__(self) -> ReconstructedStage1Owners: ...


class Stage1SourcesFactory(Protocol):
    def __call__(
        self,
        strategy_package: StrategyPackage,
        research_runner: ResearchRunnerOwnerHttpClient,
        research_prepared: PreparedExperiment,
        trading_runner: TradingRunnerAcceptanceHttpQuery,
    ) -> Stage1DeploymentSources: ...


class Stage1ManagedOwnersLoader(Protocol):
    """Build owners and immediately register every acquisition on the lifecycle.

    Implementations must finish synchronously, unwind their own partial
    construction if a factory fails before returning, and must not re-enter the
    manager's ``start`` or ``close`` methods.
    """

    def __call__(self, lifecycle: Stage1Lifecycle) -> ReconstructedStage1Owners: ...


class Stage1ManagedSourcesFactory(Protocol):
    """Build sources and immediately register every acquisition on the lifecycle.

    Implementations must finish synchronously, unwind their own partial
    construction if a factory fails before returning, and must not re-enter the
    manager's ``start`` or ``close`` methods.
    """

    def __call__(
        self,
        lifecycle: Stage1Lifecycle,
        strategy_package: StrategyPackage,
        research_runner: ResearchRunnerOwnerHttpClient,
        research_prepared: PreparedExperiment,
        trading_runner: TradingRunnerAcceptanceHttpQuery,
    ) -> Stage1DeploymentSources: ...


@dataclass(frozen=True, slots=True)
class Stage1MacServiceManagerConfig:
    """Fixed private transport settings for one launchd-owned process."""

    acceptance: Stage1MacServiceConfig
    proof_path: Path = field(repr=False)
    trading_reader_token: str = field(repr=False)
    trading_operator_token: str = field(repr=False)
    research_reader_token: str = field(repr=False)
    research_operator_token: str = field(repr=False)
    trading_port: int = 8780
    research_port: int = 8782

    def __post_init__(self) -> None:
        if not isinstance(self.acceptance, Stage1MacServiceConfig):
            raise TypeError("acceptance must be Stage1MacServiceConfig")
        if self.acceptance.port != 8781:
            raise ValueError("System Acceptance must use loopback port 8781")
        if not isinstance(self.proof_path, Path) or not self.proof_path.is_absolute():
            raise ValueError("proof_path must be an absolute Path")
        if self.proof_path.is_symlink():
            raise ValueError("proof_path must not be a symlink")
        for value, name in (
            (self.trading_reader_token, "trading_reader_token"),
            (self.trading_operator_token, "trading_operator_token"),
            (self.research_reader_token, "research_reader_token"),
            (self.research_operator_token, "research_operator_token"),
        ):
            if not isinstance(value, str) or not 16 <= len(value) <= 4096:
                raise ValueError(f"{name} must contain 16..4096 characters")
        if self.trading_reader_token == self.trading_operator_token:
            raise ValueError("Trading reader/operator tokens must be distinct")
        if self.research_reader_token == self.research_operator_token:
            raise ValueError("Research reader/operator tokens must be distinct")
        if self.trading_port != 8780 or self.research_port != 8782:
            raise ValueError("Stage-1 owner transports must use ports 8780 and 8782")


class Stage1MacServiceManager:
    """Own one reconstructed owner graph and close it in reverse lifecycle order."""

    def __init__(
        self,
        *,
        config: Stage1MacServiceManagerConfig,
        load_owners: Stage1OwnersLoader | None = None,
        build_sources: Stage1SourcesFactory | None = None,
        managed_load_owners: Stage1ManagedOwnersLoader | None = None,
        managed_build_sources: Stage1ManagedSourcesFactory | None = None,
    ) -> None:
        if not isinstance(config, Stage1MacServiceManagerConfig):
            raise TypeError("config must be Stage1MacServiceManagerConfig")
        legacy_selected = load_owners is not None or build_sources is not None
        managed_selected = managed_load_owners is not None or managed_build_sources is not None
        if legacy_selected == managed_selected:
            raise TypeError("provide exactly one complete legacy or managed bootstrap pair")
        selected = (
            (load_owners, build_sources)
            if legacy_selected
            else (managed_load_owners, managed_build_sources)
        )
        if any(not callable(callback) for callback in selected):
            raise TypeError("owner loader and source factory must both be callable")
        self.config = config
        self._load_owners = load_owners
        self._build_sources = build_sources
        self._managed_load_owners = managed_load_owners
        self._managed_build_sources = managed_build_sources
        self._owners: ReconstructedStage1Owners | None = None
        self._trading_server: TradingRunnerLifecycleHttpServer | None = None
        self._research_server: ResearchRunnerOwnerHttpServer | None = None
        self._acceptance_host: Stage1MacServiceHost | None = None
        self._lifecycle = Stage1Lifecycle()
        self._condition = threading.Condition(threading.Lock())
        self._starting = False
        self._started = False
        self._closed = False
        self._drained = False

    def start(self) -> None:
        with self._condition:
            while self._starting:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("Stage-1 service manager is closed")
            if self._started:
                return
            self._starting = True
        owners: ReconstructedStage1Owners | None = None
        trading_server: TradingRunnerLifecycleHttpServer | None = None
        research_server: ResearchRunnerOwnerHttpServer | None = None
        host: Stage1MacServiceHost | None = None
        lifecycle = self._lifecycle
        try:
            if self._managed_load_owners is not None:
                owners = self._managed_load_owners(lifecycle)
            else:
                assert self._load_owners is not None
                owners = self._load_owners()
            if not isinstance(owners, ReconstructedStage1Owners):
                raise TypeError("owner loader must return ReconstructedStage1Owners")
            lifecycle.start(
                lambda: asyncio.run(owners.trading.runner.start()),
                lambda: asyncio.run(owners.trading.runner.stop()),
            )
            trading_server = lifecycle.acquire(
                lambda: TradingRunnerLifecycleHttpServer(
                    owners.trading.lifecycle,
                    reader_token=self.config.trading_reader_token,
                    operator_token=self.config.trading_operator_token,
                    host=_LOOPBACK,
                    port=self.config.trading_port,
                ),
                start=lambda server: server.start(),
                close=lambda server: server.close(),
            )
            research_server = lifecycle.acquire(
                lambda: ResearchRunnerOwnerHttpServer(
                    owners.research_runner,
                    experiment_id=owners.research_prepared.manifest.experiment_id,
                    reader_token=self.config.research_reader_token,
                    operator_token=self.config.research_operator_token,
                    host=_LOOPBACK,
                    port=self.config.research_port,
                ),
                start=lambda server: server.start(),
                close=lambda server: server.close(),
            )
            research_client = ResearchRunnerOwnerHttpClient(
                f"http://{_LOOPBACK}:{self.config.research_port}",
                experiment_id=owners.research_prepared.manifest.experiment_id,
                reader_token=self.config.research_reader_token,
                operator_token=self.config.research_operator_token,
            )
            trading_client = TradingRunnerAcceptanceHttpQuery(
                f"http://{_LOOPBACK}:{self.config.trading_port}",
                reader_token=self.config.trading_reader_token,
            )
            if self._managed_build_sources is not None:
                sources = self._managed_build_sources(
                    lifecycle,
                    owners.strategy_package,
                    research_client,
                    owners.research_prepared,
                    trading_client,
                )
            else:
                assert self._build_sources is not None
                sources = self._build_sources(
                    owners.strategy_package,
                    research_client,
                    owners.research_prepared,
                    trading_client,
                )
            composition = materialize_stage1_deployment(sources)
            host = Stage1MacServiceHost(
                composition=composition,
                config=self.config.acceptance,
            )
            _write_sanitized_proof(self.config.proof_path, host.prestart_proof.to_json())
            lifecycle.start(host.start, host.close)
        except BaseException as startup_error:
            primary_error: BaseException = startup_error
            try:
                lifecycle.close()
            except BaseException as cleanup_error:
                primary_error = BaseExceptionGroup(
                    "Stage-1 startup and rollback both failed", [primary_error, cleanup_error]
                )
            with self._condition:
                self._closed = True
                self._starting = False
                self._drained = True
                self._condition.notify_all()
            raise primary_error
        with self._condition:
            if self._closed:
                startup_failure: BaseException | None = RuntimeError(
                    "Stage-1 service manager closed during startup"
                )
            else:
                startup_failure = None
                self._owners = owners
                self._trading_server = trading_server
                self._research_server = research_server
                self._acceptance_host = host
                self._started = True
            if startup_failure is None:
                self._starting = False
                self._condition.notify_all()
        if startup_failure is not None:
            try:
                lifecycle.close()
            except BaseException as cleanup_error:
                startup_failure = BaseExceptionGroup(
                    "Stage-1 shutdown during startup and rollback both failed",
                    [startup_failure, cleanup_error],
                )
            with self._condition:
                self._starting = False
                self._drained = True
                self._condition.notify_all()
            raise startup_failure

    def run(self, stop_event: threading.Event | None = None) -> None:
        event = stop_event or threading.Event()
        if not isinstance(event, threading.Event):
            raise TypeError("stop_event must be threading.Event")
        restore: list[
            tuple[
                signal.Signals,
                signal.Handlers | Callable[[int, FrameType | None], object] | int | None,
            ]
        ] = []
        if threading.current_thread() is threading.main_thread():

            def request_stop(signum: int, frame: FrameType | None) -> None:
                del signum, frame
                event.set()

            for member in (signal.SIGTERM, signal.SIGINT):
                restore.append((member, signal.getsignal(member)))
                signal.signal(member, request_stop)
        errors: list[BaseException] = []
        try:
            self.start()
            lifecycle = self._lifecycle
            lifecycle.wait_for_stop(
                event,
                is_running=lambda: self.running,
                wait_seconds=self.config.acceptance.host_wait_seconds,
                failure_message="Stage-1 owner service stopped unexpectedly",
            )
        except BaseException as run_error:
            errors.append(run_error)
        try:
            self.close()
        except BaseException as cleanup_error:
            errors.append(cleanup_error)
        for member, handler in restore:
            try:
                signal.signal(member, handler)
            except BaseException as restore_error:
                errors.append(restore_error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Stage-1 run and shutdown failed", errors)

    def close(self) -> None:
        """Drain bootstrap and close owned resources; callbacks must not re-enter."""
        with self._condition:
            if self._closed:
                while not self._drained:
                    self._condition.wait()
                return
            self._closed = True
            while self._starting:
                self._condition.wait()
            lifecycle = self._lifecycle
            self._acceptance_host = None
            self._research_server = None
            self._trading_server = None
            self._owners = None
        try:
            lifecycle.close()
        finally:
            with self._condition:
                self._drained = True
                self._condition.notify_all()

    @property
    def running(self) -> bool:
        host = self._acceptance_host
        trading = self._trading_server
        research = self._research_server
        return bool(
            host is not None and host.running and trading is not None and research is not None
        )


def _write_sanitized_proof(path: Path, payload: str) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise ValueError("proof directory must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("proof path must not be a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "ReconstructedStage1Owners",
    "Stage1MacServiceManager",
    "Stage1MacServiceManagerConfig",
    "Stage1ManagedOwnersLoader",
    "Stage1ManagedSourcesFactory",
    "Stage1OwnersLoader",
    "Stage1SourcesFactory",
]
