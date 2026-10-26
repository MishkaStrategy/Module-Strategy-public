from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from .contracts import ExecutableTradePlan
from .enums import IntentRole, PaperReasonCode
from .exceptions import PlanValidationError
from .oco import OcoGroup
from .state_machine import OrderAggregate


class ClientOrderIdProvider(Protocol):
    def create(self, *, plan_id: str, intent_id: str, role: str) -> str: ...


def build_submitted_oco_groups(
    *,
    plan: ExecutableTradePlan,
    submitted_client_ids: tuple[str, ...],
    orders: Mapping[str, OrderAggregate],
    id_factory: ClientOrderIdProvider,
) -> tuple[OcoGroup, ...]:
    """Build fail-closed OCO groups only from orders accepted by Core."""

    submitted = set(submitted_client_ids)
    rows_by_group: dict[str, list[tuple[str, IntentRole, Decimal]]] = {}
    for intent in plan.intents:
        group_id = intent.oco_group_id
        if group_id is None:
            continue
        if intent.role not in {IntentRole.STOP, IntentRole.TARGET}:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "synthetic OCO supports only Stop/Target members",
            )
        client_id = id_factory.create(
            plan_id=plan.plan_id,
            intent_id=intent.intent_id,
            role=intent.role.value,
        )
        if client_id not in submitted:
            continue
        aggregate = orders.get(client_id)
        if aggregate is None:
            raise PlanValidationError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "submitted OCO member is missing from projection",
            )
        rows_by_group.setdefault(group_id, []).append(
            (client_id, intent.role, aggregate.requested_quantity)
        )

    groups: list[OcoGroup] = []
    for group_id, rows in sorted(rows_by_group.items()):
        if len(rows) < 2:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "synthetic OCO group requires at least two submitted members",
            )
        stop_quantities = [quantity for _, role, quantity in rows if role is IntentRole.STOP]
        target_quantity = sum(
            (quantity for _, role, quantity in rows if role is IntentRole.TARGET),
            Decimal("0"),
        )
        stop_quantity = max(stop_quantities, default=Decimal("0"))
        if stop_quantity <= 0 or target_quantity <= 0:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "synthetic OCO requires Stop and Target coverage",
            )
        if stop_quantity != target_quantity:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "synthetic OCO Stop and Target coverage differ",
            )
        group = OcoGroup(group_id, stop_quantity)
        for client_id, _, quantity in rows:
            group.register(client_id, quantity)
        groups.append(group)
    return tuple(groups)


def find_oco_group(
    groups: Mapping[str, OcoGroup],
    client_order_id: str,
) -> OcoGroup | None:
    matches = tuple(group for group in groups.values() if client_order_id in group.members)
    if len(matches) > 1:
        raise PlanValidationError(
            PaperReasonCode.INVARIANT_VIOLATION,
            "client order belongs to multiple OCO groups",
        )
    return matches[0] if matches else None
