from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .canonical import deep_thaw, fingerprint
from .decimal_utils import (
    ZERO,
    canonical_decimal,
    nonnegative_decimal,
    weighted_average,
)
from .enums import AdjustmentDirection, ScalingLifecycle
from .models import PositionAdjustmentPlan

STATE_SCHEMA_VERSION = "4"


@dataclass(slots=True)
class PlannerState:
    config_fingerprint: str
    position_id: str | None = None
    setup_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    initial_quantity: Decimal = ZERO
    initial_entry_price: Decimal = ZERO
    initial_stop_price: Decimal = ZERO
    last_quantity: Decimal = ZERO
    last_entry_price: Decimal = ZERO
    attached_index: int | None = None
    last_adjustment_index: int | None = None
    last_adjustment_price: Decimal | None = None
    scale_in_count: int = 0
    scale_out_count: int = 0
    completed_stages: set[int] = field(default_factory=set)
    pending_plan: Mapping[str, Any] | None = None
    pending_fingerprint: str | None = None
    pending_approved_quantity: Decimal | None = None
    pending_feedback_fingerprint: str | None = None
    lifecycle: ScalingLifecycle = ScalingLifecycle.IDLE
    cooldown_until_index: int = -1
    last_seen_index: int = -1
    processed_event_ids: deque[str] = field(default_factory=deque)
    processed_event_set: set[str] = field(default_factory=set)
    history: deque[Mapping[str, Any]] = field(default_factory=deque)
    counters: dict[str, int] = field(default_factory=dict)

    def reset_position(self) -> None:
        self.position_id = None
        self.setup_id = None
        self.symbol = None
        self.side = None
        self.initial_quantity = ZERO
        self.initial_entry_price = ZERO
        self.initial_stop_price = ZERO
        self.last_quantity = ZERO
        self.last_entry_price = ZERO
        self.attached_index = None
        self.last_adjustment_index = None
        self.last_adjustment_price = None
        self.scale_in_count = 0
        self.scale_out_count = 0
        self.completed_stages.clear()
        self.pending_plan = None
        self.pending_fingerprint = None
        self.pending_approved_quantity = None
        self.pending_feedback_fingerprint = None
        self.lifecycle = ScalingLifecycle.IDLE
        self.cooldown_until_index = -1

    def remember_event(self, event_id: str, capacity: int) -> None:
        if event_id in self.processed_event_set:
            return
        self.processed_event_ids.append(event_id)
        self.processed_event_set.add(event_id)
        while len(self.processed_event_ids) > capacity:
            removed = self.processed_event_ids.popleft()
            self.processed_event_set.discard(removed)

    def add_history(self, row: Mapping[str, Any], capacity: int) -> None:
        self.history.append(dict(row))
        while len(self.history) > capacity:
            self.history.popleft()

    def snapshot(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": self.config_fingerprint,
            "position_id": self.position_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "side": self.side,
            "initial_quantity": canonical_decimal(self.initial_quantity),
            "initial_entry_price": canonical_decimal(self.initial_entry_price),
            "initial_stop_price": canonical_decimal(self.initial_stop_price),
            "last_quantity": canonical_decimal(self.last_quantity),
            "last_entry_price": canonical_decimal(self.last_entry_price),
            "attached_index": self.attached_index,
            "last_adjustment_index": self.last_adjustment_index,
            "last_adjustment_price": (
                None
                if self.last_adjustment_price is None
                else canonical_decimal(self.last_adjustment_price)
            ),
            "scale_in_count": self.scale_in_count,
            "scale_out_count": self.scale_out_count,
            "completed_stages": sorted(self.completed_stages),
            "pending_plan": None if self.pending_plan is None else deep_thaw(self.pending_plan),
            "pending_fingerprint": self.pending_fingerprint,
            "pending_approved_quantity": (
                None
                if self.pending_approved_quantity is None
                else canonical_decimal(self.pending_approved_quantity)
            ),
            "pending_feedback_fingerprint": self.pending_feedback_fingerprint,
            "lifecycle": self.lifecycle.value,
            "cooldown_until_index": self.cooldown_until_index,
            "last_seen_index": self.last_seen_index,
            "processed_event_ids": list(self.processed_event_ids),
            "history": [deep_thaw(row) for row in self.history],
            "counters": dict(self.counters),
        }
        payload["state_fingerprint"] = fingerprint(payload)
        return payload

    @classmethod
    def restore(
        cls,
        payload: Mapping[str, object],
        *,
        config_fingerprint: str,
        max_history: int,
        max_processed_events: int,
    ) -> PlannerState:
        schema_version = str(payload.get("schema_version", ""))
        if schema_version not in {"1", "2", "3", STATE_SCHEMA_VERSION}:
            raise ValueError("unsupported position scaling state schema_version")
        if payload.get("config_fingerprint") != config_fingerprint:
            raise ValueError("position scaling state config fingerprint mismatch")
        allowed = {
            "schema_version",
            "config_fingerprint",
            "position_id",
            "setup_id",
            "symbol",
            "side",
            "initial_quantity",
            "initial_entry_price",
            "initial_stop_price",
            "last_quantity",
            "last_entry_price",
            "attached_index",
            "last_adjustment_index",
            "last_adjustment_price",
            "scale_in_count",
            "scale_out_count",
            "completed_stages",
            "pending_plan",
            "pending_fingerprint",
            "pending_approved_quantity",
            "pending_feedback_fingerprint",
            "lifecycle",
            "cooldown_until_index",
            "last_seen_index",
            "processed_event_ids",
            "history",
            "counters",
            "state_fingerprint",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown position scaling state fields: {', '.join(unknown)}")
        state_fingerprint = payload.get("state_fingerprint")
        if schema_version == STATE_SCHEMA_VERSION:
            if not isinstance(state_fingerprint, str) or not state_fingerprint:
                raise ValueError("position scaling state fingerprint is required")
            fingerprint_payload = dict(payload)
            fingerprint_payload.pop("state_fingerprint", None)
            if fingerprint(fingerprint_payload) != state_fingerprint:
                raise ValueError("position scaling state fingerprint mismatch")
        elif state_fingerprint is not None:
            raise ValueError("legacy position scaling state cannot carry state_fingerprint")
        events = payload.get("processed_event_ids", [])
        history = payload.get("history", [])
        completed = payload.get("completed_stages", [])
        counters = payload.get("counters", {})
        pending = payload.get("pending_plan")
        if (
            isinstance(events, (str, bytes, bytearray))
            or not isinstance(events, Sequence)
            or len(events) > max_processed_events
        ):
            raise ValueError("processed_event_ids is invalid or oversized")
        if len(events) != len(set(str(item) for item in events)):
            raise ValueError("processed_event_ids contains duplicates")
        if (
            isinstance(history, (str, bytes, bytearray))
            or not isinstance(history, Sequence)
            or len(history) > max_history
        ):
            raise ValueError("history is invalid or oversized")
        if isinstance(completed, (str, bytes, bytearray)) or not isinstance(completed, Sequence):
            raise ValueError("completed_stages must be an array")
        if not isinstance(counters, Mapping):
            raise ValueError("counters must be a mapping")
        if pending is not None and not isinstance(pending, Mapping):
            raise ValueError("pending_plan must be a mapping or null")
        state = cls(config_fingerprint=config_fingerprint)
        state.position_id = _optional_string(payload.get("position_id"), "position_id")
        state.setup_id = _optional_string(payload.get("setup_id"), "setup_id")
        state.symbol = _optional_string(payload.get("symbol"), "symbol")
        state.side = _optional_string(payload.get("side"), "side")
        state.initial_quantity = nonnegative_decimal(
            payload.get("initial_quantity", 0), "initial_quantity"
        )
        state.initial_entry_price = nonnegative_decimal(
            payload.get("initial_entry_price", 0), "initial_entry_price"
        )
        state.initial_stop_price = nonnegative_decimal(
            payload.get("initial_stop_price", 0), "initial_stop_price"
        )
        state.last_quantity = nonnegative_decimal(payload.get("last_quantity", 0), "last_quantity")
        state.last_entry_price = nonnegative_decimal(
            payload.get("last_entry_price", 0), "last_entry_price"
        )
        state.attached_index = _optional_int(payload.get("attached_index"), "attached_index")
        state.last_adjustment_index = _optional_int(
            payload.get("last_adjustment_index"), "last_adjustment_index"
        )
        last_adjustment_price = payload.get("last_adjustment_price")
        state.last_adjustment_price = (
            None
            if last_adjustment_price is None
            else nonnegative_decimal(last_adjustment_price, "last_adjustment_price")
        )
        state.scale_in_count = _nonnegative_int(payload.get("scale_in_count", 0), "scale_in_count")
        state.scale_out_count = _nonnegative_int(
            payload.get("scale_out_count", 0), "scale_out_count"
        )
        state.completed_stages = {_positive_int(item, "completed_stage") for item in completed}
        raw_pending = None if pending is None else dict(pending)
        state.pending_plan = raw_pending
        state.pending_fingerprint = _optional_string(
            payload.get("pending_fingerprint"), "pending_fingerprint"
        )
        approved_quantity = payload.get("pending_approved_quantity")
        state.pending_approved_quantity = (
            None
            if approved_quantity is None
            else nonnegative_decimal(approved_quantity, "pending_approved_quantity")
        )
        state.pending_feedback_fingerprint = _optional_string(
            payload.get("pending_feedback_fingerprint"),
            "pending_feedback_fingerprint",
        )
        state.lifecycle = ScalingLifecycle(str(payload.get("lifecycle", "idle")))
        state.cooldown_until_index = _integer(
            payload.get("cooldown_until_index", -1), "cooldown_until_index"
        )
        state.last_seen_index = _integer(payload.get("last_seen_index", -1), "last_seen_index")
        state.processed_event_ids = deque(str(item) for item in events)
        state.processed_event_set = set(state.processed_event_ids)
        state.history = deque(dict(item) for item in history if isinstance(item, Mapping))
        if len(state.history) != len(history):
            raise ValueError("history entries must be mappings")
        state.counters = {
            _counter_key(key): _nonnegative_int(value, f"counter {key}")
            for key, value in counters.items()
        }
        if state.position_id is None:
            if any(
                value > ZERO
                for value in (
                    state.initial_quantity,
                    state.last_quantity,
                    state.initial_entry_price,
                )
            ):
                raise ValueError("flat state cannot contain position quantities or prices")
        elif any(
            value <= ZERO
            for value in (
                state.initial_quantity,
                state.last_quantity,
                state.initial_entry_price,
                state.last_entry_price,
            )
        ):
            raise ValueError("open position state requires positive quantities and prices")
        if state.pending_plan is not None:
            raw_fingerprint = fingerprint(state.pending_plan)
            if state.pending_fingerprint != raw_fingerprint:
                raise ValueError("pending plan fingerprint mismatch")
            normalized_pending = (
                _migrate_legacy_pending_plan(state.pending_plan)
                if schema_version in {"1", "2", "3"}
                else dict(state.pending_plan)
            )
            plan = PositionAdjustmentPlan.from_mapping(normalized_pending)
            state.pending_plan = plan.to_mapping()
            state.pending_fingerprint = fingerprint(state.pending_plan)
            if state.position_id != plan.position_id:
                raise ValueError("pending plan position_id mismatch")
            if state.pending_approved_quantity is not None:
                if state.pending_approved_quantity <= ZERO:
                    raise ValueError("pending approved quantity must be positive")
                if state.pending_approved_quantity > plan.desired_quantity:
                    raise ValueError("pending approved quantity exceeds desired quantity")
            if (
                state.pending_feedback_fingerprint is not None
                and state.pending_approved_quantity is None
            ):
                raise ValueError("feedback fingerprint requires approved quantity")
        elif (
            state.pending_fingerprint is not None
            or state.pending_approved_quantity is not None
            or state.pending_feedback_fingerprint is not None
        ):
            raise ValueError("pending state requires pending_plan")
        pending_lifecycles = {
            ScalingLifecycle.PLAN_CREATED,
            ScalingLifecycle.RISK_PENDING,
            ScalingLifecycle.APPROVED,
            ScalingLifecycle.ORDER_PENDING,
            ScalingLifecycle.PARTIALLY_FILLED,
        }
        approved_lifecycles = {
            ScalingLifecycle.APPROVED,
            ScalingLifecycle.ORDER_PENDING,
            ScalingLifecycle.PARTIALLY_FILLED,
        }
        if state.pending_plan is not None and state.lifecycle not in pending_lifecycles:
            raise ValueError("pending plan is inconsistent with lifecycle")
        if state.pending_plan is None and state.lifecycle in pending_lifecycles:
            raise ValueError("pending lifecycle requires pending plan")
        if (
            state.pending_approved_quantity is not None
            and state.lifecycle not in approved_lifecycles
        ):
            raise ValueError("approved quantity is inconsistent with lifecycle")
        if state.lifecycle in approved_lifecycles and state.pending_approved_quantity is None:
            raise ValueError("approved lifecycle requires approved quantity")
        return state


def _migrate_legacy_pending_plan(payload: Mapping[str, object]) -> dict[str, object]:
    """Rebuild derivable geometry lost by legacy context-sensitive Decimal serialization."""
    migrated = dict(payload)
    direction = AdjustmentDirection(str(migrated.get("direction", "none")))
    current_quantity = nonnegative_decimal(migrated.get("current_quantity", 0), "current_quantity")
    desired_quantity = nonnegative_decimal(migrated.get("desired_quantity", 0), "desired_quantity")
    current_average = nonnegative_decimal(
        migrated.get("current_average_price", 0), "current_average_price"
    )
    desired_price = nonnegative_decimal(migrated.get("desired_price", 0), "desired_price")
    if direction is AdjustmentDirection.INCREASE:
        migrated["expected_total_quantity"] = canonical_decimal(current_quantity + desired_quantity)
        migrated["expected_average_price"] = canonical_decimal(
            weighted_average(current_average, current_quantity, desired_price, desired_quantity)
        )
    elif direction is AdjustmentDirection.DECREASE:
        migrated["expected_total_quantity"] = canonical_decimal(current_quantity - desired_quantity)
        migrated["expected_average_price"] = canonical_decimal(current_average)
    else:
        migrated["expected_total_quantity"] = canonical_decimal(current_quantity)
        migrated["expected_average_price"] = canonical_decimal(current_average)
    return migrated


def _counter_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("counter keys must be non-empty strings")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    result = _integer(value, name)
    if result < 0:
        raise ValueError(f"{name} must be >= 0")
    return result


def _positive_int(value: object, name: str) -> int:
    result = _integer(value, name)
    if result < 1:
        raise ValueError(f"{name} must be >= 1")
    return result


def _optional_int(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be null or non-blank string")
    return value
