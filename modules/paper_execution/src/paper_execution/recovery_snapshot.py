from __future__ import annotations

from typing import Mapping

from ._utils import canonical_json, sha256_text
from .contracts import ExecutionResult
from .manifest import MODULE_VERSION
from .orchestration_state import ExecutionOrchestrationState
from .recovery_types import STATE_SCHEMA_VERSION
from .state_machine import OrderAggregate


def snapshot_execution_state(
    *,
    configuration_hash: str,
    plan_results: Mapping[str, ExecutionResult],
    plan_scopes: Mapping[str, tuple[str, str]],
    orders: Mapping[str, OrderAggregate],
    blocked_scopes: set[tuple[str, str]],
    event_sequence: int,
    journal_attestation: Mapping[str, object],
    orchestration_state: ExecutionOrchestrationState | None = None,
) -> Mapping[str, object]:
    plans: list[dict[str, object]] = []
    for plan_id in sorted(plan_results):
        result = plan_results[plan_id]
        scope = plan_scopes.get(plan_id)
        if scope is None:
            raise ValueError(f"missing scope for plan {plan_id}")
        plans.append(
            {
                "plan_id": result.plan_id,
                "content_hash": result.content_hash,
                "accepted": result.accepted,
                "terminal": result.terminal,
                "client_order_ids": list(result.client_order_ids),
                "reason_code": (
                    result.reason_code.value if result.reason_code is not None else None
                ),
                "message": result.message,
                "portfolio_id": scope[0],
                "instrument": scope[1],
            }
        )
    order_rows: list[dict[str, object]] = []
    for client_id in sorted(orders):
        aggregate = orders[client_id]
        if set(aggregate.processed_event_hashes) != set(aggregate.processed_event_ids):
            raise ValueError("order event IDs and fingerprints do not match")
        order_rows.append(
            {
                "client_order_id": aggregate.client_order_id,
                "core_order_id": aggregate.core_order_id,
                "requested_quantity": str(aggregate.requested_quantity),
                "state": aggregate.state.value,
                "filled_quantity": str(aggregate.filled_quantity),
                "average_fill_price": (
                    str(aggregate.average_fill_price)
                    if aggregate.average_fill_price is not None
                    else None
                ),
                "fees_paid": str(aggregate.fees_paid),
                "last_sequence": aggregate.last_sequence,
                "last_event_timestamp": (
                    aggregate.last_event_timestamp.isoformat()
                    if aggregate.last_event_timestamp is not None
                    else None
                ),
                "processed_event_ids": sorted(aggregate.processed_event_ids),
                "processed_event_hashes": dict(sorted(aggregate.processed_event_hashes.items())),
                "portfolio_id": aggregate.portfolio_id,
                "instrument": aggregate.instrument,
                "intent_role": aggregate.intent_role,
            }
        )
    orchestration = orchestration_state or ExecutionOrchestrationState()
    body: dict[str, object] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "configuration_hash": configuration_hash,
        "plans": plans,
        "orders": order_rows,
        "blocked_scopes": [
            {"portfolio_id": portfolio, "instrument": instrument}
            for portfolio, instrument in sorted(blocked_scopes)
        ],
        "event_sequence": event_sequence,
        "journal_attestation": dict(journal_attestation),
        "orchestration_state": orchestration.snapshot_state(),
    }
    return {
        **body,
        "state_hash": sha256_text(canonical_json(body)),
    }
