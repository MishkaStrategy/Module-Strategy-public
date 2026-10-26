from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .runtime import TradingRunner
from .service import TradingRuntimeService


def _utc(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    runtime_id: str
    portfolio_id: str
    strategy_id: str
    native_state: str
    execution_mode: str
    base_timeframe: str
    market: str
    instruments: tuple[str, ...]
    configuration_hash: str
    kill_switch_active: bool
    kill_switch_scope: str
    kill_switch_activated_at: datetime | None
    kill_switch_cancellation_pending: bool
    health_ready: bool
    queue_depth: int | None
    queue_capacity: int | None
    queue_high_water_mark: int | None


class ReadOnlyTradingRuntimeQuery:
    """Side-effect-free projection over one already-owned live TradingRunner.

    This observer never creates, starts, stops, recovers or mutates the runner. It
    intentionally omits free-form kill-switch reasons and component health details.
    """

    def __init__(
        self,
        runner: TradingRunner,
        *,
        runtime_service: TradingRuntimeService | None = None,
    ) -> None:
        if not isinstance(runner, TradingRunner):
            raise TypeError("runner must be TradingRunner")
        if runtime_service is not None:
            if not isinstance(runtime_service, TradingRuntimeService):
                raise TypeError("runtime_service must be TradingRuntimeService or None")
            if runtime_service.runner is not runner:
                raise ValueError("runtime_service must own the supplied runner")
        self._runner = runner
        self._runtime_service = runtime_service

    def snapshot(self) -> RuntimeControlSnapshot:
        runner = self._runner
        config = runner.config
        kill_switch = runner.kill_switch
        execution_mode = config.execution_mode.value
        if execution_mode != "paper":
            raise ValueError("Trading Runner control projection accepts paper mode only")

        queue_depth: int | None = None
        queue_capacity: int | None = None
        queue_high_water_mark: int | None = None
        if self._runtime_service is not None:
            queue = self._runtime_service.queue
            queue_depth = queue.depth
            queue_capacity = queue.capacity
            queue_high_water_mark = queue.high_water_mark

        return RuntimeControlSnapshot(
            runtime_id=config.runtime_id,
            portfolio_id=config.portfolio_id,
            strategy_id=config.strategy_id,
            native_state=runner.state.value,
            execution_mode=execution_mode,
            base_timeframe=config.base_timeframe,
            market=config.market,
            instruments=tuple(config.instruments),
            configuration_hash=config.canonical_hash,
            kill_switch_active=kill_switch.active,
            kill_switch_scope=kill_switch.scope.value,
            kill_switch_activated_at=_utc(
                kill_switch.activated_at,
                "kill_switch.activated_at",
            ),
            kill_switch_cancellation_pending=kill_switch.cancellation_pending,
            health_ready=bool(runner.health.is_ready()),
            queue_depth=queue_depth,
            queue_capacity=queue_capacity,
            queue_high_water_mark=queue_high_water_mark,
        )
