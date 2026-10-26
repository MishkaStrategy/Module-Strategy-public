from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from ._utils import canonical_json, decimal, sha256_text, utc
from .contracts import ExecutionResult
from .enums import PaperOrderState, PaperReasonCode
from .manifest import MODULE_VERSION
from .orchestration_state import ExecutionOrchestrationState
from .recovery_types import (
    EVENT_FINGERPRINT_SCHEMAS,
    EVENT_TIMESTAMP_SCHEMAS,
    MAX_STATE_RECORDS,
    ORCHESTRATION_STATE_SCHEMAS,
    SUPPORTED_STATE_SCHEMA_VERSIONS,
    RestoredExecutionState,
    boolean,
    hash_digest,
    integer,
    mapping,
    require_keys,
    sequence,
    string,
)
from .state_machine import OrderAggregate


def restore_execution_state(
    state: Mapping[str, object],
    *,
    expected_configuration_hash: str,
) -> RestoredExecutionState:
    root = mapping(state, "state")
    schema_version = string(root.get("schema_version"), "schema_version")
    if schema_version not in SUPPORTED_STATE_SCHEMA_VERSIONS:
        raise ValueError("unsupported paper execution state schema")

    required = {
        "schema_version",
        "module_version",
        "configuration_hash",
        "plans",
        "orders",
        "blocked_scopes",
        "event_sequence",
        "journal_attestation",
        "state_hash",
    }
    if schema_version in ORCHESTRATION_STATE_SCHEMAS:
        required.add("orchestration_state")
    if set(root) != required:
        raise ValueError("state contains missing or unknown fields")
    if string(root["module_version"], "module_version") != MODULE_VERSION:
        raise ValueError("paper execution state module version mismatch")
    if string(root["configuration_hash"], "configuration_hash") != expected_configuration_hash:
        raise ValueError("paper execution state configuration hash mismatch")
    state_hash = string(root["state_hash"], "state_hash")
    body = {key: value for key, value in root.items() if key != "state_hash"}
    if sha256_text(canonical_json(body)) != state_hash:
        raise ValueError("paper execution state hash mismatch")

    plan_rows = sequence(root["plans"], "plans")
    order_rows = sequence(root["orders"], "orders")
    blocked_rows = sequence(root["blocked_scopes"], "blocked_scopes")
    if max(len(plan_rows), len(order_rows), len(blocked_rows)) > MAX_STATE_RECORDS:
        raise ValueError("paper execution state exceeds record limit")

    plan_results: dict[str, ExecutionResult] = {}
    plan_hashes: dict[str, str] = {}
    plan_scopes: dict[str, tuple[str, str]] = {}
    for index, raw in enumerate(plan_rows):
        row = mapping(raw, f"plans[{index}]")
        require_keys(
            row,
            {
                "plan_id",
                "content_hash",
                "accepted",
                "terminal",
                "client_order_ids",
                "reason_code",
                "message",
                "portfolio_id",
                "instrument",
            },
            f"plans[{index}]",
        )
        plan_id = string(row.get("plan_id"), "plan_id")
        if plan_id in plan_results:
            raise ValueError(f"duplicate restored plan_id: {plan_id}")
        reason_raw = row.get("reason_code")
        reason = None if reason_raw is None else PaperReasonCode(string(reason_raw, "reason_code"))
        client_ids = tuple(
            string(value, "client_order_id")
            for value in sequence(
                row.get("client_order_ids"),
                "client_order_ids",
            )
        )
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("restored plan contains duplicate client order IDs")
        result = ExecutionResult(
            plan_id=plan_id,
            content_hash=hash_digest(
                row.get("content_hash"),
                "content_hash",
            ),
            accepted=boolean(row.get("accepted"), "accepted"),
            terminal=boolean(row.get("terminal"), "terminal"),
            client_order_ids=client_ids,
            reason_code=reason,
            message=string(
                row.get("message"),
                "message",
                allow_empty=True,
            ),
        )
        if result.accepted and result.reason_code is not None:
            raise ValueError("accepted restored plan cannot contain a reason code")
        if not result.accepted and (not result.terminal or result.reason_code is None):
            raise ValueError("rejected restored plan must be terminal with a reason code")
        portfolio_id = string(row.get("portfolio_id"), "portfolio_id")
        instrument = string(row.get("instrument"), "instrument")
        plan_results[plan_id] = result
        plan_hashes[plan_id] = result.content_hash
        plan_scopes[plan_id] = (portfolio_id, instrument)

    orders: dict[str, OrderAggregate] = {}
    max_sequence = 0
    for index, raw in enumerate(order_rows):
        row = mapping(raw, f"orders[{index}]")
        expected_order_fields = {
            "client_order_id",
            "core_order_id",
            "requested_quantity",
            "state",
            "filled_quantity",
            "average_fill_price",
            "fees_paid",
            "last_sequence",
            "processed_event_ids",
            "portfolio_id",
            "instrument",
            "intent_role",
        }
        if schema_version in EVENT_FINGERPRINT_SCHEMAS:
            expected_order_fields.add("processed_event_hashes")
        if schema_version in EVENT_TIMESTAMP_SCHEMAS:
            expected_order_fields.add("last_event_timestamp")
        require_keys(
            row,
            expected_order_fields,
            f"orders[{index}]",
        )
        client_id = string(
            row.get("client_order_id"),
            "client_order_id",
        )
        if client_id in orders:
            raise ValueError(f"duplicate restored client_order_id: {client_id}")
        processed_values = tuple(
            string(value, "processed_event_id")
            for value in sequence(
                row.get("processed_event_ids"),
                "processed_event_ids",
            )
        )
        if len(processed_values) != len(set(processed_values)):
            raise ValueError("restored order contains duplicate processed event IDs")
        processed_ids = set(processed_values)
        if schema_version in EVENT_FINGERPRINT_SCHEMAS:
            raw_hashes = mapping(
                row.get("processed_event_hashes"),
                "processed_event_hashes",
            )
            if set(raw_hashes) != processed_ids:
                raise ValueError("restored event IDs and fingerprints do not match")
            processed_hashes = {
                event_id: hash_digest(
                    raw_hashes[event_id],
                    f"processed_event_hashes[{event_id!r}]",
                )
                for event_id in sorted(processed_ids)
            }
        else:
            processed_hashes = {event_id: "" for event_id in processed_ids}
        last_event_timestamp = None
        if schema_version in EVENT_TIMESTAMP_SCHEMAS:
            raw_timestamp = row.get("last_event_timestamp")
            if raw_timestamp is not None:
                try:
                    parsed_timestamp = datetime.fromisoformat(
                        string(raw_timestamp, "last_event_timestamp")
                    )
                except ValueError as exc:
                    raise ValueError("last_event_timestamp must be ISO-8601") from exc
                last_event_timestamp = utc(
                    parsed_timestamp,
                    "last_event_timestamp",
                )
        aggregate = OrderAggregate(
            client_order_id=client_id,
            core_order_id=string(
                row.get("core_order_id"),
                "core_order_id",
            ),
            requested_quantity=decimal(
                row.get("requested_quantity"),
                "requested_quantity",
                positive=True,
            ),
            state=PaperOrderState(string(row.get("state"), "order state")),
            filled_quantity=decimal(
                row.get("filled_quantity"),
                "filled_quantity",
            ),
            average_fill_price=(
                None
                if row.get("average_fill_price") is None
                else decimal(
                    row.get("average_fill_price"),
                    "average_fill_price",
                    positive=True,
                )
            ),
            fees_paid=decimal(row.get("fees_paid"), "fees_paid"),
            last_sequence=integer(
                row.get("last_sequence"),
                "last_sequence",
            ),
            last_event_timestamp=last_event_timestamp,
            processed_event_ids=processed_ids,
            processed_event_hashes=processed_hashes,
            portfolio_id=string(
                row.get("portfolio_id"),
                "portfolio_id",
            ),
            instrument=string(row.get("instrument"), "instrument"),
            intent_role=string(row.get("intent_role"), "intent_role"),
        )
        if (
            aggregate.filled_quantity < 0
            or aggregate.filled_quantity > aggregate.requested_quantity
        ):
            raise ValueError("restored filled quantity violates order invariant")
        if aggregate.filled_quantity == 0 and aggregate.average_fill_price is not None:
            raise ValueError("unfilled restored order cannot have average fill price")
        if aggregate.filled_quantity > 0 and aggregate.average_fill_price is None:
            raise ValueError("filled restored order requires average fill price")
        if (
            aggregate.state is PaperOrderState.FILLED
            and aggregate.filled_quantity != aggregate.requested_quantity
        ):
            raise ValueError("filled restored order must have full cumulative quantity")
        if aggregate.state is PaperOrderState.PARTIALLY_FILLED and not (
            Decimal("0") < aggregate.filled_quantity < aggregate.requested_quantity
        ):
            raise ValueError("partially filled restored order has invalid quantity")
        if aggregate.fees_paid < 0:
            raise ValueError("restored fees cannot be negative")
        if aggregate.last_sequence < 0:
            raise ValueError("restored last_sequence cannot be negative")
        if schema_version in EVENT_TIMESTAMP_SCHEMAS:
            if aggregate.last_sequence == 0 and aggregate.last_event_timestamp is not None:
                raise ValueError("restored event timestamp requires a processed event")
            if aggregate.last_sequence > 0 and aggregate.last_event_timestamp is None:
                raise ValueError("restored processed event requires a timestamp")
        if aggregate.last_sequence < len(aggregate.processed_event_ids):
            raise ValueError("restored last_sequence precedes processed event count")
        orders[client_id] = aggregate
        max_sequence = max(max_sequence, aggregate.last_sequence)

    for result in plan_results.values():
        missing_order_ids = set(result.client_order_ids) - set(orders)
        if missing_order_ids:
            raise ValueError(
                f"restored plan references unknown orders: {sorted(missing_order_ids)}"
            )

    blocked_scopes: set[tuple[str, str]] = set()
    for index, raw in enumerate(blocked_rows):
        row = mapping(raw, f"blocked_scopes[{index}]")
        require_keys(
            row,
            {"portfolio_id", "instrument"},
            f"blocked_scopes[{index}]",
        )
        scope = (
            string(row.get("portfolio_id"), "portfolio_id"),
            string(row.get("instrument"), "instrument"),
        )
        if scope in blocked_scopes:
            raise ValueError("duplicate blocked execution scope")
        blocked_scopes.add(scope)

    event_sequence = integer(root["event_sequence"], "event_sequence")
    if event_sequence < 0:
        raise ValueError("event_sequence cannot be negative")
    if event_sequence < max_sequence:
        raise ValueError("event_sequence precedes restored order state")
    attestation = mapping(
        root["journal_attestation"],
        "journal_attestation",
    )

    orchestration_state = ExecutionOrchestrationState()
    if schema_version in ORCHESTRATION_STATE_SCHEMAS:
        orchestration_row = mapping(
            root["orchestration_state"],
            "orchestration_state",
        )
        raw_oco = mapping(
            orchestration_row.get("oco_groups"),
            "orchestration_state.oco_groups",
        )
        raw_protection = mapping(
            orchestration_row.get("protection_by_entry"),
            "orchestration_state.protection_by_entry",
        )
        if max(len(raw_oco), len(raw_protection)) > MAX_STATE_RECORDS:
            raise ValueError("orchestration state exceeds record limit")
        orchestration_state = ExecutionOrchestrationState.from_state(orchestration_row)

    return RestoredExecutionState(
        plan_results=plan_results,
        plan_hashes=plan_hashes,
        plan_scopes=plan_scopes,
        orders=orders,
        blocked_scopes=blocked_scopes,
        event_sequence=event_sequence,
        journal_attestation=dict(attestation),
        orchestration_state=orchestration_state,
        source_schema_version=schema_version,
    )
