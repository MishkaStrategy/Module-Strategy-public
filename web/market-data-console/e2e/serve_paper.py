from __future__ import annotations

import argparse
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import uvicorn

from modular_control_plane import ControlPlaneService, PaperRuntimeControlService
from modular_control_plane.server import ServerSettings, create_server
from modular_trading_runner import (
    TradingRunner,
    TradingRunnerConfig,
    TradingRunnerLifecycleHttpServer,
    TradingRunnerOperatorLifecycle,
)

NOW = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class Journal:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append(self, **kwargs: object) -> None:
        self.events.append(str(kwargs.get("event_type", "unknown")))


class Store:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def load(self, key: str) -> dict[str, object] | None:
        value = self.data.get(key)
        return value if isinstance(value, dict) else None

    def save(self, key: str, value: object) -> None:
        if not isinstance(value, dict):
            raise TypeError("runtime state value must be dict")
        self.data[key] = value


class Module:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def on_start(self) -> None:
        self.calls.append(f"start:{self.name}")

    def on_finish(self) -> None:
        self.calls.append(f"finish:{self.name}")


class Planner:
    def __init__(self, modules: tuple[Module, ...]) -> None:
        self.modules = modules

    def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
        if tuple(modules) != self.modules or base_timeframe != "5m":
            raise RuntimeError("unexpected E2E dependency plan input")
        return SimpleNamespace(modules=self.modules)


class Toggle:
    def __init__(self) -> None:
        self.calls = 0

    def recover(self) -> bool:
        self.calls += 1
        return True

    def reconcile(self) -> bool:
        self.calls += 1
        return True


class Health:
    def is_ready(self) -> bool:
        return True


class MarketData:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def start(self) -> None:
        self.calls.append("market:start")

    def stop(self) -> None:
        self.calls.append("market:stop")


class Executor:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def accept(self, envelope: object, **kwargs: object) -> object:
        return {"accepted": True, "plan_id": getattr(envelope, "plan_id", "e2e")}

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        self.cancel_calls += 1
        return True


class Checkpoint:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def capture(self) -> None:
        self.calls.append("checkpoint")


@dataclass(frozen=True)
class RuntimeSnapshot:
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
    queue_depth: int | None = 2
    queue_capacity: int | None = 4096
    queue_high_water_mark: int | None = 7
    private_reason: str = "BROWSER-PRIVATE-KILL-SWITCH-REASON"
    private_path: str = "/Users/private/paper-runtime.json"


@dataclass(frozen=True)
class ExecutionSnapshot:
    native_lifecycle: str = "ready"
    order_count: int = 1
    active_order_count: int = 1
    blocked_scope_count: int = 1
    private_recovery_payload: str = "BROWSER-PRIVATE-RECOVERY-PAYLOAD"


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str = "browser-paper-order"
    core_order_id: str = "browser-core-order"
    portfolio_id: str = "portfolio-1"
    instrument: str = "BTCUSDT"
    intent_role: str = "entry"
    native_state: str = "open"
    requested_quantity: Decimal = Decimal("0.010")
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0.010")
    average_fill_price: Decimal | None = None
    fees_paid: Decimal = Decimal("0")
    last_sequence: int = 0
    last_event_timestamp: datetime | None = None
    private_metadata: str = "BROWSER-PRIVATE-ORDER-METADATA"


@dataclass(frozen=True)
class ScopeSnapshot:
    portfolio_id: str = "portfolio-1"
    instrument: str = "BTCUSDT"


class RuntimeReader:
    def __init__(self, runner: TradingRunner) -> None:
        self.runner = runner

    def snapshot(self) -> RuntimeSnapshot:
        kill_switch = self.runner.kill_switch
        config = self.runner.config
        return RuntimeSnapshot(
            runtime_id=config.runtime_id,
            portfolio_id=config.portfolio_id,
            strategy_id=config.strategy_id,
            native_state=self.runner.state.value,
            execution_mode=config.execution_mode.value,
            base_timeframe=config.base_timeframe,
            market=config.market,
            instruments=tuple(config.instruments),
            configuration_hash=config.canonical_hash,
            kill_switch_active=kill_switch.active,
            kill_switch_scope=kill_switch.scope.value,
            kill_switch_activated_at=kill_switch.activated_at,
            kill_switch_cancellation_pending=kill_switch.cancellation_pending,
            health_ready=True,
        )


class ExecutionReader:
    def snapshot(self) -> ExecutionSnapshot:
        return ExecutionSnapshot()

    def list_orders(self, *, limit: int, offset: int) -> tuple[OrderSnapshot, ...]:
        first = OrderSnapshot()
        rows = (first,) + tuple(
            replace(
                first,
                client_order_id=f"browser-paper-order-{index:03}",
                core_order_id=f"browser-core-order-{index:03}",
            )
            for index in range(1, 101)
        )
        return rows[offset : offset + limit]

    def list_blocked_scopes(self) -> tuple[ScopeSnapshot, ...]:
        return (ScopeSnapshot(),)


def build_runner() -> TradingRunner:
    calls: list[str] = []
    modules = (Module("first", calls), Module("second", calls))
    config = TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "browser-paper-runtime",
            "portfolio_id": "portfolio-1",
            "strategy_id": "browser-accepted-strategy",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "usdt_perpetual",
            "instruments": ("BTCUSDT",),
            "market_data_sources": ("binance-usdtm",),
            "modules": (),
        }
    )
    return TradingRunner(
        config=config,
        modules=modules,
        planner=Planner(modules),
        clock=Clock(),
        journal=Journal(),
        state_store=Store(),
        recovery=Toggle(),
        reconciliation=Toggle(),
        health=Health(),
        market_data=MarketData(calls),
        plan_execution=Executor(),
        checkpoint=Checkpoint(calls),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reader-token", required=True)
    parser.add_argument("--operator-token", required=True)
    args = parser.parse_args()

    runner = build_runner()
    owner = TradingRunnerOperatorLifecycle(runner)
    downstream_reader = "runner-reader-" + secrets.token_hex(24)
    downstream_operator = "runner-operator-" + secrets.token_hex(24)
    owner_http = TradingRunnerLifecycleHttpServer(
        owner,
        reader_token=downstream_reader,
        operator_token=downstream_operator,
        port=0,
    )
    owner_http.start()
    owner_host, owner_port = owner_http.address

    control = PaperRuntimeControlService(
        RuntimeReader(runner), ExecutionReader(), clock=lambda: NOW
    )
    service = ControlPlaneService([control], clock=lambda: NOW)
    app = create_server(
        ServerSettings(
            reader_token=args.reader_token,
            operator_token=args.operator_token,
            trading_runner_lifecycle_url=f"http://{owner_host}:{owner_port}",
            trading_runner_lifecycle_reader_token=downstream_reader,
            trading_runner_lifecycle_operator_token=downstream_operator,
        ),
        service=service,
        paper_control=control,
        paper_positions_reader=lambda: {
            "schema_version": 1,
            "status": "available",
            "owner_id": "browser-paper-execution",
            "composition_id": "browser-composition-1",
            "portfolio_id": "portfolio-1",
            "owner_generation": "browser-generation-1",
            "membership_version": "browser-membership-3",
            "snapshot_version": "browser-snapshot-7",
            "state_version": "browser-state-11",
            "as_of": NOW.isoformat(),
            "coverage": "complete",
            "positions": [
                {
                    "owner_position_id": "owner-position-e2e",
                    "instrument_id": "venue:BTCUSDT",
                    "instrument_version": "instrument-v1",
                    "side": "long",
                    "quantity": "0.125000000000000001",
                    "quantity_unit": "contracts",
                    "strategy_id": None,
                },
                {
                    "owner_position_id": "owner-position-e2e-short",
                    "instrument_id": "venue:BTCUSDT",
                    "instrument_version": "instrument-v1",
                    "side": "short",
                    "quantity": "0.25",
                    "quantity_unit": "contracts",
                    "strategy_id": "browser-strategy-2",
                },
            ],
        },
    )
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        owner_http.close()


if __name__ == "__main__":
    main()
