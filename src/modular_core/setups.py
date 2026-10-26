from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast

from .models import SetupEvent, SetupStatus
from .serialization import deep_freeze
from .validation import require_nonblank, utc_datetime


class SetupTransitionError(ValueError):
    pass


_TERMINAL = {
    SetupStatus.CLOSED,
    SetupStatus.INVALIDATED,
    SetupStatus.EXPIRED,
    SetupStatus.REJECTED,
    SetupStatus.CANCELLED,
}

_ALLOWED: dict[SetupStatus, set[SetupStatus]] = {
    SetupStatus.CREATED: {
        SetupStatus.ARMED,
        SetupStatus.CONFIRMED,
        SetupStatus.INVALIDATED,
        SetupStatus.EXPIRED,
        SetupStatus.REJECTED,
        SetupStatus.CANCELLED,
    },
    SetupStatus.ARMED: {
        SetupStatus.CONFIRMED,
        SetupStatus.INVALIDATED,
        SetupStatus.EXPIRED,
        SetupStatus.REJECTED,
        SetupStatus.CANCELLED,
    },
    SetupStatus.CONFIRMED: {
        SetupStatus.ORDERED,
        SetupStatus.INVALIDATED,
        SetupStatus.EXPIRED,
        SetupStatus.REJECTED,
        SetupStatus.CANCELLED,
    },
    SetupStatus.ORDERED: {
        SetupStatus.PARTIALLY_FILLED,
        SetupStatus.FILLED,
        SetupStatus.EXPIRED,
        SetupStatus.REJECTED,
        SetupStatus.CANCELLED,
    },
    SetupStatus.PARTIALLY_FILLED: {
        SetupStatus.FILLED,
        SetupStatus.CLOSED,
        SetupStatus.EXPIRED,
        SetupStatus.CANCELLED,
    },
    SetupStatus.FILLED: {SetupStatus.CLOSED},
}


@dataclass(frozen=True, slots=True)
class SetupTransition:
    timestamp: datetime
    from_status: SetupStatus | None
    to_status: SetupStatus
    reason: str = ""
    event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp",
            utc_datetime(self.timestamp, "setup transition timestamp"),
        )
        if self.from_status is not None and not isinstance(self.from_status, SetupStatus):
            raise TypeError("setup transition from_status must be SetupStatus or None")
        if not isinstance(self.to_status, SetupStatus):
            raise TypeError("setup transition to_status must be SetupStatus")
        if not isinstance(self.reason, str):
            raise TypeError("setup transition reason must be str")
        if self.event_id is not None:
            require_nonblank(self.event_id, "setup transition event_id")


@dataclass(frozen=True, slots=True)
class SetupRecord:
    setup_id: str
    module: str
    symbol: str
    side: str | None
    created_at: datetime
    updated_at: datetime
    status: SetupStatus
    parent_setup_id: str | None = None
    child_setup_ids: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()
    position_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    transitions: tuple[SetupTransition, ...] = ()

    def __post_init__(self) -> None:
        require_nonblank(self.setup_id, "setup record setup_id")
        require_nonblank(self.module, "setup record module")
        require_nonblank(self.symbol, "setup record symbol")
        if self.side is not None:
            require_nonblank(self.side, "setup record side")
        created_at = utc_datetime(self.created_at, "setup record created_at")
        updated_at = utc_datetime(self.updated_at, "setup record updated_at")
        if updated_at < created_at:
            raise ValueError("setup record updated_at cannot precede created_at")
        if not isinstance(self.status, SetupStatus):
            raise TypeError("setup record status must be SetupStatus")
        for name in ("parent_setup_id", "position_id"):
            value = getattr(self, name)
            if value is not None:
                require_nonblank(value, f"setup record {name}")
        child_setup_ids = _identity_tuple(
            self.child_setup_ids,
            "child_setup_ids",
        )
        order_ids = _identity_tuple(self.order_ids, "order_ids")
        transitions = _transition_tuple(self.transitions)
        if transitions:
            if transitions[0].from_status is not None:
                raise ValueError("setup record first transition must start from None")
            if transitions[0].timestamp < created_at:
                raise ValueError("setup transition cannot precede record created_at")
            for previous, current in zip(transitions, transitions[1:]):
                if current.from_status is not previous.to_status:
                    raise ValueError("setup transition chain is discontinuous")
            if transitions[-1].to_status is not self.status:
                raise ValueError("setup record status must match the final transition")
            if transitions[-1].timestamp > updated_at:
                raise ValueError("setup transition cannot follow record updated_at")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("setup record metadata must be a mapping")
        frozen_metadata = deep_freeze(self.metadata)
        if not isinstance(frozen_metadata, Mapping):
            raise RuntimeError("frozen setup metadata must remain a mapping")

        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "child_setup_ids", child_setup_ids)
        object.__setattr__(self, "order_ids", order_ids)
        object.__setattr__(self, "transitions", transitions)
        object.__setattr__(
            self,
            "metadata",
            cast(Mapping[str, Any], frozen_metadata),
        )


class SetupRegistry:
    """Kernel-owned state machine connecting setup -> order -> fill -> position."""

    def __init__(self) -> None:
        self._records: dict[str, SetupRecord] = {}

    def apply(
        self,
        event: SetupEvent,
        timestamp: datetime,
        *,
        symbol: str = "*",
        event_id: str | None = None,
    ) -> SetupRecord:
        if not isinstance(event, SetupEvent):
            raise TypeError("event must be SetupEvent")
        timestamp = utc_datetime(timestamp, "setup registry timestamp")
        require_nonblank(symbol, "setup registry symbol")
        if event_id is not None:
            require_nonblank(event_id, "setup registry event_id")
        current = self._records.get(event.setup_id)
        if current is None:
            parent = event.parent_setup_id
            updated_parent: SetupRecord | None = None
            if parent is not None:
                parent_record = self._records.get(parent)
                if parent_record is None:
                    raise SetupTransitionError(f"unknown parent setup: {parent}")
                if timestamp < parent_record.updated_at:
                    raise SetupTransitionError(
                        "child setup timestamp cannot precede parent updated_at"
                    )
                updated_parent = replace(
                    parent_record,
                    child_setup_ids=parent_record.child_setup_ids + (event.setup_id,),
                    updated_at=timestamp,
                )
            record = SetupRecord(
                setup_id=event.setup_id,
                module=event.module,
                symbol=event.symbol or symbol,
                side=event.side.value if event.side is not None else None,
                created_at=timestamp,
                updated_at=timestamp,
                status=event.status,
                parent_setup_id=parent,
                metadata=event.metadata,
                transitions=(
                    SetupTransition(timestamp, None, event.status, event.reason, event_id),
                ),
            )
            if updated_parent is not None and parent is not None:
                self._records[parent] = updated_parent
            self._records[event.setup_id] = record
            return record

        if timestamp < current.updated_at:
            raise SetupTransitionError("setup event timestamp cannot move backwards")
        if current.module != event.module:
            raise SetupTransitionError(
                f"setup {event.setup_id} belongs to {current.module}, not {event.module}"
            )
        if current.status in _TERMINAL:
            raise SetupTransitionError(
                f"setup {event.setup_id} is terminal ({current.status.value})"
            )
        allowed = _ALLOWED.get(current.status, set())
        if event.status != current.status and event.status not in allowed:
            raise SetupTransitionError(
                f"invalid setup transition {current.status.value} -> {event.status.value}"
            )
        transition = SetupTransition(
            timestamp,
            current.status,
            event.status,
            event.reason,
            event_id,
        )
        record = replace(
            current,
            updated_at=timestamp,
            status=event.status,
            side=(event.side.value if event.side is not None else current.side),
            metadata={**dict(current.metadata), **dict(event.metadata)},
            transitions=current.transitions + (transition,),
        )
        self._records[event.setup_id] = record
        return record

    def link_order(self, setup_id: str, order_id: str, timestamp: datetime) -> SetupRecord:
        require_nonblank(setup_id, "setup_id")
        require_nonblank(order_id, "order_id")
        timestamp = utc_datetime(timestamp, "setup order timestamp")
        current = self._require(setup_id)
        if timestamp < current.updated_at:
            raise SetupTransitionError("setup order timestamp cannot move backwards")
        if order_id in current.order_ids:
            return current
        record = replace(
            current,
            order_ids=current.order_ids + (order_id,),
            updated_at=timestamp,
        )
        self._records[setup_id] = record
        return record

    def link_position(self, setup_id: str, position_id: str, timestamp: datetime) -> SetupRecord:
        require_nonblank(setup_id, "setup_id")
        require_nonblank(position_id, "position_id")
        timestamp = utc_datetime(timestamp, "setup position timestamp")
        current = self._require(setup_id)
        if timestamp < current.updated_at:
            raise SetupTransitionError("setup position timestamp cannot move backwards")
        if current.position_id is not None:
            if current.position_id == position_id:
                return current
            raise SetupTransitionError(
                f"setup {setup_id} is already linked to {current.position_id}"
            )
        record = replace(current, position_id=position_id, updated_at=timestamp)
        self._records[setup_id] = record
        return record

    def get(self, setup_id: str) -> SetupRecord | None:
        require_nonblank(setup_id, "setup_id")
        return self._records.get(setup_id)

    def all(self) -> tuple[SetupRecord, ...]:
        return tuple(self._records.values())

    def copy(self) -> SetupRegistry:
        clone = SetupRegistry()
        clone._records = dict(self._records)
        return clone

    def _require(self, setup_id: str) -> SetupRecord:
        record = self._records.get(setup_id)
        if record is None:
            raise SetupTransitionError(f"unknown setup: {setup_id}")
        return record


def _identity_tuple(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"setup record {name} must be a sequence")
    identities = tuple(value)
    for identity in identities:
        if not isinstance(identity, str):
            raise TypeError(f"setup record {name} must contain str")
        require_nonblank(identity, f"setup record {name} item")
    if len(identities) != len(set(identities)):
        raise ValueError(f"setup record {name} cannot contain duplicates")
    return identities


def _transition_tuple(value: object) -> tuple[SetupTransition, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("setup record transitions must be a sequence")
    transitions = tuple(value)
    if any(not isinstance(item, SetupTransition) for item in transitions):
        raise TypeError("setup record transitions must contain SetupTransition")
    for previous, current in zip(transitions, transitions[1:]):
        if current.timestamp < previous.timestamp:
            raise ValueError("setup transitions must be ordered by timestamp")
    return transitions
