from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from modular_core import InMemoryEventStore

from modular_trading_runner.errors import ReconciliationError, RecoveryError
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.orchestration import (
    PaperScopedReconciliationCoordinator,
    ReconciliationScope,
    RuntimeRecoveryCoordinator,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


class Checkpoint:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def restore(self) -> dict[str, object] | None:
        self.calls.append("checkpoint")
        if self.fail:
            raise RuntimeError("checkpoint failed")
        return {"restored": True}


class StartupCheckpoint:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def capture(self) -> object:
        self.calls.append("checkpoint-capture")
        if self.fail:
            raise RuntimeError("checkpoint capture failed")
        return {"captured": True}


class FactualRecovery:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def recover(self) -> object:
        self.calls.append("factual")
        return SimpleNamespace(event_id="factual-1")


@dataclass(frozen=True, slots=True)
class Report:
    blocked: bool
    findings: tuple[object, ...] = ()


class Paper:
    def __init__(
        self,
        reports: dict[tuple[str, str], Report],
        calls: list[str] | None = None,
    ) -> None:
        self.reports = reports
        self.calls: list[tuple[str, str, datetime | None]] = []
        self.call_order = calls

    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime | None = None,
    ) -> Report:
        self.calls.append((portfolio_id, instrument, timestamp))
        if self.call_order is not None:
            self.call_order.append(f"paper:{portfolio_id}:{instrument}")
        return self.reports[(portfolio_id, instrument)]


class InvalidPaper:
    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime | None = None,
    ) -> object:
        return object()


def journal() -> RuntimeEventJournal:
    return RuntimeEventJournal(run_id="run-1", store=InMemoryEventStore())


def test_recovery_chain_restores_checkpoint_before_factual_replay() -> None:
    calls: list[str] = []
    audit = journal()
    coordinator = RuntimeRecoveryCoordinator(
        checkpoint=Checkpoint(calls),
        factual_recovery=FactualRecovery(calls),
        journal=audit,
        clock=Clock(),
        aggregate_id="runner-1",
    )
    assert coordinator.recover() is True
    assert calls == ["checkpoint", "factual"]
    event = audit.read("runtime.recovery_completed")[0]
    assert event.payload["checkpoint_restored"] is True
    assert event.payload["factual_event_id"] == "factual-1"


def test_recovery_chain_fails_before_factual_replay_when_checkpoint_fails() -> None:
    calls: list[str] = []
    coordinator = RuntimeRecoveryCoordinator(
        checkpoint=Checkpoint(calls, fail=True),
        factual_recovery=FactualRecovery(calls),
        journal=journal(),
        clock=Clock(),
        aggregate_id="runner-1",
    )
    with pytest.raises(RecoveryError, match="recovery chain"):
        coordinator.recover()
    assert calls == ["checkpoint"]


def test_paper_reconciliation_is_deterministic_and_captures_fresh_checkpoint() -> None:
    first = ReconciliationScope("portfolio-b", "ETHUSDT")
    second = ReconciliationScope("portfolio-a", "BTCUSDT")
    call_order: list[str] = []
    paper = Paper(
        {
            (first.portfolio_id, first.instrument): Report(False, ("warning",)),
            (second.portfolio_id, second.instrument): Report(False),
        },
        call_order,
    )
    audit = journal()
    coordinator = PaperScopedReconciliationCoordinator(
        paper=paper,
        scopes=(first, second),
        journal=audit,
        clock=Clock(),
        checkpoint=StartupCheckpoint(call_order),
    )
    assert coordinator.reconcile() is True
    assert [(item[0], item[1]) for item in paper.calls] == [
        ("portfolio-a", "BTCUSDT"),
        ("portfolio-b", "ETHUSDT"),
    ]
    assert call_order == [
        "paper:portfolio-a:BTCUSDT",
        "paper:portfolio-b:ETHUSDT",
        "checkpoint-capture",
    ]
    assert len(audit.read("paper.reconciliation_completed")) == 2


def test_blocking_paper_report_stops_readiness_and_skips_checkpoint() -> None:
    blocked = ReconciliationScope("portfolio-a", "BTCUSDT")
    later = ReconciliationScope("portfolio-b", "ETHUSDT")
    call_order: list[str] = []
    paper = Paper(
        {
            (blocked.portfolio_id, blocked.instrument): Report(True, ("critical",)),
            (later.portfolio_id, later.instrument): Report(False),
        },
        call_order,
    )
    audit = journal()
    coordinator = PaperScopedReconciliationCoordinator(
        paper=paper,
        scopes=(later, blocked),
        journal=audit,
        clock=Clock(),
        checkpoint=StartupCheckpoint(call_order),
    )
    assert coordinator.reconcile() is False
    assert [(item[0], item[1]) for item in paper.calls] == [("portfolio-a", "BTCUSDT")]
    assert call_order == ["paper:portfolio-a:BTCUSDT"]
    assert len(audit.read("paper.reconciliation_blocked")) == 1


def test_startup_checkpoint_failure_blocks_readiness() -> None:
    scope = ReconciliationScope("portfolio-a", "BTCUSDT")
    coordinator = PaperScopedReconciliationCoordinator(
        paper=Paper({(scope.portfolio_id, scope.instrument): Report(False)}),
        scopes=(scope,),
        journal=journal(),
        clock=Clock(),
        checkpoint=StartupCheckpoint([], fail=True),
    )
    with pytest.raises(ReconciliationError, match="checkpoint capture"):
        coordinator.reconcile()


def test_invalid_paper_report_fails_closed() -> None:
    coordinator = PaperScopedReconciliationCoordinator(
        paper=InvalidPaper(),
        scopes=(ReconciliationScope("portfolio-a", "BTCUSDT"),),
        journal=journal(),
        clock=Clock(),
    )
    with pytest.raises(ReconciliationError, match="blocked flag"):
        coordinator.reconcile()
