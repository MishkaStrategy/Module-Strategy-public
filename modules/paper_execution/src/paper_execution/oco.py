from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from ._utils import canonical_json, decimal, sha256_text
from .enums import PaperReasonCode
from .exceptions import OrderStateError

MAX_OCO_MEMBERS = 10_000


class OcoAdjustmentKind(str, Enum):
    CANCEL = "cancel"
    REDUCE_REMAINING_TO = "reduce_remaining_to"


@dataclass(frozen=True, slots=True)
class OcoAdjustment:
    client_order_id: str
    kind: OcoAdjustmentKind
    remaining_quantity: Decimal = Decimal("0")


@dataclass(slots=True)
class OcoGroup:
    group_id: str
    position_quantity: Decimal
    members: dict[str, Decimal] = field(default_factory=dict)
    filled: dict[str, Decimal] = field(default_factory=dict)
    canceled: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id.strip():
            raise ValueError("OCO group_id must be non-blank")
        self.position_quantity = decimal(
            self.position_quantity,
            "position_quantity",
            positive=True,
        )

    def register(self, client_order_id: str, quantity: Decimal) -> None:
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("OCO client_order_id must be non-blank")
        qty = decimal(quantity, "oco quantity", positive=True)
        if client_order_id in self.members:
            if self.members[client_order_id] != qty:
                raise OrderStateError(
                    PaperReasonCode.INVARIANT_VIOLATION,
                    "OCO member quantity changed",
                )
            return
        if len(self.members) >= MAX_OCO_MEMBERS:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "OCO member limit exceeded",
            )
        self.members[client_order_id] = qty
        self.filled[client_order_id] = Decimal("0")

    @property
    def total_filled(self) -> Decimal:
        return sum(self.filled.values(), Decimal("0"))

    @property
    def remaining_position_quantity(self) -> Decimal:
        return max(Decimal("0"), self.position_quantity - self.total_filled)

    def record_fill(
        self,
        client_order_id: str,
        cumulative_quantity: Decimal,
    ) -> tuple[str, ...]:
        if client_order_id not in self.members:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                "unknown OCO member",
            )
        if client_order_id in self.canceled:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "fill received for canceled OCO member",
            )
        cumulative = decimal(cumulative_quantity, "cumulative_quantity")
        previous = self.filled[client_order_id]
        if cumulative < previous or cumulative > self.members[client_order_id]:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "invalid OCO cumulative fill",
            )
        if cumulative == previous:
            return ()
        self.filled[client_order_id] = cumulative
        if self.total_filled > self.position_quantity:
            self.filled[client_order_id] = previous
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "OCO over-close detected",
            )
        if self.remaining_position_quantity == 0:
            return tuple(
                member
                for member in self.members
                if member != client_order_id
                and member not in self.canceled
                and self.filled[member] < self.members[member]
            )
        return ()

    def required_adjustments(self) -> tuple[OcoAdjustment, ...]:
        remaining_position = self.remaining_position_quantity
        adjustments: list[OcoAdjustment] = []
        for member, member_quantity in self.members.items():
            if member in self.canceled:
                continue
            member_remaining = member_quantity - self.filled[member]
            if member_remaining <= 0:
                continue
            if remaining_position <= 0:
                adjustments.append(OcoAdjustment(member, OcoAdjustmentKind.CANCEL))
                continue
            safe_remaining = min(member_remaining, remaining_position)
            if safe_remaining < member_remaining:
                adjustments.append(
                    OcoAdjustment(
                        member,
                        OcoAdjustmentKind.REDUCE_REMAINING_TO,
                        safe_remaining,
                    )
                )
        return tuple(adjustments)

    def record_canceled(self, client_order_id: str) -> bool:
        if client_order_id not in self.members:
            raise OrderStateError(
                PaperReasonCode.ORDER_STATE_ERROR,
                "unknown OCO member",
            )
        if client_order_id in self.canceled:
            return False
        self.canceled.add(client_order_id)
        return True

    def snapshot_state(self) -> Mapping[str, object]:
        payload = self._state_payload()
        return {**payload, "state_hash": sha256_text(canonical_json(payload))}

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> "OcoGroup":
        expected_keys = {
            "schema_version",
            "group_id",
            "position_quantity",
            "members",
            "filled",
            "canceled",
            "state_hash",
        }
        if set(state) != expected_keys:
            raise ValueError("invalid OCO state fields")
        if state["schema_version"] != "1":
            raise ValueError("unsupported OCO state schema")
        group_id = state["group_id"]
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError("invalid OCO group_id")
        raw_members = state["members"]
        raw_filled = state["filled"]
        raw_canceled = state["canceled"]
        if not isinstance(raw_members, Mapping) or not isinstance(
            raw_filled,
            Mapping,
        ):
            raise ValueError("invalid OCO member state")
        if not isinstance(raw_canceled, list):
            raise ValueError("invalid OCO canceled state")
        if (
            len(raw_members) > MAX_OCO_MEMBERS
            or len(raw_filled) > MAX_OCO_MEMBERS
            or len(raw_canceled) > MAX_OCO_MEMBERS
        ):
            raise ValueError("OCO state exceeds member limit")
        if set(raw_members) != set(raw_filled):
            raise ValueError("OCO members and fills do not match")
        if any(
            not isinstance(client_order_id, str) or not client_order_id.strip()
            for client_order_id in raw_canceled
        ):
            raise ValueError("invalid canceled OCO member")
        if len(set(raw_canceled)) != len(raw_canceled):
            raise ValueError("duplicate canceled OCO member")

        state_hash = state["state_hash"]
        if not isinstance(state_hash, str):
            raise ValueError("invalid OCO state hash")
        payload = {key: state[key] for key in expected_keys if key != "state_hash"}
        if sha256_text(canonical_json(payload)) != state_hash:
            raise ValueError("OCO state hash mismatch")

        group = cls(
            group_id=group_id,
            position_quantity=decimal(
                state["position_quantity"],
                "position_quantity",
                positive=True,
            ),
        )
        for client_order_id, raw_quantity in raw_members.items():
            if not isinstance(client_order_id, str) or not client_order_id.strip():
                raise ValueError("invalid OCO member identifier")
            group.register(
                client_order_id,
                decimal(raw_quantity, "oco quantity", positive=True),
            )
            cumulative = decimal(
                raw_filled[client_order_id],
                "cumulative_quantity",
            )
            if cumulative < 0 or cumulative > group.members[client_order_id]:
                raise ValueError("invalid OCO persisted fill")
            group.filled[client_order_id] = cumulative
        if group.total_filled > group.position_quantity:
            raise ValueError("persisted OCO state over-closes position")
        for client_order_id in raw_canceled:
            if client_order_id not in group.members:
                raise ValueError("unknown canceled OCO member")
            group.canceled.add(client_order_id)
        return group

    def _state_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "group_id": self.group_id,
            "position_quantity": self.position_quantity,
            "members": dict(sorted(self.members.items())),
            "filled": dict(sorted(self.filled.items())),
            "canceled": sorted(self.canceled),
        }
