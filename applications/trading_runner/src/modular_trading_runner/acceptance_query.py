from __future__ import annotations

from dataclasses import dataclass

from .runtime import TradingRunner
from .supervision import RuntimeHealthRegistry


@dataclass(frozen=True, slots=True)
class TradingRuntimeAcceptanceSnapshot:
    """Bounded PAPER runtime identity and health required by System Acceptance."""

    runtime_id: str
    portfolio_id: str
    strategy_id: str
    execution_mode: str
    base_timeframe: str
    configuration_hash: str
    native_state: str
    ready: bool
    live: bool
    component_count: int
    critical_failure_count: int


class ReadOnlyTradingRuntimeAcceptanceQuery:
    """Side-effect-free projection over one already-owned live TradingRunner.

    The query never creates, starts, stops, recovers, reconciles, or mutates the
    runtime. It deliberately omits component names/details, free-form errors, paths,
    credentials, queue payloads, and every operator mutation surface.
    """

    def __init__(self, runner: TradingRunner) -> None:
        if not isinstance(runner, TradingRunner):
            raise TypeError("runner must be TradingRunner")
        if runner.config.execution_mode.value != "paper":
            raise ValueError("Stage-1 runtime projection accepts PAPER mode only")
        if not isinstance(runner.health, RuntimeHealthRegistry):
            raise TypeError("runner must expose authoritative RuntimeHealthRegistry health")
        self._runner = runner

    def snapshot(self) -> TradingRuntimeAcceptanceSnapshot:
        runner = self._runner
        config = runner.config
        if config.execution_mode.value != "paper":
            raise ValueError("Stage-1 runtime projection accepts PAPER mode only")
        health = runner.health
        if not isinstance(health, RuntimeHealthRegistry):
            raise TypeError("runner must expose authoritative RuntimeHealthRegistry health")
        health_snapshot = health.snapshot()
        return TradingRuntimeAcceptanceSnapshot(
            runtime_id=config.runtime_id,
            portfolio_id=config.portfolio_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode.value,
            base_timeframe=config.base_timeframe,
            configuration_hash=config.canonical_hash,
            native_state=runner.state.value,
            ready=health_snapshot.ready,
            live=health_snapshot.live,
            component_count=len(health_snapshot.components),
            critical_failure_count=len(health_snapshot.critical_failures),
        )


__all__ = [
    "ReadOnlyTradingRuntimeAcceptanceQuery",
    "TradingRuntimeAcceptanceSnapshot",
]
