from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from paper_execution import (
    PaperExecutionCoordinator,
    RouterExecutionAdapter,
)
from paper_execution.coordinator import _PreparedIntent
from paper_execution.enums import PaperOrderState
from paper_execution.fastpaths import (
    _CachingRouterPlanAdapter,
    _PendingSnapshotElidingVenue,
    _ScopedOrderView,
)
from paper_execution.router_adapter import RouterPlanAdapter, RouterPlanEvidence
from paper_execution.router_execution import (
    RouterExecutionAdapter as LegacyRouterExecutionAdapter,
)
from paper_execution.service import (
    PaperExecutionCoordinator as LegacyPaperExecutionCoordinator,
)
from paper_execution.state_machine import OrderAggregate

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _aggregate(
    client_id: str,
    *,
    portfolio_id: str = "portfolio-main",
    instrument: str = "BTCUSDT",
    state: PaperOrderState = PaperOrderState.OPEN,
) -> OrderAggregate:
    return OrderAggregate(
        client_order_id=client_id,
        core_order_id=f"core-{client_id}",
        requested_quantity=Decimal("1"),
        state=state,
        portfolio_id=portfolio_id,
        instrument=instrument,
        intent_role="entry",
    )


def test_public_fastpaths_preserve_legacy_substitutability() -> None:
    assert issubclass(PaperExecutionCoordinator, LegacyPaperExecutionCoordinator)
    assert issubclass(RouterExecutionAdapter, LegacyRouterExecutionAdapter)


def test_router_plan_adapter_reuses_exact_inspection(monkeypatch: Any) -> None:
    calls = 0
    evidence = cast(RouterPlanEvidence, object())
    mapping = {"portfolio_id": "portfolio-main"}

    def fake_inspect(
        self: RouterPlanAdapter,
        envelope: object,
        *,
        now: datetime,
    ) -> tuple[RouterPlanEvidence, dict[str, str]]:
        del self, envelope, now
        nonlocal calls
        calls += 1
        return evidence, mapping

    monkeypatch.setattr(RouterPlanAdapter, "inspect", fake_inspect)
    adapter = _CachingRouterPlanAdapter()
    envelope = object()
    first = adapter.inspect(envelope, now=NOW)
    second = adapter.inspect(envelope, now=NOW)
    assert first == second
    assert calls == 1

    adapter.inspect(envelope, now=NOW.replace(microsecond=1))
    assert calls == 2


def test_scoped_order_view_caches_only_active_scope_items() -> None:
    active = _aggregate("active")
    terminal = _aggregate("terminal", state=PaperOrderState.FILLED)
    other = _aggregate("other", portfolio_id="portfolio-other")
    orders = {
        active.client_order_id: active,
        terminal.client_order_id: terminal,
        other.client_order_id: other,
    }

    view = _ScopedOrderView(orders, ("portfolio-main", "BTCUSDT"))
    assert view.items() == (("active", active),)
    assert "terminal" in view
    assert view.get("terminal") is terminal
    assert view.get("other") is other


class _Venue:
    def __init__(self) -> None:
        self.pending_calls = 0
        self.marker = "delegated"

    def pending_snapshots(self) -> tuple[object, ...]:
        self.pending_calls += 1
        return (object(),)


class _Plan:
    intents: tuple[object, ...] = ()


def test_pending_snapshot_proxy_elides_only_snapshot_materialization() -> None:
    venue = _Venue()
    proxy = _PendingSnapshotElidingVenue(cast(Any, venue))
    assert proxy.pending_snapshots() == ()
    assert venue.pending_calls == 0
    assert proxy.marker == "delegated"


def test_coordinator_fastpath_is_scoped_and_restores_original_state(
    monkeypatch: Any,
) -> None:
    active = _aggregate("active")
    terminal = _aggregate("terminal", state=PaperOrderState.CANCELED)
    other = _aggregate("other", portfolio_id="portfolio-other")
    orders = {
        active.client_order_id: active,
        terminal.client_order_id: terminal,
        other.client_order_id: other,
    }
    venue = _Venue()
    coordinator = object.__new__(PaperExecutionCoordinator)
    coordinator.orders = orders
    coordinator.venue = cast(Any, venue)
    observed: dict[str, object] = {}

    def fake_prepare(
        self: LegacyPaperExecutionCoordinator,
        **kwargs: object,
    ) -> tuple[_PreparedIntent, ...]:
        del kwargs
        observed["items"] = tuple(self.orders.items())
        observed["pending"] = self.venue.pending_snapshots()
        return ()

    monkeypatch.setattr(LegacyPaperExecutionCoordinator, "_prepare_plan", fake_prepare)
    result = coordinator._prepare_plan(
        plan=cast(Any, _Plan()),
        metadata=cast(Any, object()),
        now=NOW,
        mark_price=Decimal("100"),
        scope=("portfolio-main", "BTCUSDT"),
    )

    assert result == ()
    assert observed["items"] == (("active", active),)
    assert observed["pending"] == ()
    assert venue.pending_calls == 0
    assert coordinator.orders is orders
    assert coordinator.venue is venue
