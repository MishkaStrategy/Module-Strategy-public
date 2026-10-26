from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .fastpaths import PaperExecutionCoordinator
from .state_machine import OrderAggregate


def _utc(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PaperExecutionControlSnapshot:
    native_lifecycle: str
    order_count: int
    active_order_count: int
    blocked_scope_count: int


@dataclass(frozen=True, slots=True)
class PaperOrderControlSnapshot:
    client_order_id: str
    core_order_id: str
    portfolio_id: str
    instrument: str
    intent_role: str
    native_state: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal | None
    fees_paid: Decimal
    last_sequence: int
    last_event_timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class PaperBlockedScopeSnapshot:
    portfolio_id: str
    instrument: str


class ReadOnlyPaperExecutionQuery:
    """Read-only projection over an already-owned Paper Execution coordinator."""

    def __init__(self, coordinator: PaperExecutionCoordinator) -> None:
        if not isinstance(coordinator, PaperExecutionCoordinator):
            raise TypeError("coordinator must be PaperExecutionCoordinator")
        self._coordinator = coordinator

    def snapshot(self) -> PaperExecutionControlSnapshot:
        coordinator = self._coordinator
        orders = tuple(coordinator.orders.values())
        blocked_scopes = tuple(coordinator.blocked_scopes)
        return PaperExecutionControlSnapshot(
            native_lifecycle=coordinator.lifecycle.value,
            order_count=len(orders),
            active_order_count=sum(not order.state.terminal for order in orders),
            blocked_scope_count=len(blocked_scopes),
        )

    def list_orders(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[PaperOrderControlSnapshot, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        ordered = sorted(self._coordinator.orders.values(), key=lambda item: item.client_order_id)
        return tuple(self._project_order(item) for item in ordered[offset : offset + limit])

    def list_blocked_scopes(self) -> tuple[PaperBlockedScopeSnapshot, ...]:
        return tuple(
            PaperBlockedScopeSnapshot(portfolio_id=portfolio_id, instrument=instrument)
            for portfolio_id, instrument in sorted(self._coordinator.blocked_scopes)
        )

    @staticmethod
    def _project_order(order: OrderAggregate) -> PaperOrderControlSnapshot:
        return PaperOrderControlSnapshot(
            client_order_id=order.client_order_id,
            core_order_id=order.core_order_id,
            portfolio_id=order.portfolio_id,
            instrument=order.instrument,
            intent_role=order.intent_role,
            native_state=order.state.value,
            requested_quantity=order.requested_quantity,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            average_fill_price=order.average_fill_price,
            fees_paid=order.fees_paid,
            last_sequence=order.last_sequence,
            last_event_timestamp=_utc(
                order.last_event_timestamp,
                "order.last_event_timestamp",
            ),
        )
