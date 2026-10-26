from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, NoReturn, overload

from .decimal_math import DecimalDomainError, exact_sum
from .canonical import (
    canonical_json,
    canonical_value,
    decimal_text,
    deep_freeze,
    has_control_characters,
    sha256_hex,
    stable_id,
    utc_text,
)
from .enums import PlanVerdict, ReasonCode, TradeAction, TradeSide
from .errors import RouterSerializationError
from .models import ExecutableTradePlan, RoutingResult

DEFAULT_MAX_PAYLOAD_BYTES: Final = 4 * 1024 * 1024
MAX_CONFIGURED_PAYLOAD_BYTES: Final = 64 * 1024 * 1024
SUPPORTED_SCHEMA_VERSION: Final = "1"
MAX_SERIALIZED_DOMAIN_TEXT_LENGTH: Final = 4_096
MAX_SERIALIZED_INTEGER: Final = 9_223_372_036_854_775_807

_PLAN_FIELDS: Final = frozenset(
    {
        "schema_version",
        "module_version",
        "portfolio_id",
        "strategy_id",
        "setup_id",
        "symbol",
        "market",
        "action",
        "side",
        "created_at",
        "valid_from",
        "expires_at",
        "expected_portfolio_version",
        "expected_position_version",
        "expected_order_state_version",
        "expected_risk_reservation_version",
        "selected_candidate_id",
        "position_intent",
        "entry_intent",
        "protection_intent",
        "sizing_intent",
        "scaling_decision_id",
        "cancellation_intents",
        "sequencing_rules",
        "preconditions",
        "reason_codes",
        "input_hashes",
        "configuration_hash",
        "warnings",
        "rejected_candidates",
        "audit_trace",
        "plan_id",
        "content_hash",
        "audit_hash",
    }
)
_RESULT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "verdict",
        "created_at",
        "portfolio_id",
        "configuration_hash",
        "plan",
        "reason_codes",
        "rejected_candidates",
        "audit_trace",
        "warnings",
        "result_id",
    }
)
_POSITION_INTENT_FIELDS: Final = frozenset(
    {"position_id", "expected_position_version", "current_side", "current_quantity"}
)
_ENTRY_INTENT_FIELDS: Final = frozenset(
    {
        "order_type",
        "quantity",
        "reference_entry_price",
        "price",
        "trigger_price",
        "limit_price",
        "post_only",
        "ttl_bars",
        "reduce_only",
    }
)
_PROTECTION_INTENT_FIELDS: Final = frozenset(
    {
        "stop_loss",
        "targets",
        "max_holding_seconds",
        "no_progress_rule",
        "invalidation_rule",
    }
)
_TARGET_INTENT_FIELDS: Final = frozenset({"price", "allocation", "target_type"})
_SIZING_INTENT_FIELDS: Final = frozenset(
    {
        "quantity",
        "notional",
        "risk_amount",
        "risk_fraction",
        "leverage",
        "binding_constraints",
    }
)
_CANCELLATION_INTENT_FIELDS: Final = frozenset(
    {"order_intent_id", "expected_order_version", "reason"}
)
_PRECONDITION_FIELDS: Final = frozenset({"kind", "expected"})
_REJECTED_CANDIDATE_FIELDS: Final = frozenset(
    {"candidate_id", "setup_id", "reason_codes", "detail"}
)
_AUDIT_EVENT_FIELDS: Final = frozenset(
    {"sequence", "stage", "outcome", "reason_code", "candidate_id", "detail"}
)
_PLAN_IDENTIFIER_FIELDS: Final = frozenset({"plan_id", "content_hash", "audit_hash"})
_PLAN_AUDIT_FIELDS: Final = frozenset({"warnings", "rejected_candidates", "audit_trace"})
_EXECUTABLE_ACTIONS: Final = frozenset(
    action.value
    for action in TradeAction
    if action not in {TradeAction.NO_ACTION, TradeAction.REJECTED}
)
_POSITION_ACTIONS: Final = frozenset(
    {
        TradeAction.ADD_TO_POSITION.value,
        TradeAction.REDUCE_POSITION.value,
        TradeAction.CLOSE_POSITION.value,
        TradeAction.REVERSE_POSITION.value,
    }
)
_RISK_REDUCING_ACTIONS: Final = frozenset(
    {TradeAction.REDUCE_POSITION.value, TradeAction.CLOSE_POSITION.value}
)
_SIDE_VALUES: Final = frozenset(side.value for side in TradeSide)
_REASON_VALUES: Final = frozenset(reason.value for reason in ReasonCode)
_VERDICT_VALUES: Final = frozenset(verdict.value for verdict in PlanVerdict)
_ORDER_TYPES: Final = frozenset({"market", "limit", "stop_market", "stop_limit"})


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _require_text(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    max_length: int = MAX_SERIALIZED_DOMAIN_TEXT_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise RouterSerializationError(f"{field} must be a string")
    if not allow_empty and not value:
        raise RouterSerializationError(f"{field} must be non-empty")
    if len(value) > max_length:
        raise RouterSerializationError(f"{field} exceeds maximum length {max_length}")
    if unicodedata.normalize("NFC", value) != value:
        raise RouterSerializationError(f"{field} must use canonical NFC text")
    if has_control_characters(value):
        raise RouterSerializationError(f"{field} cannot contain control characters")
    return value


def _require_optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RouterSerializationError(f"{field} must be boolean")
    return value


@overload
def _require_positive_int(
    value: Any,
    field: str,
    *,
    allow_none: Literal[False] = False,
) -> int: ...


@overload
def _require_positive_int(
    value: Any,
    field: str,
    *,
    allow_none: Literal[True],
) -> int | None: ...


def _require_positive_int(
    value: Any,
    field: str,
    *,
    allow_none: bool = False,
) -> int | None:
    if value is None and allow_none:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SERIALIZED_INTEGER
    ):
        suffix = " or null" if allow_none else ""
        raise RouterSerializationError(
            f"{field} must be a positive integer in [1, {MAX_SERIALIZED_INTEGER}]{suffix}"
        )
    return value


@overload
def _require_decimal_text(
    value: Any,
    field: str,
    *,
    allow_none: Literal[False] = False,
    positive: bool = False,
) -> Decimal: ...


@overload
def _require_decimal_text(
    value: Any,
    field: str,
    *,
    allow_none: Literal[True],
    positive: bool = False,
) -> Decimal | None: ...


def _require_decimal_text(
    value: Any,
    field: str,
    *,
    allow_none: bool = False,
    positive: bool = False,
) -> Decimal | None:
    if value is None and allow_none:
        return None
    text = _require_text(value, field, max_length=256)
    try:
        decimal = Decimal(text)
    except InvalidOperation as exc:
        raise RouterSerializationError(f"{field} must be a canonical decimal string") from exc
    if not decimal.is_finite() or decimal_text(decimal) != text:
        raise RouterSerializationError(f"{field} must be a canonical finite decimal string")
    if positive and decimal <= 0:
        raise RouterSerializationError(f"{field} must be positive")
    if not positive and decimal < 0:
        raise RouterSerializationError(f"{field} must be non-negative")
    return decimal


def _require_datetime_text(value: Any, field: str) -> datetime:
    text = _require_text(value, field, max_length=64)
    if not text.endswith("Z"):
        raise RouterSerializationError(f"{field} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise RouterSerializationError(f"{field} must be canonical UTC text") from exc
    parsed = parsed.astimezone(UTC)
    if utc_text(parsed) != text:
        raise RouterSerializationError(f"{field} must be canonical UTC text")
    return parsed


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RouterSerializationError(f"{field} must be a JSON object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise RouterSerializationError(f"{field} must be a JSON array")
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    subject: str,
) -> None:
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise RouterSerializationError(f"invalid {subject} schema: {'; '.join(details)}")


def _require_digest(value: Any, field: str) -> str:
    text = _require_text(value, field, max_length=64)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise RouterSerializationError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _require_enum(value: Any, field: str, allowed: frozenset[str]) -> str:
    text = _require_text(value, field)
    if text not in allowed:
        raise RouterSerializationError(f"{field} contains an unsupported value: {text!r}")
    return text


def _require_string_list(
    value: Any,
    field: str,
    *,
    non_empty: bool = False,
    allowed: frozenset[str] | None = None,
    sorted_unique: bool = False,
    allow_empty_members: bool = False,
) -> list[str]:
    rows = _require_list(value, field)
    result = [
        _require_text(
            item,
            f"{field}[{index}]",
            allow_empty=allow_empty_members,
        )
        for index, item in enumerate(rows)
    ]
    if non_empty and not result:
        raise RouterSerializationError(f"{field} cannot be empty")
    if allowed is not None:
        invalid = sorted(set(result) - allowed)
        if invalid:
            raise RouterSerializationError(f"{field} contains unsupported values: {invalid}")
    if sorted_unique and result != sorted(set(result)):
        raise RouterSerializationError(f"{field} must be sorted and unique")
    return result


def _validate_position_intent(value: Any) -> None:
    row = _require_mapping(value, "position_intent")
    _require_exact_fields(row, _POSITION_INTENT_FIELDS, "position_intent")
    _require_text(row["position_id"], "position_intent.position_id")
    _require_text(
        row["expected_position_version"],
        "position_intent.expected_position_version",
    )
    _require_enum(row["current_side"], "position_intent.current_side", _SIDE_VALUES)
    _require_decimal_text(
        row["current_quantity"],
        "position_intent.current_quantity",
        positive=True,
    )


def _validate_entry_intent(value: Any) -> tuple[Decimal, bool]:
    row = _require_mapping(value, "entry_intent")
    _require_exact_fields(row, _ENTRY_INTENT_FIELDS, "entry_intent")
    order_type = _require_enum(row["order_type"], "entry_intent.order_type", _ORDER_TYPES)
    quantity = _require_decimal_text(row["quantity"], "entry_intent.quantity", positive=True)
    _require_decimal_text(
        row["reference_entry_price"],
        "entry_intent.reference_entry_price",
        positive=True,
    )
    price = _require_decimal_text(
        row["price"], "entry_intent.price", allow_none=True, positive=True
    )
    trigger = _require_decimal_text(
        row["trigger_price"],
        "entry_intent.trigger_price",
        allow_none=True,
        positive=True,
    )
    limit = _require_decimal_text(
        row["limit_price"],
        "entry_intent.limit_price",
        allow_none=True,
        positive=True,
    )
    post_only = _require_bool(row["post_only"], "entry_intent.post_only")
    reduce_only = _require_bool(row["reduce_only"], "entry_intent.reduce_only")
    _require_positive_int(row["ttl_bars"], "entry_intent.ttl_bars", allow_none=True)

    if order_type == "market":
        if any(member is not None for member in (price, trigger, limit)) or post_only:
            raise RouterSerializationError("market entry_intent has invalid price geometry")
    elif order_type == "limit":
        if price is None and limit is None:
            raise RouterSerializationError("limit entry_intent requires a price")
    elif order_type == "stop_market":
        if trigger is None:
            raise RouterSerializationError("stop_market entry_intent requires trigger_price")
    elif trigger is None or limit is None:
        raise RouterSerializationError(
            "stop_limit entry_intent requires trigger_price and limit_price"
        )
    return quantity, reduce_only


def _validate_target(value: Any, index: int) -> Decimal:
    row = _require_mapping(value, f"protection_intent.targets[{index}]")
    _require_exact_fields(row, _TARGET_INTENT_FIELDS, "target_intent")
    _require_decimal_text(row["price"], f"protection_intent.targets[{index}].price", positive=True)
    allocation = _require_decimal_text(
        row["allocation"],
        f"protection_intent.targets[{index}].allocation",
        positive=True,
    )
    _require_text(row["target_type"], f"protection_intent.targets[{index}].target_type")
    if allocation > 1:
        raise RouterSerializationError("target allocation must be in (0, 1]")
    return allocation


def _validate_protection_intent(value: Any) -> None:
    row = _require_mapping(value, "protection_intent")
    _require_exact_fields(row, _PROTECTION_INTENT_FIELDS, "protection_intent")
    _require_decimal_text(row["stop_loss"], "protection_intent.stop_loss", positive=True)
    targets = _require_list(row["targets"], "protection_intent.targets")
    if not targets:
        raise RouterSerializationError("protection_intent.targets cannot be empty")
    try:
        total = exact_sum(
            (_validate_target(target, index) for index, target in enumerate(targets)),
            Decimal("0"),
        )
    except DecimalDomainError as exc:
        raise RouterSerializationError(str(exc)) from exc
    if total > 1:
        raise RouterSerializationError("target allocations exceed 1")
    _require_positive_int(
        row["max_holding_seconds"],
        "protection_intent.max_holding_seconds",
        allow_none=True,
    )
    _require_mapping(row["no_progress_rule"], "protection_intent.no_progress_rule")
    _require_mapping(row["invalidation_rule"], "protection_intent.invalidation_rule")


def _validate_sizing_intent(
    value: Any,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    row = _require_mapping(value, "sizing_intent")
    _require_exact_fields(row, _SIZING_INTENT_FIELDS, "sizing_intent")
    quantity = _require_decimal_text(row["quantity"], "sizing_intent.quantity", positive=True)
    values: dict[str, Decimal] = {}
    for field in ("notional", "risk_amount", "risk_fraction", "leverage"):
        decimal = _require_decimal_text(row[field], f"sizing_intent.{field}")
        values[field] = decimal
        if field == "risk_fraction" and decimal > 1:
            raise RouterSerializationError("sizing_intent.risk_fraction cannot exceed 1")
    _require_string_list(
        row["binding_constraints"],
        "sizing_intent.binding_constraints",
        sorted_unique=True,
    )
    return (
        quantity,
        values["notional"],
        values["risk_amount"],
        values["risk_fraction"],
        values["leverage"],
    )


def _validate_cancellation_intents(value: Any) -> None:
    rows = _require_list(value, "cancellation_intents")
    identifiers: list[str] = []
    for index, value_row in enumerate(rows):
        row = _require_mapping(value_row, f"cancellation_intents[{index}]")
        _require_exact_fields(row, _CANCELLATION_INTENT_FIELDS, "cancellation_intent")
        identifiers.append(
            _require_text(
                row["order_intent_id"],
                f"cancellation_intents[{index}].order_intent_id",
            )
        )
        _require_text(
            row["expected_order_version"],
            f"cancellation_intents[{index}].expected_order_version",
        )
        _require_text(row["reason"], f"cancellation_intents[{index}].reason")
    if identifiers != sorted(identifiers) or len(identifiers) != len(set(identifiers)):
        raise RouterSerializationError(
            "cancellation_intents must be sorted and unique by order_intent_id"
        )


def _validate_preconditions(value: Any) -> dict[str, str]:
    rows = _require_list(value, "preconditions")
    result: dict[str, str] = {}
    for index, value_row in enumerate(rows):
        row = _require_mapping(value_row, f"preconditions[{index}]")
        _require_exact_fields(row, _PRECONDITION_FIELDS, "execution_precondition")
        kind = _require_text(row["kind"], f"preconditions[{index}].kind")
        expected = _require_text(row["expected"], f"preconditions[{index}].expected")
        if kind in result:
            raise RouterSerializationError(f"duplicate execution precondition kind: {kind}")
        result[kind] = expected
    return result


def _validate_rejected_candidates(value: Any, field: str) -> None:
    rows = _require_list(value, field)
    identifiers: list[str] = []
    for index, value_row in enumerate(rows):
        row = _require_mapping(value_row, f"{field}[{index}]")
        _require_exact_fields(row, _REJECTED_CANDIDATE_FIELDS, "rejected_candidate")
        identifiers.append(_require_text(row["candidate_id"], f"{field}[{index}].candidate_id"))
        _require_text(row["setup_id"], f"{field}[{index}].setup_id")
        _require_string_list(
            row["reason_codes"],
            f"{field}[{index}].reason_codes",
            non_empty=True,
            allowed=_REASON_VALUES,
            sorted_unique=True,
        )
        _require_text(
            row["detail"],
            f"{field}[{index}].detail",
            allow_empty=True,
        )
    if identifiers != sorted(identifiers) or len(identifiers) != len(set(identifiers)):
        raise RouterSerializationError(f"{field} must be sorted and unique by candidate_id")


def _validate_audit_trace(value: Any, field: str) -> None:
    rows = _require_list(value, field)
    sequences: list[int] = []
    for index, value_row in enumerate(rows):
        row = _require_mapping(value_row, f"{field}[{index}]")
        _require_exact_fields(row, _AUDIT_EVENT_FIELDS, "audit_event")
        sequence = _require_positive_int(row["sequence"], f"{field}[{index}].sequence")
        sequences.append(sequence)
        _require_text(row["stage"], f"{field}[{index}].stage")
        _require_text(row["outcome"], f"{field}[{index}].outcome")
        if row["reason_code"] is not None:
            _require_enum(
                row["reason_code"],
                f"{field}[{index}].reason_code",
                _REASON_VALUES,
            )
        _require_optional_text(row["candidate_id"], f"{field}[{index}].candidate_id")
        _require_mapping(row["detail"], f"{field}[{index}].detail")
    if sequences != list(range(1, len(sequences) + 1)):
        raise RouterSerializationError(f"{field} sequence must be contiguous and start at 1")


def _validate_plan_mapping(value: dict[str, Any]) -> None:
    _require_exact_fields(value, _PLAN_FIELDS, "ExecutableTradePlan")
    if value["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise RouterSerializationError("unsupported ExecutableTradePlan schema_version")

    for field in (
        "module_version",
        "portfolio_id",
        "strategy_id",
        "setup_id",
        "symbol",
        "market",
        "expected_portfolio_version",
        "expected_position_version",
        "expected_order_state_version",
        "expected_risk_reservation_version",
        "selected_candidate_id",
    ):
        _require_text(value[field], field)
    action = _require_enum(value["action"], "action", _EXECUTABLE_ACTIONS)
    _require_enum(value["side"], "side", _SIDE_VALUES)
    created_at = _require_datetime_text(value["created_at"], "created_at")
    valid_from = _require_datetime_text(value["valid_from"], "valid_from")
    expires_at = _require_datetime_text(value["expires_at"], "expires_at")
    if valid_from < created_at or expires_at <= valid_from:
        raise RouterSerializationError("invalid plan validity interval")

    _require_digest(value["configuration_hash"], "configuration_hash")
    content_hash = _require_digest(value["content_hash"], "content_hash")
    audit_hash = _require_digest(value["audit_hash"], "audit_hash")
    plan_id = _require_text(value["plan_id"], "plan_id")
    if not plan_id.startswith("trade-plan-"):
        raise RouterSerializationError("plan_id has invalid prefix")

    position_intent = value["position_intent"]
    if position_intent is not None:
        _validate_position_intent(position_intent)
    if action in _POSITION_ACTIONS and position_intent is None:
        raise RouterSerializationError("position-changing plan requires position_intent")
    if action not in _POSITION_ACTIONS and position_intent is not None:
        raise RouterSerializationError("new-position plan cannot carry position_intent")

    entry_quantity, reduce_only = _validate_entry_intent(value["entry_intent"])
    protection_intent = value["protection_intent"]
    if action in _RISK_REDUCING_ACTIONS:
        if protection_intent is not None or not reduce_only:
            raise RouterSerializationError(
                "risk-reducing plan requires reduce_only and no protection_intent"
            )
    else:
        if protection_intent is None or reduce_only:
            raise RouterSerializationError(
                "risk-increasing plan requires protection and non-reduce-only entry"
            )
        _validate_protection_intent(protection_intent)

    (
        sizing_quantity,
        sizing_notional,
        sizing_risk_amount,
        sizing_risk_fraction,
        sizing_leverage,
    ) = _validate_sizing_intent(value["sizing_intent"])
    if entry_quantity != sizing_quantity:
        raise RouterSerializationError("entry and sizing quantities must match")
    if action not in _RISK_REDUCING_ACTIONS:
        for field, member in (
            ("notional", sizing_notional),
            ("risk_amount", sizing_risk_amount),
            ("risk_fraction", sizing_risk_fraction),
            ("leverage", sizing_leverage),
        ):
            if member <= 0:
                raise RouterSerializationError(
                    f"risk-increasing plan requires positive sizing_intent.{field}"
                )
    _require_optional_text(value["scaling_decision_id"], "scaling_decision_id")
    _validate_cancellation_intents(value["cancellation_intents"])
    _require_string_list(value["sequencing_rules"], "sequencing_rules")
    if len(value["sequencing_rules"]) != len(set(value["sequencing_rules"])):
        raise RouterSerializationError("sequencing_rules cannot contain duplicates")
    preconditions = _validate_preconditions(value["preconditions"])
    required_preconditions = {
        "portfolio_version": value["expected_portfolio_version"],
        "position_state_version": value["expected_position_version"],
        "order_state_version": value["expected_order_state_version"],
        "risk_reservation_version": value["expected_risk_reservation_version"],
        "configuration_hash": value["configuration_hash"],
    }
    missing_preconditions = sorted(
        (set(required_preconditions) | {"portfolio_content_hash"}) - set(preconditions)
    )
    if missing_preconditions:
        raise RouterSerializationError(
            "missing execution preconditions: " + ", ".join(missing_preconditions)
        )
    for kind, expected in required_preconditions.items():
        if preconditions[kind] != expected:
            raise RouterSerializationError(
                f"execution precondition {kind} conflicts with plan state"
            )
    portfolio_content_hash = _require_digest(
        preconditions["portfolio_content_hash"],
        "portfolio_content_hash",
    )
    _require_string_list(
        value["reason_codes"],
        "reason_codes",
        non_empty=True,
        allowed=_REASON_VALUES,
        sorted_unique=True,
    )
    input_hashes = _require_string_list(
        value["input_hashes"],
        "input_hashes",
        non_empty=True,
        sorted_unique=True,
    )
    for index, digest in enumerate(input_hashes):
        _require_digest(digest, f"input_hashes[{index}]")
    if portfolio_content_hash not in input_hashes:
        raise RouterSerializationError(
            "portfolio_content_hash precondition must be present in input_hashes"
        )
    _require_string_list(value["warnings"], "warnings", sorted_unique=True)
    _validate_rejected_candidates(value["rejected_candidates"], "rejected_candidates")
    _validate_audit_trace(value["audit_trace"], "audit_trace")

    semantic = {
        key: member
        for key, member in value.items()
        if key not in _PLAN_IDENTIFIER_FIELDS and key not in _PLAN_AUDIT_FIELDS
    }
    audit = {key: value[key] for key in _PLAN_AUDIT_FIELDS}
    if content_hash != sha256_hex(semantic):
        raise RouterSerializationError("content_hash does not match canonical plan content")
    if plan_id != stable_id("trade-plan", semantic):
        raise RouterSerializationError("plan_id does not match canonical plan content")
    if audit_hash != sha256_hex(audit):
        raise RouterSerializationError("audit_hash does not match canonical audit content")


def _validate_result_mapping(value: dict[str, Any]) -> None:
    _require_exact_fields(value, _RESULT_FIELDS, "RoutingResult")
    if value["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise RouterSerializationError("unsupported RoutingResult schema_version")

    verdict = _require_enum(value["verdict"], "verdict", _VERDICT_VALUES)
    _require_datetime_text(value["created_at"], "created_at")
    _require_text(value["portfolio_id"], "portfolio_id")
    configuration_hash = _require_digest(value["configuration_hash"], "configuration_hash")
    result_id = _require_text(value["result_id"], "result_id")
    if not result_id.startswith("routing-"):
        raise RouterSerializationError("result_id has invalid prefix")
    reason_codes = _require_string_list(
        value["reason_codes"],
        "reason_codes",
        non_empty=True,
        allowed=_REASON_VALUES,
        sorted_unique=True,
    )
    _validate_rejected_candidates(value["rejected_candidates"], "rejected_candidates")
    _validate_audit_trace(value["audit_trace"], "audit_trace")
    _require_string_list(value["warnings"], "warnings", sorted_unique=True)

    plan = value["plan"]
    if verdict == PlanVerdict.ACCEPTED.value:
        plan_mapping = _require_mapping(plan, "plan")
        _validate_plan_mapping(plan_mapping)
        plan_identity: dict[str, str] | None = {
            "plan_id": _require_text(plan_mapping["plan_id"], "plan.plan_id"),
            "content_hash": _require_digest(plan_mapping["content_hash"], "plan.content_hash"),
            "audit_hash": _require_digest(plan_mapping["audit_hash"], "plan.audit_hash"),
        }
        rejection_hash: str | None = None
    else:
        if plan is not None:
            raise RouterSerializationError("rejected RoutingResult cannot contain a plan")
        plan_identity = None
        rejection_hash = sha256_hex(
            {
                "reason_codes": reason_codes,
                "rejected_candidates": value["rejected_candidates"],
                "audit_trace": value["audit_trace"],
                "warnings": value["warnings"],
            }
        )

    identity = {
        "schema_version": value["schema_version"],
        "verdict": verdict,
        "created_at": value["created_at"],
        "portfolio_id": value["portfolio_id"],
        "configuration_hash": configuration_hash,
        "plan_identity": plan_identity,
        "rejection_hash": rejection_hash,
        "reason_codes": reason_codes,
    }
    if result_id != stable_id("routing", identity):
        raise RouterSerializationError("result_id does not match canonical result content")


@dataclass(frozen=True, slots=True)
class TradePlanSerializer:
    """Safe canonical JSON boundary for Router plans and routing results."""

    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_payload_bytes, bool)
            or not isinstance(self.max_payload_bytes, int)
            or not 1 <= self.max_payload_bytes <= MAX_CONFIGURED_PAYLOAD_BYTES
        ):
            raise ValueError(
                f"max_payload_bytes must be an integer in [1, {MAX_CONFIGURED_PAYLOAD_BYTES}]"
            )

    def dumps_plan(self, plan: ExecutableTradePlan) -> str:
        if not isinstance(plan, ExecutableTradePlan):
            raise RouterSerializationError("plan must be ExecutableTradePlan")
        return self._bounded_dump(plan.to_mapping())

    def dumps_result(self, result: RoutingResult) -> str:
        if not isinstance(result, RoutingResult):
            raise RouterSerializationError("result must be RoutingResult")
        return self._bounded_dump(result.to_mapping())

    def loads_plan_mapping(self, payload: str | bytes) -> Mapping[str, Any]:
        value = self._load_mapping(payload)
        _validate_plan_mapping(value)
        return self._freeze(value)

    def loads_result_mapping(self, payload: str | bytes) -> Mapping[str, Any]:
        value = self._load_mapping(payload)
        _validate_result_mapping(value)
        return self._freeze(value)

    def _bounded_dump(self, value: Mapping[str, Any]) -> str:
        try:
            payload = canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise RouterSerializationError(f"cannot serialize canonical payload: {exc}") from exc
        if len(payload.encode("utf-8")) > self.max_payload_bytes:
            raise RouterSerializationError(
                f"serialized payload exceeds {self.max_payload_bytes} bytes"
            )
        return payload

    def _load_mapping(self, payload: str | bytes) -> dict[str, Any]:
        text = self._decode(payload)
        try:
            value = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
            raise RouterSerializationError(f"invalid JSON payload: {exc}") from exc
        if not isinstance(value, dict):
            raise RouterSerializationError("serialized payload root must be a JSON object")
        try:
            canonical = canonical_value(value)
        except (TypeError, ValueError) as exc:
            raise RouterSerializationError(f"invalid canonical payload: {exc}") from exc
        if not isinstance(canonical, dict):
            raise RouterSerializationError(
                "canonical serialized payload root must be a JSON object"
            )
        return canonical

    def _decode(self, payload: str | bytes) -> str:
        if not isinstance(payload, (str, bytes)):
            raise RouterSerializationError("payload must be str or bytes")
        if isinstance(payload, bytes):
            if len(payload) > self.max_payload_bytes:
                raise RouterSerializationError(
                    f"serialized payload exceeds {self.max_payload_bytes} bytes"
                )
            try:
                return payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise RouterSerializationError("payload must be valid UTF-8") from exc
        try:
            encoded_length = len(payload.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise RouterSerializationError("payload contains invalid Unicode") from exc
        if encoded_length > self.max_payload_bytes:
            raise RouterSerializationError(
                f"serialized payload exceeds {self.max_payload_bytes} bytes"
            )
        return payload

    @staticmethod
    def _freeze(value: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            frozen = deep_freeze(value)
        except (TypeError, ValueError) as exc:
            raise RouterSerializationError(f"cannot freeze serialized payload: {exc}") from exc
        return frozen
