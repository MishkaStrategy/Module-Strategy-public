"""Opt-in Paper venue bridge over the canonical Core SQLite aggregate.

This is deliberately not a production composition. Existing Paper/Runner venue
calls do not carry durable effect identity or authority context. A trusted host
must first persist a complete CoreLifecycleEffect and explicitly bind that effect
to one invocation. Unbound mutation calls fail closed; no context is inferred
from prices, timestamps, client order IDs, or the latest lifecycle generation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .broker_state import _decode_instrument, _decode_order, _decode_position
from .events import EventEnvelope
from .execution import Portfolio
from .lifecycle import (
    CoreCommitStatus,
    CoreEffectCommitReceipt,
    CoreLifecycleCommitCoordinator,
    CoreLifecycleEffect,
)
from .lifecycle_sqlite import CoreLifecycleStoreSnapshot, SQLiteCoreLifecycleCommitPort
from .models import (
    Candle,
    CancelRequest,
    ClosePositionRequest,
    Order,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    PositionUpdateRequest,
)
from .serialization import to_jsonable
from .validation import positive_number


class CorePaperContextUnavailable(RuntimeError):
    """No persisted, trusted effect context covers this venue mutation."""


class CorePaperEffectNotCommitted(RuntimeError):
    """A denied/unavailable/ambiguous effect must not become a venue success."""

    def __init__(self, effect_id: str, status: CoreCommitStatus, reason: str) -> None:
        self.effect_id = effect_id
        self.status = status
        super().__init__(f"Core effect {effect_id} is {status.value}: {reason}")


@dataclass(frozen=True, slots=True)
class CoreInvocationRef:
    """Immutable in-memory identity for one bound Core venue invocation."""

    effect_id: str
    request_digest: str
    operation: str
    canonical_payload_digest: str
    target_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("effect_id", "operation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-blank canonical text")
        for name in ("request_digest", "canonical_payload_digest"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.target_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.target_ids
        ):
            raise TypeError("target_ids must be a tuple of non-blank strings")

    @classmethod
    def from_request(cls, request: CoreLifecycleEffect) -> CoreInvocationRef:
        if type(request) is not CoreLifecycleEffect:
            raise TypeError("request must be CoreLifecycleEffect")
        request.__post_init__()
        return cls(
            request.effect_id,
            request.request_digest,
            request.operation,
            request.canonical_payload_digest,
            request.target_ids,
        )

    def matches_request(self, request: CoreLifecycleEffect) -> bool:
        return type(request) is CoreLifecycleEffect and self == type(self).from_request(request)

    def matches_receipt(self, receipt: CoreEffectCommitReceipt) -> bool:
        return (
            type(receipt) is CoreEffectCommitReceipt
            and receipt.effect_id == self.effect_id
            and receipt.operation == self.operation
            and receipt.canonical_payload_digest == self.canonical_payload_digest
            and receipt.target_ids == self.target_ids
        )


class CorePaperCommittedProjectionUnavailable(RuntimeError):
    """Core committed an effect, but Paper could not construct its projection."""

    _receipt: CoreEffectCommitReceipt
    _invocation_ref: CoreInvocationRef
    __slots__ = ("_receipt", "_invocation_ref")

    def __init__(
        self,
        receipt: CoreEffectCommitReceipt,
        invocation_ref: CoreInvocationRef,
        cause: Exception,
    ) -> None:
        if type(receipt) is not CoreEffectCommitReceipt:
            raise TypeError("receipt must be CoreEffectCommitReceipt")
        if type(invocation_ref) is not CoreInvocationRef:
            raise TypeError("invocation_ref must be CoreInvocationRef")
        receipt.__post_init__()
        if (
            receipt.effect_id != invocation_ref.effect_id
            or receipt.operation != invocation_ref.operation
            or receipt.canonical_payload_digest != invocation_ref.canonical_payload_digest
            or receipt.target_ids != invocation_ref.target_ids
        ):
            raise ValueError("receipt does not match exception invocation reference")
        object.__setattr__(self, "_receipt", receipt)
        object.__setattr__(self, "_invocation_ref", invocation_ref)
        RuntimeError.__init__(
            self,
            f"Core effect {invocation_ref.effect_id} committed but Paper projection is unavailable "
            f"({type(cause).__name__})",
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Core committed effect evidence is immutable")

    @property
    def receipt(self) -> CoreEffectCommitReceipt:
        return self._receipt

    @property
    def effect_id(self) -> str:
        return self._invocation_ref.effect_id

    @property
    def request_digest(self) -> str:
        return self._invocation_ref.request_digest

    @property
    def invocation_ref(self) -> CoreInvocationRef:
        return self._invocation_ref


class DurableCorePaperEffectSource(Protocol):
    """Trusted host adapter for already persisted, immutable effect requests.

    load_effect must survive restart and return the same complete request for an
    ID, including binding, generation, writer epoch, authorities and payload. It
    must not refresh/rebase an existing request or derive authority from caller
    labels. The bridge provides no implementation or fallback for this authority
    and persistence boundary.
    """

    def load_effect(self, effect_id: str) -> CoreLifecycleEffect: ...


class CoreReceiptAwarePaperVenue(Protocol):
    """Optional mutation adapter contract for Core-correlated Paper calls."""

    core_receipt_aware: bool

    def invocation_ref(self, operation: str, **fields: Any) -> CoreInvocationRef: ...

    def receipt_for_invocation(
        self, invocation_ref: CoreInvocationRef
    ) -> CoreEffectCommitReceipt | None: ...


def canonical_core_paper_payload(operation: str, **fields: Any) -> bytes:
    """Encode the exact transport payload before the host persists its request."""
    return json.dumps(
        to_jsonable({"operation": operation, **fields}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class CoreLedgerEventView:
    """Read-only EventStore projection; all appends belong to Core transactions."""

    def __init__(self, store: SQLiteCoreLifecycleCommitPort) -> None:
        self._store = store

    def read(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        return tuple(
            event for event in self._store.snapshot().events if event.sequence > after_sequence
        )

    def append(self, event: EventEnvelope) -> None:
        raise CorePaperContextUnavailable("Core event writes require a canonical transaction")

    def append_many(self, events: Sequence[EventEnvelope]) -> None:
        raise CorePaperContextUnavailable("Core event writes require a canonical transaction")


class SQLiteCorePaperVenue:
    """One ledger, with explicit per-effect capabilities and detached read views.

    for_effect loads context from the trusted durable source. Its returned venue
    may only invoke the exact saved operation/payload; all other mutations fail
    before Core is called. Reconstruct the capability from the same ID after a
    crash. Core owns idempotency, currentness checks and the durable receipt.
    """

    core_receipt_aware = True

    def __init__(
        self,
        store: SQLiteCoreLifecycleCommitPort,
        *,
        effect_source: DurableCorePaperEffectSource | None = None,
    ) -> None:
        if type(store) is not SQLiteCoreLifecycleCommitPort:
            raise TypeError("store must be the canonical SQLiteCoreLifecycleCommitPort")
        if effect_source is not None and not callable(getattr(effect_source, "load_effect", None)):
            raise TypeError("effect_source must expose load_effect()")
        self._store = store
        self._source = effect_source
        self._coordinator = CoreLifecycleCommitCoordinator(store)
        self._request: CoreLifecycleEffect | None = None
        self._receipt: CoreEffectCommitReceipt | None = None
        self.event_store = CoreLedgerEventView(store)

    def for_effect(self, effect_id: str) -> SQLiteCorePaperVenue:
        if not isinstance(effect_id, str) or not effect_id.strip():
            raise ValueError("effect_id must be non-empty text")
        if self._source is None:
            raise CorePaperContextUnavailable("trusted durable Core effect source unavailable")
        request = self._source.load_effect(effect_id)
        if type(request) is not CoreLifecycleEffect or request.effect_id != effect_id:
            raise CorePaperContextUnavailable("durable effect source returned another request")
        request.__post_init__()
        bound = SQLiteCorePaperVenue(self._store, effect_source=self._source)
        bound._request = request
        return bound

    @property
    def committed_receipt(self) -> CoreEffectCommitReceipt | None:
        """Receipt observed by this invocation, never an inferred commit outcome."""
        return self._receipt

    def validate_invocation(self, operation: str, **fields: Any) -> str:
        """Validate saved context without mutation; return its immutable digest."""
        request = self._request
        if request is None:
            raise CorePaperContextUnavailable("venue mutation requires a saved Core effect ID")
        if (
            request.operation != operation
            or request.canonical_payload != canonical_core_paper_payload(operation, **fields)
        ):
            raise CorePaperContextUnavailable("venue invocation differs from the persisted effect")
        return request.request_digest

    def invocation_ref(self, operation: str, **fields: Any) -> CoreInvocationRef:
        """Capture exact current bound request identity before invoking the venue."""
        self.validate_invocation(operation, **fields)
        request = self._request
        if request is None:
            raise CorePaperContextUnavailable("venue mutation requires a saved Core effect ID")
        return CoreInvocationRef.from_request(request)

    def receipt_for_invocation(
        self, invocation_ref: CoreInvocationRef
    ) -> CoreEffectCommitReceipt | None:
        """Return only this capability's receipt matching the exact saved request."""
        if type(invocation_ref) is not CoreInvocationRef:
            return None
        request = self._request
        receipt = self._receipt
        if (
            request is None
            or receipt is None
            or not invocation_ref.matches_request(request)
            or not receipt.matches_request(request)
        ):
            return None
        return receipt

    def _commit(self, operation: str, **fields: Any) -> CoreEffectCommitReceipt:
        self._receipt = None
        self.validate_invocation(operation, **fields)
        request = self._request
        if request is None:  # validate_invocation already rejects unbound capabilities.
            raise CorePaperContextUnavailable("venue mutation requires a saved Core effect ID")
        result = self._coordinator.commit_effect(request)
        if result.status is not CoreCommitStatus.COMMITTED:
            raise CorePaperEffectNotCommitted(request.effect_id, result.status, result.reason)
        receipt = result.receipt
        if type(receipt) is not CoreEffectCommitReceipt or not receipt.matches_request(request):
            raise CorePaperContextUnavailable("Core returned an invalid effect receipt")
        self._receipt = receipt
        return receipt

    def submit(
        self,
        request: OrderRequest,
        *,
        source_module: str,
        created_at: datetime,
        current_index: int,
        mark_price: float,
    ) -> Order:
        invocation_ref = self.invocation_ref(
            "submit_order",
            request=request,
            source_module=source_module,
            created_at=created_at,
            current_index=current_index,
            mark_price=mark_price,
        )
        if self._request is None:
            raise CorePaperContextUnavailable("venue mutation requires a saved Core effect ID")
        receipt = self._commit(
            "submit_order",
            request=request,
            source_module=source_module,
            created_at=created_at,
            current_index=current_index,
            mark_price=mark_price,
        )
        try:
            orders = tuple(
                value
                for value in self._store.snapshot().broker_state.orders
                if value["client_order_id"] == request.client_order_id
            )
            if len(orders) != 1:
                raise CorePaperContextUnavailable("committed order projection is unavailable")
            # This detached object is not the ledger's mutable broker or its state.
            return _decode_order(orders[0])
        except Exception as exc:
            raise CorePaperCommittedProjectionUnavailable(
                receipt,
                invocation_ref,
                exc,
            ) from exc

    def cancel(self, request: CancelRequest, *, timestamp: datetime, source_module: str) -> int:
        self._commit(
            "cancel_order", request=request, timestamp=timestamp, source_module=source_module
        )
        return 1  # Core only commits an exact, single pending-order cancellation.

    def update_position(
        self,
        request: PositionUpdateRequest,
        *,
        timestamp: datetime,
        source_module: str,
        mark_price: float,
    ) -> bool:
        self._commit(
            "update_position",
            request=request,
            timestamp=timestamp,
            source_module=source_module,
            mark_price=mark_price,
        )
        return True

    def schedule_close(
        self,
        request: ClosePositionRequest,
        *,
        timestamp: datetime,
        current_index: int,
        source_module: str,
    ) -> bool:
        self._commit(
            "schedule_close",
            request=request,
            timestamp=timestamp,
            current_index=current_index,
            source_module=source_module,
        )
        return True

    def process_bar(self, bar: object, index: int) -> None:
        if not isinstance(bar, Candle):
            raise TypeError("process_bar requires a canonical Candle")
        self._commit("process_bar", bar=bar, index=index)

    def cancel_all(self, timestamp: datetime, reason: str = "end_of_data") -> None:
        self._commit("cancel_all", timestamp=timestamp, reason=reason)

    def close_position(self, *args: Any, **kwargs: Any) -> None:
        raise CorePaperContextUnavailable(
            "direct priced close_position has no canonical Core effect; use schedule_close"
        )

    def pending_snapshots(self) -> tuple[OrderSnapshot, ...]:
        return _pending_snapshots(self._store.snapshot())

    def position_snapshot(self, mark_price: float) -> PositionSnapshot | None:
        return _position_snapshot(self._store.snapshot(), mark_price)

    def snapshot_evidence(self, event: object) -> Mapping[str, Any]:
        """Runner-compatible evidence captured from one SQLite read transaction."""
        bar = getattr(event, "candle", None)
        if not isinstance(bar, Candle):
            raise TypeError("evidence requires an event with a canonical Candle")
        snapshot = self._store.snapshot()
        if bar.symbol != _decode_instrument(snapshot.broker_state.instrument).symbol:
            raise ValueError("evidence symbol differs from the canonical broker")
        value = to_jsonable(
            {
                "core_event_sequence": snapshot.events[-1].sequence if snapshot.events else 0,
                "core_commit_sequence": snapshot.commit_sequence,
                "core_lifecycle_generation": snapshot.lifecycle.expected_generation,
                "pending_orders": _pending_snapshots(snapshot),
                "position": _position_snapshot(snapshot, bar.close),
                "symbol": bar.symbol,
                "mark_price": bar.close,
            }
        )
        if not isinstance(value, Mapping):
            raise TypeError("Core evidence must be a mapping")
        return value


def _pending_snapshots(snapshot: CoreLifecycleStoreSnapshot) -> tuple[OrderSnapshot, ...]:
    pending = frozenset(snapshot.broker_state.pending_order_ids)
    return tuple(
        OrderSnapshot.from_order(_decode_order(value))
        for value in sorted(snapshot.broker_state.orders, key=lambda item: str(item["id"]))
        if value["id"] in pending
    )


def _position_snapshot(
    snapshot: CoreLifecycleStoreSnapshot,
    mark_price: float,
) -> PositionSnapshot | None:
    positive_number(mark_price, "mark_price")
    state = snapshot.broker_state
    position = None if state.position is None else _decode_position(state.position)
    return Portfolio(
        cash=state.cash,
        position=position,
        contract_size=_decode_instrument(state.instrument).contract_size,
    ).position_snapshot(mark_price)


_COMPOSITION_BLOCKERS = (
    "Paper plan, OCO, repair and cancellation callers lack a durable per-effect context adapter",
    "default Runner PaperBarExecutor lacks Core context; the opt-in ledger caller is not qualified",
    "trusted production identity, authority and deployment-state bindings are not integrated",
    "populated-owner migration into the canonical Core ledger is not implemented",
)


@dataclass(frozen=True, slots=True)
class LocalCorePaperLedgerBinding:
    """A usable local bridge, explicitly unable to qualify a PAPER composition."""

    venue: SQLiteCorePaperVenue
    qualification_complete: bool = field(default=False, init=False)
    qualification_blockers: tuple[str, ...] = field(default=_COMPOSITION_BLOCKERS, init=False)

    def require_runtime_qualified(self) -> None:
        raise CorePaperContextUnavailable("; ".join(self.qualification_blockers))


def prepare_local_core_paper_binding(
    store: SQLiteCoreLifecycleCommitPort,
    *,
    effect_source: DurableCorePaperEffectSource | None = None,
) -> LocalCorePaperLedgerBinding:
    """Prepare an opt-in seam, without activating or changing any composition."""
    return LocalCorePaperLedgerBinding(SQLiteCorePaperVenue(store, effect_source=effect_source))
