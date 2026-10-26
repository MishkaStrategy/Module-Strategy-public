from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from modular_core import OrderEvent, OrderStatus

from ._utils import canonical_json, decimal, sha256_text, utc
from .enums import PaperOrderState, PaperReasonCode
from .exceptions import OrderStateError

_ALLOWED: dict[PaperOrderState, frozenset[PaperOrderState]] = {
    PaperOrderState.CREATED: frozenset(
        {
            PaperOrderState.SUBMITTED,
            PaperOrderState.REJECTED,
            PaperOrderState.ERROR,
        }
    ),
    PaperOrderState.SUBMITTED: frozenset(
        {
            PaperOrderState.ACKNOWLEDGED,
            PaperOrderState.OPEN,
            PaperOrderState.PARTIALLY_FILLED,
            PaperOrderState.FILLED,
            PaperOrderState.REJECTED,
            PaperOrderState.CANCEL_PENDING,
            PaperOrderState.CANCELED,
            PaperOrderState.EXPIRED,
            PaperOrderState.ERROR,
        }
    ),
    PaperOrderState.ACKNOWLEDGED: frozenset(
        {
            PaperOrderState.OPEN,
            PaperOrderState.PARTIALLY_FILLED,
            PaperOrderState.FILLED,
            PaperOrderState.CANCEL_PENDING,
            PaperOrderState.CANCELED,
            PaperOrderState.EXPIRED,
            PaperOrderState.REJECTED,
            PaperOrderState.ERROR,
        }
    ),
    PaperOrderState.OPEN: frozenset(
        {
            PaperOrderState.PARTIALLY_FILLED,
            PaperOrderState.FILLED,
            PaperOrderState.CANCEL_PENDING,
            PaperOrderState.CANCELED,
            PaperOrderState.EXPIRED,
            PaperOrderState.REJECTED,
            PaperOrderState.ERROR,
        }
    ),
    PaperOrderState.PARTIALLY_FILLED: frozenset(
        {
            PaperOrderState.PARTIALLY_FILLED,
            PaperOrderState.FILLED,
            PaperOrderState.CANCEL_PENDING,
            PaperOrderState.CANCELED,
            PaperOrderState.EXPIRED,
            PaperOrderState.ERROR,
        }
    ),
    PaperOrderState.CANCEL_PENDING: frozenset(
        {
            PaperOrderState.CANCELED,
            PaperOrderState.PARTIALLY_FILLED,
            PaperOrderState.FILLED,
            PaperOrderState.ERROR,
        }
    ),
    PaperOrderState.FILLED: frozenset(),
    PaperOrderState.CANCELED: frozenset(),
    PaperOrderState.EXPIRED: frozenset(),
    PaperOrderState.REJECTED: frozenset(),
    PaperOrderState.ERROR: frozenset(),
}

_STATUS_MAP = {
    OrderStatus.NEW: PaperOrderState.OPEN,
    OrderStatus.PENDING: PaperOrderState.OPEN,
    OrderStatus.PARTIALLY_FILLED: PaperOrderState.PARTIALLY_FILLED,
    OrderStatus.FILLED: PaperOrderState.FILLED,
    OrderStatus.CANCEL_PENDING: PaperOrderState.CANCEL_PENDING,
    OrderStatus.CANCELLED: PaperOrderState.CANCELED,
    OrderStatus.EXPIRED: PaperOrderState.EXPIRED,
    OrderStatus.REJECTED: PaperOrderState.REJECTED,
}


@dataclass(slots=True)
class OrderAggregate:
    client_order_id: str
    core_order_id: str
    requested_quantity: Decimal
    state: PaperOrderState = PaperOrderState.CREATED
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    fees_paid: Decimal = Decimal("0")
    last_sequence: int = 0
    last_event_timestamp: datetime | None = None
    processed_event_ids: set[str] = field(default_factory=set)
    processed_event_hashes: dict[str, str] = field(default_factory=dict)
    portfolio_id: str = ""
    instrument: str = ""
    intent_role: str = ""

    def __post_init__(self) -> None:
        self.requested_quantity = decimal(
            self.requested_quantity,
            "requested_quantity",
            positive=True,
        )
        self.filled_quantity = decimal(
            self.filled_quantity,
            "filled_quantity",
        )
        self.fees_paid = decimal(self.fees_paid, "fees_paid")
        if self.last_event_timestamp is not None:
            self.last_event_timestamp = utc(
                self.last_event_timestamp,
                "last_event_timestamp",
            )
        if self.average_fill_price is not None:
            self.average_fill_price = decimal(
                self.average_fill_price,
                "average_fill_price",
                positive=True,
            )
        if self.filled_quantity < 0 or self.filled_quantity > self.requested_quantity:
            raise ValueError("filled quantity violates order invariant")
        if self.fees_paid < 0:
            raise ValueError("fees_paid cannot be negative")
        if self.last_sequence < 0:
            raise ValueError("last_sequence cannot be negative")
        if self.last_sequence == 0 and self.last_event_timestamp is not None:
            raise ValueError("event timestamp requires a processed event")
        if set(self.processed_event_hashes) != set(self.processed_event_ids):
            raise ValueError("processed event IDs and fingerprints do not match")

    @property
    def remaining_quantity(self) -> Decimal:
        return max(
            Decimal("0"),
            self.requested_quantity - self.filled_quantity,
        )

    def transition(self, new_state: PaperOrderState) -> None:
        if new_state is self.state:
            return
        self._validate_transition(new_state)
        self.state = new_state

    def apply_event(self, event: OrderEvent, sequence: int) -> bool:
        event_id = event.event_id or f"core-order-{sequence:012d}"
        return self.apply_core_event(
            event,
            sequence=sequence,
            event_id=event_id,
        )

    def apply_core_event(
        self,
        event: OrderEvent,
        *,
        sequence: int,
        event_id: str,
    ) -> bool:
        if not isinstance(event_id, str) or not event_id.strip():
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                "event_id must be non-blank",
            )
        event_timestamp = utc(event.timestamp, "order event timestamp")
        fingerprint = self._event_fingerprint(event, event_id=event_id)
        if event_id in self.processed_event_ids:
            previous = self.processed_event_hashes.get(event_id)
            if not previous or previous != fingerprint:
                raise OrderStateError(
                    PaperReasonCode.INVARIANT_VIOLATION,
                    "duplicate event_id has conflicting content",
                )
            return False
        if sequence <= self.last_sequence:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                "out-of-order event sequence",
            )
        if self.last_event_timestamp is not None and event_timestamp < self.last_event_timestamp:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                "stale order event timestamp",
            )

        snapshot = event.order
        if snapshot.client_order_id != self.client_order_id or snapshot.id != self.core_order_id:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                "order identity mismatch",
            )
        filled = decimal(snapshot.filled_quantity, "filled_quantity")
        remaining = decimal(snapshot.remaining_quantity, "remaining_quantity")
        fees = decimal(snapshot.fees_paid, "fees_paid")
        average = (
            None
            if snapshot.average_fill_price is None
            else decimal(
                snapshot.average_fill_price,
                "average_fill_price",
                positive=True,
            )
        )
        if filled < self.filled_quantity:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "cumulative fill decreased",
            )
        if filled > self.requested_quantity:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "filled quantity exceeds requested quantity",
            )
        if remaining < 0 or abs((filled + remaining) - self.requested_quantity) > Decimal(
            "0.000000001"
        ):
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "filled and remaining quantities are inconsistent",
            )
        if fees < 0:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "fees cannot be negative",
            )
        if filled == 0 and average is not None:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "unfilled order cannot have average fill price",
            )
        if filled > 0 and average is None:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "filled order requires average fill price",
            )

        try:
            new_state = _STATUS_MAP[snapshot.status]
        except KeyError as exc:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                f"unsupported Core order status: {snapshot.status!r}",
            ) from exc
        self._validate_transition(new_state)
        if new_state is PaperOrderState.FILLED and filled != self.requested_quantity:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "filled state requires full cumulative quantity",
            )
        if new_state is PaperOrderState.PARTIALLY_FILLED and not (
            Decimal("0") < filled < self.requested_quantity
        ):
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "partially filled state has invalid cumulative quantity",
            )

        self.state = new_state
        self.filled_quantity = filled
        self.average_fill_price = average
        self.fees_paid = fees
        self.last_sequence = sequence
        self.last_event_timestamp = event_timestamp
        self.processed_event_ids.add(event_id)
        self.processed_event_hashes[event_id] = fingerprint
        return True

    def _validate_transition(self, new_state: PaperOrderState) -> None:
        if new_state is self.state:
            return
        if self.state.terminal or new_state not in _ALLOWED[self.state]:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                f"invalid order transition {self.state.value}->{new_state.value}",
            )

    @staticmethod
    def _event_fingerprint(
        event: OrderEvent,
        *,
        event_id: str,
    ) -> str:
        snapshot = event.order
        payload: dict[str, Any] = {
            "event_id": event_id,
            "timestamp": event.timestamp,
            "event": event.event,
            "reason": event.reason,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "order": {
                "id": snapshot.id,
                "source_module": snapshot.source_module,
                "created_at": snapshot.created_at,
                "side": snapshot.side,
                "order_type": snapshot.order_type,
                "quantity": snapshot.quantity,
                "price": snapshot.price,
                "stop_loss": snapshot.stop_loss,
                "take_profit": snapshot.take_profit,
                "tag": snapshot.tag,
                "setup_id": snapshot.setup_id,
                "client_order_id": snapshot.client_order_id,
                "status": snapshot.status,
                "filled_quantity": snapshot.filled_quantity,
                "remaining_quantity": snapshot.remaining_quantity,
                "average_fill_price": snapshot.average_fill_price,
                "fees_paid": snapshot.fees_paid,
                "time_in_force": snapshot.time_in_force,
                "post_only": snapshot.post_only,
                "reduce_only": snapshot.reduce_only,
                "good_till_time": snapshot.good_till_time,
                "trigger_price": snapshot.trigger_price,
                "limit_price": snapshot.limit_price,
                "triggered_at": snapshot.triggered_at,
                "triggered_index": snapshot.triggered_index,
                "metadata": _optional_mapping(snapshot, "metadata"),
            },
        }
        return sha256_text(canonical_json(payload))


def _optional_mapping(value: object, name: str) -> Mapping[str, Any]:
    candidate = getattr(value, name, None)
    if candidate is None:
        return {}
    if not isinstance(candidate, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return candidate
