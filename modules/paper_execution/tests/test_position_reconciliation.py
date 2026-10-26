from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from modular_core import PositionSide, PositionSnapshot

from paper_execution.enums import PaperOrderState
from paper_execution.position_reconciliation import ExpectedPositionState, reconcile_position
from paper_execution.state_machine import OrderAggregate


def _position(
    *,
    side: PositionSide = PositionSide.LONG,
    quantity: float = 1.0,
    stop_loss: float | None = 90.0,
    take_profit: float | None = 120.0,
) -> PositionSnapshot:
    return PositionSnapshot(
        id="pos-1",
        side=side,
        quantity=quantity,
        entry_price=100.0,
        entry_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        stop_loss=stop_loss,
        take_profit=take_profit,
        tag="test",
        unrealized_pnl=0.0,
    )


def _order(
    role: str,
    quantity: str,
    *,
    state: PaperOrderState = PaperOrderState.OPEN,
) -> OrderAggregate:
    return OrderAggregate(
        client_order_id=f"pevr-{role}-{quantity}",
        core_order_id=f"core-{role}-{quantity}",
        requested_quantity=Decimal(quantity),
        state=state,
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
        intent_role=role,
    )


def test_exact_position_level_protection_match() -> None:
    findings = reconcile_position(
        expected=ExpectedPositionState(
            PositionSide.LONG,
            Decimal("1"),
            require_target=True,
        ),
        observed=_position(),
        orders={},
    )
    assert findings == ()


def test_exact_order_level_protection_match() -> None:
    findings = reconcile_position(
        expected=ExpectedPositionState(
            PositionSide.LONG,
            Decimal("1"),
            require_target=True,
        ),
        observed=_position(stop_loss=None, take_profit=None),
        orders={"stop": _order("stop", "1"), "target": _order("target", "1")},
    )
    assert findings == ()


def test_position_side_and_quantity_mismatch_are_blocking() -> None:
    findings = reconcile_position(
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=_position(side=PositionSide.SHORT, quantity=2.0),
        orders={},
    )
    assert {finding.category for finding in findings} >= {
        "position_side_mismatch",
        "position_quantity_mismatch",
    }
    assert all(finding.blocking for finding in findings)
    assert all(not finding.automatic_resolution_eligible for finding in findings)


def test_missing_underallocated_and_overclosing_stop_are_detected() -> None:
    missing = reconcile_position(
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=_position(stop_loss=None, take_profit=None),
        orders={},
    )
    assert [finding.category for finding in missing] == ["position_unprotected"]

    underprotected = reconcile_position(
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=_position(stop_loss=None, take_profit=None),
        orders={"stop": _order("stop", "0.5")},
    )
    assert [finding.category for finding in underprotected] == ["position_underprotected"]

    excessive = reconcile_position(
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=_position(stop_loss=None, take_profit=None),
        orders={"stop": _order("stop", "1.5")},
    )
    assert [finding.category for finding in excessive] == ["protection_overclose_risk"]


def test_duplicate_position_and_order_stop_paths_are_blocking() -> None:
    findings = reconcile_position(
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=_position(stop_loss=90.0, take_profit=None),
        orders={"stop": _order("stop", "1")},
    )
    assert [finding.category for finding in findings] == ["duplicate_stop_exit_paths"]


def test_target_missing_underallocated_overclosing_and_duplicate_are_detected() -> None:
    expected = ExpectedPositionState(
        PositionSide.LONG,
        Decimal("1"),
        require_stop=False,
        require_target=True,
    )
    missing = reconcile_position(
        expected=expected,
        observed=_position(stop_loss=None, take_profit=None),
        orders={},
    )
    assert [finding.category for finding in missing] == ["target_missing"]

    underallocated = reconcile_position(
        expected=expected,
        observed=_position(stop_loss=None, take_profit=None),
        orders={"target": _order("target", "0.5")},
    )
    assert [finding.category for finding in underallocated] == ["target_underallocated"]

    excessive = reconcile_position(
        expected=expected,
        observed=_position(stop_loss=None, take_profit=None),
        orders={"target": _order("target", "1.5")},
    )
    assert [finding.category for finding in excessive] == ["target_overclose_risk"]

    duplicate = reconcile_position(
        expected=expected,
        observed=_position(stop_loss=None, take_profit=120.0),
        orders={"target": _order("target", "1")},
    )
    assert [finding.category for finding in duplicate] == ["duplicate_target_exit_paths"]


def test_orphan_and_missing_position_findings() -> None:
    orphan = reconcile_position(expected=None, observed=_position(), orders={})
    assert [finding.category for finding in orphan] == ["orphan_position"]

    missing = reconcile_position(
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=None,
        orders={},
    )
    assert [finding.category for finding in missing] == ["position_missing"]
