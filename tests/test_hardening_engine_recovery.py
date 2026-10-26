from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    DataConfig,
    EngineConfig,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    OrderRequest,
    OrderType,
    SetupEvent,
    SetupStatus,
    Side,
    StrategyModule,
)
from modular_core.audit import Journal
from modular_core.events import EventEnvelope, EventStore
from modular_core.extensions import ExecutionStateSnapshot, reconcile_execution_state
from modular_core.models import PositionSide, PositionSnapshot

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def bars(count: int) -> tuple[Candle, ...]:
    return tuple(
        Candle(NOW + timedelta(minutes=5 * index), 100, 101, 99, 100, 1, "X", "5m")
        for index in range(count)
    )


class FailingStore(EventStore):
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []
        self.fail = True

    def append(self, event: EventEnvelope) -> None:
        if self.fail:
            self.fail = False
            raise OSError("disk full")
        self.events.append(event)

    def read(self, *, after_sequence: int = 0):
        return tuple(event for event in self.events if event.sequence > after_sequence)


def test_journal_commits_sequence_only_after_event_store_append() -> None:
    store = FailingStore()
    journal = Journal(run_id="run", event_store=store)
    with pytest.raises(OSError, match="disk full"):
        journal.emit_event(timestamp=NOW, event_type="x", source="test", payload={})
    event = journal.emit_event(timestamp=NOW, event_type="x", source="test", payload={})
    assert event.sequence == 1
    assert event.event_id == "evt-0000000001"


class HistoryProbe(StrategyModule):
    name = "history_probe"

    def __init__(self) -> None:
        self.max_seen = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.max_seen = max(self.max_seen, len(view.base_bars))
        return ModuleDecision()


def test_base_history_can_be_bounded_without_changing_global_index() -> None:
    module = HistoryProbe()
    BacktestEngine(module, EngineConfig(data=DataConfig(max_base_history=3))).run(bars(10))
    assert module.max_seen == 3


class SetupProducer(StrategyModule):
    name = "analytics"
    manifest = ModuleManifest(name="analytics")

    def __init__(self) -> None:
        self.done = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self.done:
            return ModuleDecision()
        self.done = True
        return ModuleDecision(
            setups=(
                SetupEvent(
                    module=self.name,
                    setup_id="setup-1",
                    status=SetupStatus.CONFIRMED,
                    side=Side.BUY,
                ),
            )
        )


class RiskOrderProducer(StrategyModule):
    name = "risk"
    manifest = ModuleManifest(name="risk")

    def __init__(self) -> None:
        self.done = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self.done:
            return ModuleDecision()
        self.done = True
        return ModuleDecision(
            orders=(
                OrderRequest(
                    Side.BUY,
                    OrderType.MARKET,
                    quantity=1,
                    stop_loss=90,
                    take_profit=110,
                    setup_id="setup-1",
                ),
            )
        )


def test_order_module_can_advance_setup_owned_by_analytics_module() -> None:
    result = BacktestEngine((SetupProducer(), RiskOrderProducer())).run(bars(3))
    record = next(record for record in result.journal.setup_records if record.setup_id == "setup-1")
    assert record.module == "analytics"
    assert record.order_ids
    assert record.status in {SetupStatus.FILLED, SetupStatus.CLOSED}


def test_structured_reason_and_error_diagnostics(tmp_path: Path) -> None:
    journal = Journal(run_id="run")
    journal.log_skip(NOW, "module", "module_not_ready:5m:10")
    journal.log_error(NOW, "stage", ValueError("bad input"))
    assert journal.skips[-1]["reason_code"] == "module_not_ready"
    assert journal.skips[-1]["reason_detail"] == "5m:10"
    assert journal.errors[-1]["error_type"] == "ValueError"
    assert journal.errors[-1]["error_message"] == "bad input"
    output = journal.export_directory(tmp_path / "audit")
    assert (output / "environment.json").exists()
    assert (output / "SHA256SUMS.txt").exists()


def test_reconciliation_report_fails_closed_on_order_or_position_mismatch() -> None:
    local_position = PositionSnapshot(
        id="p",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        stop_loss=90,
        take_profit=110,
        tag="",
        unrealized_pnl=0,
    )
    local = ExecutionStateSnapshot(NOW, pending_order_ids=("a",), position=local_position)
    remote = ExecutionStateSnapshot(NOW, pending_order_ids=("b",), position=None)
    report = reconcile_execution_state(local, remote)
    assert not report.matched
    assert report.safe_halt_required
    assert report.missing_local_order_ids == ("b",)
    assert report.missing_remote_order_ids == ("a",)
    assert report.position_mismatch
