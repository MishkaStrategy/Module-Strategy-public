from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any
import unicodedata

from .canonical import decimal_text, has_control_characters, sha256_hex, stable_id
from .decimal_math import DecimalDomainError, validate_decimal_domain
from .enums import ReasonCode, ScalingAction, ScalingDirection, TradeSide
from .errors import RouterInputError
from .limits import MAX_ROUTER_COLLECTION_ITEMS
from .models import (
    EntryCandidate,
    MAX_DOMAIN_INTEGER_ABS,
    MAX_PRIORITY_ABS,
    PendingOrderState,
    PortfolioSnapshot,
    PositionState,
    ProtectionDecision,
    ScalingDecision,
    SizingDecision,
    TargetIntent,
)


MAX_INPUT_MAPPING_KEYS = 4_096
MAX_INPUT_SEQUENCE_ITEMS = 4_096
MAX_INPUT_TEXT_LENGTH = 512


def _safe_datetime_add(value: datetime, delta: timedelta, name: str) -> datetime:
    try:
        return value + delta
    except (OverflowError, ValueError) as exc:
        raise RouterInputError(f"{name} exceeds supported datetime range") from exc


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RouterInputError(f"{name} must be a mapping")
    result: dict[str, Any] = {}
    for index, row in enumerate(value.items()):
        if index >= MAX_INPUT_MAPPING_KEYS:
            raise RouterInputError(
                f"{name} exceeds maximum key count {MAX_INPUT_MAPPING_KEYS}",
                ReasonCode.INPUT_LIMIT_EXCEEDED,
            )
        try:
            key, member = row
        except (TypeError, ValueError) as exc:
            raise RouterInputError(f"{name} items must be key/value pairs") from exc
        if not isinstance(key, str):
            raise RouterInputError(f"{name} keys must be strings")
        if not key or key != key.strip():
            raise RouterInputError(f"{name} keys must be exact non-blank strings")
        if len(key) > MAX_INPUT_TEXT_LENGTH:
            raise RouterInputError(f"{name} key exceeds maximum length {MAX_INPUT_TEXT_LENGTH}")
        if unicodedata.normalize("NFC", key) != key:
            raise RouterInputError(f"{name} keys must use NFC normalization")
        if has_control_characters(key):
            raise RouterInputError(f"{name} keys cannot contain control characters")
        if key in result:
            raise RouterInputError(f"{name} contains duplicate key {key!r}")
        result[key] = member
    return MappingProxyType(result)


def _text(value: Any, name: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        value = default
    if not isinstance(value, str) or not value.strip():
        raise RouterInputError(f"{name} must be a non-blank string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if len(normalized) > MAX_INPUT_TEXT_LENGTH:
        raise RouterInputError(f"{name} exceeds maximum length {MAX_INPUT_TEXT_LENGTH}")
    if has_control_characters(normalized):
        raise RouterInputError(f"{name} cannot contain control characters")
    return normalized


def _optional_text(value: Any, name: str = "value") -> str | None:
    return None if value is None else _text(value, name)


def _schema_version(value: Any, name: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        value = default
    if not isinstance(value, str) or not value or value != value.strip():
        raise RouterInputError(f"{name} must be an exact non-blank string")
    if has_control_characters(value):
        raise RouterInputError(f"{name} cannot contain control characters")
    return value


def _datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RouterInputError(f"{name} must be ISO-8601 datetime") from exc
    else:
        raise RouterInputError(f"{name} must be datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise RouterInputError(f"{name} must be timezone-aware")
    return result.astimezone(UTC)


def _optional_datetime(value: Any, name: str) -> datetime | None:
    return None if value is None else _datetime(value, name)


def _decimal(value: Any, name: str, *, default: str | None = None) -> Decimal:
    if value is None and default is not None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise RouterInputError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RouterInputError(f"{name} must be numeric") from exc
    try:
        validate_decimal_domain(result, name)
    except DecimalDomainError as exc:
        raise RouterInputError(str(exc)) from exc
    return result


def _optional_decimal(value: Any, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _integer(
    value: Any,
    name: str,
    *,
    default: int = 0,
    minimum: int = -MAX_DOMAIN_INTEGER_ABS,
    maximum: int = MAX_DOMAIN_INTEGER_ABS,
) -> int:
    result = default if value is None else value
    if isinstance(result, bool) or not isinstance(result, int):
        raise RouterInputError(f"{name} must be integer")
    if not minimum <= result <= maximum:
        raise RouterInputError(f"{name} must be in range [{minimum}, {maximum}]")
    return result


def _boolean(value: Any, name: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RouterInputError(f"{name} must be boolean")
    return value


def _side(value: Any) -> TradeSide:
    if isinstance(value, TradeSide):
        return value
    if not isinstance(value, str):
        raise RouterInputError("side must be buy/long or sell/short")
    text = value.strip().lower()
    if text in {"buy", "long"}:
        return TradeSide.LONG
    if text in {"sell", "short"}:
        return TradeSide.SHORT
    raise RouterInputError("side must be buy/long or sell/short")


def _message_text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RouterInputError(f"{name} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > 4_096:
        raise RouterInputError(f"{name} exceeds maximum length 4096")
    if has_control_characters(normalized):
        raise RouterInputError(f"{name} cannot contain control characters")
    return normalized


def _metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = payload.get("metadata", {})
    return _mapping(raw, "metadata")


def _tuple_strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise RouterInputError(f"{name} must be an array")
    if len(value) > MAX_INPUT_SEQUENCE_ITEMS:
        raise RouterInputError(
            f"{name} exceeds maximum item count {MAX_INPUT_SEQUENCE_ITEMS}",
            ReasonCode.INPUT_LIMIT_EXCEEDED,
        )
    return tuple(_text(item, name) for item in value)


def _reject_unknown(
    payload: Mapping[str, Any],
    allowed: set[str],
    contract: str,
    *,
    strict_mode: bool,
) -> None:
    if not strict_mode:
        return
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise RouterInputError(f"unknown {contract} fields: {', '.join(unknown)}")


def _stable_payload_id(prefix: str, payload: Mapping[str, Any]) -> str:
    try:
        return stable_id(prefix, payload)
    except (TypeError, ValueError) as exc:
        raise RouterInputError(f"{prefix} payload is not canonicalizable: {exc}") from exc


def _stable_payload_hash(label: str, payload: Mapping[str, Any]) -> str:
    try:
        return sha256_hex(payload)
    except (TypeError, ValueError) as exc:
        raise RouterInputError(f"{label} payload is not canonicalizable: {exc}") from exc


def _require_schema_match(
    payload_schema: str,
    feature_schema: str,
    contract: str,
) -> None:
    if feature_schema != payload_schema:
        raise RouterInputError(f"{contract} feature schema conflicts with payload schema")


class DecisionNormalizer:
    """Compatibility boundary for released upstream decision schemas."""

    def __init__(
        self,
        *,
        strict_mode: bool = True,
        max_targets_per_plan: int = 100,
        max_portfolio_positions: int = 5_000,
        max_pending_orders: int = 10_000,
    ) -> None:
        if not isinstance(strict_mode, bool):
            raise RouterInputError("strict_mode must be boolean")
        for name, value in (
            ("max_targets_per_plan", max_targets_per_plan),
            ("max_portfolio_positions", max_portfolio_positions),
            ("max_pending_orders", max_pending_orders),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RouterInputError(f"{name} must be a positive integer")
            if value > MAX_ROUTER_COLLECTION_ITEMS:
                raise RouterInputError(
                    f"{name} must be in range [1, {MAX_ROUTER_COLLECTION_ITEMS}]"
                )
        self.strict_mode = strict_mode
        self.max_targets_per_plan = max_targets_per_plan
        self.max_portfolio_positions = max_portfolio_positions
        self.max_pending_orders = max_pending_orders

    ENTRY_KEYS = ("entry_model.plan", "entry_model.entry_plan")
    PROTECTION_KEY = "protection_target.plan"
    SIZING_KEY = "risk.decision"
    SCALING_KEY = "position_scaling.approved"
    PORTFOLIO_KEY = "portfolio.snapshot"

    def normalize_entry(
        self,
        payload: Mapping[str, Any],
        *,
        feature_key: str,
        feature_schema_version: str,
        feature_created_at: datetime,
        feature_valid_until: datetime | None,
        feature_id: str | None,
        source_module: str,
        symbol: str,
        portfolio_id: str = "default",
    ) -> EntryCandidate:
        payload = _mapping(payload, "entry payload")
        feature_key = _text(feature_key, "entry feature key")
        feature_created_at = _datetime(feature_created_at, "entry feature created_at")
        feature_valid_until = _optional_datetime(feature_valid_until, "entry feature valid_until")
        feature_id = _optional_text(feature_id, "entry feature id")
        source_module = _text(source_module, "entry source module")
        symbol = _text(symbol, "entry feature symbol")
        portfolio_id = _text(portfolio_id, "entry portfolio id")
        if feature_key not in self.ENTRY_KEYS:
            raise RouterInputError(f"unsupported entry feature key: {feature_key}")
        feature_schema_version = _schema_version(
            feature_schema_version,
            "entry feature schema_version",
        )
        payload_schema = _schema_version(
            payload.get("schema_version", feature_schema_version),
            "entry payload schema_version",
        )
        payload_symbol = _text(payload.get("symbol"), "symbol")
        metadata = _metadata(payload)
        priority = _integer(
            metadata.get("setup_priority", metadata.get("priority")),
            "priority",
            default=0,
            minimum=-MAX_PRIORITY_ABS,
            maximum=MAX_PRIORITY_ABS,
        )
        if feature_key == "entry_model.plan" and payload_schema == "2":
            if feature_schema_version != "2":
                raise RouterInputError("entry feature schema conflicts with payload schema")
            allowed = {
                "schema_version",
                "plan_id",
                "module_version",
                "setup_id",
                "setup_fingerprint",
                "config_fingerprint",
                "symbol",
                "side",
                "entry_kind",
                "order_type",
                "created_at",
                "setup_confirmed_at",
                "expires_at",
                "ttl_bars",
                "price",
                "trigger_price",
                "limit_price",
                "post_only",
                "rationale",
                "metadata",
            }
            unknown = sorted(set(payload) - allowed)
            if unknown and self.strict_mode:
                raise RouterInputError(f"unknown EntryPlan fields: {', '.join(unknown)}")
            candidate_id = _text(payload.get("plan_id"), "plan_id")
            setup_id = _text(payload.get("setup_id"), "setup_id")
            created_at = _datetime(payload.get("created_at"), "created_at")
            available_at = _datetime(payload.get("setup_confirmed_at"), "setup_confirmed_at")
            signal_time = available_at
            expires_at = _datetime(payload.get("expires_at"), "expires_at")
            module_version = _text(payload.get("module_version"), "module_version")
            entry_kind = _text(payload.get("entry_kind"), "entry_kind")
            order_type = _text(payload.get("order_type"), "order_type").lower()
            ttl_bars = _integer(payload.get("ttl_bars"), "ttl_bars", minimum=1)
            price = _optional_decimal(payload.get("price"), "price")
            trigger_price = _optional_decimal(payload.get("trigger_price"), "trigger_price")
            limit_price = _optional_decimal(payload.get("limit_price"), "limit_price")
            post_only = payload.get("post_only", False)
            if not isinstance(post_only, bool):
                raise RouterInputError("post_only must be boolean")
            setup_fingerprint = _text(payload.get("setup_fingerprint"), "setup_fingerprint")
            config_fingerprint = _text(payload.get("config_fingerprint"), "config_fingerprint")
            if len(setup_fingerprint) != 64 or len(config_fingerprint) != 64:
                raise RouterInputError("entry setup/config fingerprints must be SHA-256 hex")
            try:
                int(setup_fingerprint, 16)
                int(config_fingerprint, 16)
            except ValueError as exc:
                raise RouterInputError(
                    "entry setup/config fingerprints must be hexadecimal"
                ) from exc
            identity = {
                "schema_version": "2",
                "module_version": module_version,
                "setup_id": setup_id,
                "setup_fingerprint": setup_fingerprint,
                "config_fingerprint": config_fingerprint,
                "symbol": payload_symbol,
                "side": _text(payload.get("side"), "side").lower(),
                "entry_kind": entry_kind,
                "order_type": order_type,
                "created_at": created_at.isoformat(),
                "setup_confirmed_at": available_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "ttl_bars": ttl_bars,
                "price": None if price is None else decimal_text(price),
                "trigger_price": None if trigger_price is None else decimal_text(trigger_price),
                "limit_price": None if limit_price is None else decimal_text(limit_price),
                "post_only": post_only,
                "rationale": _message_text(payload.get("rationale"), "rationale"),
                "metadata": metadata,
            }
            expected_candidate_id = _stable_payload_id("entry", identity)
            if candidate_id != expected_candidate_id:
                raise RouterInputError("entry plan_id does not match canonical upstream identity")
            if created_at != feature_created_at:
                raise RouterInputError("entry payload created_at conflicts with feature.created_at")
            if feature_valid_until is not None and expires_at != feature_valid_until:
                raise RouterInputError(
                    "entry payload expires_at conflicts with feature.valid_until"
                )
        elif payload_schema == "1":
            if feature_schema_version != "1":
                raise RouterInputError("legacy entry feature schema conflicts with payload schema")
            allowed = {
                "schema_version",
                "entry_plan_id",
                "setup_id",
                "symbol",
                "timeframe",
                "side",
                "entry_price",
                "created_at",
                "valid_until",
                "module_version",
                "entry_kind",
                "order_type",
                "ttl_bars",
                "post_only",
                "metadata",
            }
            unknown = sorted(set(payload) - allowed)
            if unknown and self.strict_mode:
                raise RouterInputError(f"unknown legacy entry fields: {', '.join(unknown)}")
            candidate_id = _text(payload.get("entry_plan_id"), "entry_plan_id")
            setup_id = _text(payload.get("setup_id"), "setup_id")
            created_at = (
                _optional_datetime(payload.get("created_at"), "created_at") or feature_created_at
            )
            available_at = created_at
            signal_time = created_at
            expires_at = (
                _optional_datetime(payload.get("valid_until"), "valid_until")
                or feature_valid_until
                or _safe_datetime_add(created_at, timedelta(minutes=5), "legacy valid_until")
            )
            module_version = _text(
                payload.get("module_version"), "module_version", default="legacy-1"
            )
            entry_kind = _text(payload.get("entry_kind"), "entry_kind", default="limit_explicit")
            order_type = _text(payload.get("order_type"), "order_type", default="limit").lower()
            ttl_bars = _integer(payload.get("ttl_bars"), "ttl_bars", default=1, minimum=1)
            entry_price = _decimal(payload.get("entry_price"), "entry_price")
            price = entry_price if order_type == "limit" else None
            trigger_price = entry_price if order_type in {"stop_market", "stop_limit"} else None
            limit_price = entry_price if order_type in {"limit", "stop_limit"} else None
            post_only_raw = payload.get("post_only", False)
            if not isinstance(post_only_raw, bool):
                raise RouterInputError("legacy post_only must be boolean")
            post_only = post_only_raw
        else:
            raise RouterInputError(f"unsupported entry plan schema: {payload_schema}")
        if payload_symbol != symbol:
            raise RouterInputError("entry payload symbol conflicts with feature symbol")
        strategy_id = _text(metadata.get("strategy_id"), "strategy_id", default=source_module)
        module_id = _text(metadata.get("module_id"), "module_id", default=source_module)
        normalized_portfolio = _text(
            metadata.get("portfolio_id"), "portfolio_id", default=portfolio_id
        )
        confidence = _optional_decimal(metadata.get("confidence"), "confidence")
        candidate_payload = {
            "source_feature_id": feature_id,
            "candidate_id": candidate_id,
            "payload": payload,
            "feature_key": feature_key,
            "feature_schema_version": feature_schema_version,
        }
        return EntryCandidate(
            candidate_id=candidate_id,
            source_decision_id=feature_id or candidate_id,
            strategy_id=strategy_id,
            setup_id=setup_id,
            module_id=module_id,
            module_version=module_version,
            portfolio_id=normalized_portfolio,
            symbol=symbol,
            market=_text(metadata.get("market"), "market", default="unknown"),
            side=_side(payload.get("side")),
            signal_time=signal_time,
            available_at=available_at,
            created_at=created_at,
            expires_at=expires_at,
            entry_kind=entry_kind,
            order_type=order_type,
            ttl_bars=ttl_bars,
            price=price,
            trigger_price=trigger_price,
            limit_price=limit_price,
            post_only=post_only,
            priority=priority,
            confidence=confidence,
            incompatibility_tags=_tuple_strings(
                metadata.get("incompatibility_tags"), "incompatibility_tags"
            ),
            exclusivity_group=_optional_text(metadata.get("exclusivity_group")),
            correlation_group=_optional_text(metadata.get("correlation_group")),
            reason_codes=_tuple_strings(metadata.get("reason_codes"), "reason_codes"),
            metadata={
                **dict(metadata),
                "source_contract_hash": _stable_payload_hash(
                    "entry source contract", candidate_payload
                ),
            },
        )

    def normalize_protection(
        self,
        payload: Mapping[str, Any],
        *,
        feature_schema_version: str,
        feature_created_at: datetime,
        feature_valid_until: datetime | None,
        feature_id: str | None,
        symbol: str,
    ) -> ProtectionDecision:
        payload = _mapping(payload, "protection payload")
        feature_created_at = _datetime(feature_created_at, "protection feature created_at")
        feature_valid_until = _optional_datetime(
            feature_valid_until, "protection feature valid_until"
        )
        feature_id = _optional_text(feature_id, "protection feature id")
        symbol = _text(symbol, "protection feature symbol")
        feature_schema_version = _schema_version(
            feature_schema_version,
            "protection feature schema_version",
        )
        schema = _schema_version(
            payload.get("schema_version", feature_schema_version),
            "protection payload schema_version",
        )
        if schema != "1":
            raise RouterInputError(f"unsupported protection schema: {schema}")
        _require_schema_match(schema, feature_schema_version, "protection")
        _reject_unknown(
            payload,
            {
                "schema_version",
                "protection_plan_id",
                "entry_plan_id",
                "setup_id",
                "symbol",
                "timeframe",
                "side",
                "entry_price",
                "invalidation_price",
                "stop_loss",
                "take_profit",
                "targets",
                "risk_distance",
                "reward_distance",
                "stop_distance_bps",
                "reward_r",
                "invalidation_mode",
                "target_mode",
                "created_at",
                "valid_until",
                "source_entry_feature_id",
                "source_invalidation_feature_id",
                "source_target_feature_id",
                "max_holding_seconds",
                "no_progress_rule",
                "invalidation_rule",
                "metadata",
            },
            "protection plan",
            strict_mode=self.strict_mode,
        )
        payload_symbol = _text(payload.get("symbol"), "symbol", default=symbol)
        if payload_symbol != symbol:
            raise RouterInputError("protection payload symbol conflicts with feature symbol")
        targets_raw = payload.get("targets")
        targets: tuple[TargetIntent, ...]
        if targets_raw is None:
            targets = (TargetIntent(price=_decimal(payload.get("take_profit"), "take_profit")),)
        else:
            if not isinstance(targets_raw, (tuple, list)):
                raise RouterInputError("targets must be an array")
            if len(targets_raw) > self.max_targets_per_plan:
                raise RouterInputError(
                    "protection targets exceed configured input limit",
                    ReasonCode.INPUT_LIMIT_EXCEEDED,
                )
            targets = tuple(
                TargetIntent(
                    price=_decimal(_mapping(item, "target").get("price"), "target.price"),
                    allocation=_decimal(
                        _mapping(item, "target").get("allocation", "1"), "target.allocation"
                    ),
                    target_type=_text(
                        _mapping(item, "target").get("target_type"),
                        "target.target_type",
                        default="limit",
                    ),
                )
                for item in targets_raw
            )
        decision_id = _text(payload.get("protection_plan_id"), "protection_plan_id")
        created_at = (
            _optional_datetime(payload.get("created_at"), "created_at") or feature_created_at
        )
        expires_at = (
            _optional_datetime(payload.get("valid_until"), "valid_until") or feature_valid_until
        )
        if payload.get("created_at") is not None and created_at != feature_created_at:
            raise RouterInputError(
                "protection payload created_at conflicts with feature.created_at"
            )
        if (
            payload.get("valid_until") is not None
            and feature_valid_until is not None
            and expires_at != feature_valid_until
        ):
            raise RouterInputError(
                "protection payload valid_until conflicts with feature.valid_until"
            )
        metadata = _metadata(payload)
        return ProtectionDecision(
            decision_id=decision_id,
            entry_candidate_id=_text(payload.get("entry_plan_id"), "entry_plan_id"),
            setup_id=_text(payload.get("setup_id"), "setup_id"),
            symbol=symbol,
            side=_side(payload.get("side")),
            entry_price=_decimal(payload.get("entry_price"), "entry_price"),
            stop_loss=_decimal(payload.get("stop_loss"), "stop_loss"),
            targets=targets,
            created_at=created_at,
            expires_at=expires_at,
            risk_distance=_decimal(payload.get("risk_distance"), "risk_distance"),
            risk_reward=_optional_decimal(payload.get("reward_r"), "reward_r"),
            max_holding_seconds=(
                None
                if payload.get("max_holding_seconds") is None
                else _integer(
                    payload.get("max_holding_seconds"),
                    "max_holding_seconds",
                    minimum=1,
                )
            ),
            no_progress_rule=_mapping(payload.get("no_progress_rule", {}), "no_progress_rule"),
            invalidation_rule=_mapping(payload.get("invalidation_rule", {}), "invalidation_rule"),
            source_decision_id=feature_id or decision_id,
            metadata=metadata,
        )

    def normalize_sizing(
        self,
        payload: Mapping[str, Any],
        *,
        feature_schema_version: str,
        feature_created_at: datetime,
        feature_valid_until: datetime | None,
        feature_id: str | None,
        symbol: str,
    ) -> SizingDecision:
        payload = _mapping(payload, "sizing payload")
        feature_created_at = _datetime(feature_created_at, "sizing feature created_at")
        feature_valid_until = _optional_datetime(feature_valid_until, "sizing feature valid_until")
        feature_id = _optional_text(feature_id, "sizing feature id")
        symbol = _text(symbol, "sizing feature symbol")
        feature_schema_version = _schema_version(
            feature_schema_version,
            "sizing feature schema_version",
        )
        schema = _schema_version(
            payload.get("schema_version", feature_schema_version),
            "sizing payload schema_version",
        )
        if schema != "1":
            raise RouterInputError(f"unsupported sizing schema: {schema}")
        _require_schema_match(schema, feature_schema_version, "sizing")
        _reject_unknown(
            payload,
            {
                "schema_version",
                "request_id",
                "symbol",
                "status",
                "approved",
                "quantity",
                "raw_quantity",
                "risk_fraction",
                "risk_budget",
                "actual_risk_amount",
                "actual_risk_fraction",
                "notional",
                "required_leverage",
                "binding_constraints",
                "reject_reason",
                "message",
                "setup_id",
                "diagnostics",
                "position_adjustment",
            },
            "risk decision",
            strict_mode=self.strict_mode,
        )
        payload_symbol = _text(payload.get("symbol"), "symbol", default=symbol)
        if payload_symbol != symbol:
            raise RouterInputError("sizing payload symbol conflicts with feature symbol")
        status = _text(payload.get("status"), "status").lower()
        if status not in {"approved", "rejected"}:
            raise RouterInputError("sizing status must be approved or rejected")
        approved_raw = payload.get("approved")
        if not isinstance(approved_raw, bool):
            raise RouterInputError("sizing approved flag must be boolean")
        if approved_raw != (status == "approved"):
            raise RouterInputError("sizing status conflicts with approved flag")
        request_id = _text(payload.get("request_id"), "request_id")
        diagnostics = _mapping(payload.get("diagnostics", {}), "diagnostics")
        position_adjustment = payload.get("position_adjustment")
        if position_adjustment is not None:
            _mapping(position_adjustment, "position_adjustment")
        setup_id = _text(payload.get("setup_id"), "setup_id")
        quantity = _decimal(payload.get("quantity", "0"), "quantity")
        notional = _decimal(payload.get("notional", "0"), "notional")
        risk_amount = _decimal(
            payload.get("actual_risk_amount", "0"),
            "actual_risk_amount",
        )
        risk_fraction = _decimal(
            payload.get(
                "actual_risk_fraction",
                payload.get("risk_fraction", "0"),
            ),
            "actual_risk_fraction",
        )
        leverage = _decimal(payload.get("required_leverage", "0"), "required_leverage")
        binding_constraints = _tuple_strings(
            payload.get("binding_constraints"),
            "binding_constraints",
        )
        rejection_reason = _optional_text(payload.get("reject_reason"))
        entry_candidate_id = _optional_text(diagnostics.get("entry_plan_id"))
        protection_decision_id = _optional_text(diagnostics.get("protection_plan_id"))
        message = _message_text(payload.get("message"), "message")
        decision_id = feature_id or _stable_payload_id("sizing", payload)
        return SizingDecision(
            decision_id=decision_id,
            request_id=request_id,
            setup_id=setup_id,
            symbol=symbol,
            approved=approved_raw,
            quantity=quantity,
            notional=notional,
            risk_amount=risk_amount,
            risk_fraction=risk_fraction,
            leverage=leverage,
            created_at=feature_created_at,
            expires_at=feature_valid_until,
            binding_constraints=binding_constraints,
            rejection_reason=rejection_reason,
            entry_candidate_id=entry_candidate_id,
            protection_decision_id=protection_decision_id,
            source_decision_id=feature_id or request_id,
            metadata={
                "message": message,
                "diagnostics": diagnostics,
                "position_adjustment": position_adjustment,
            },
        )

    def normalize_scaling(
        self,
        payload: Mapping[str, Any],
        *,
        feature_schema_version: str,
        feature_created_at: datetime,
        feature_valid_until: datetime | None,
        feature_id: str | None,
        symbol: str,
    ) -> ScalingDecision:
        payload = _mapping(payload, "scaling payload")
        feature_created_at = _datetime(feature_created_at, "scaling feature created_at")
        feature_valid_until = _optional_datetime(feature_valid_until, "scaling feature valid_until")
        feature_id = _optional_text(feature_id, "scaling feature id")
        symbol = _text(symbol, "scaling feature symbol")
        feature_schema_version = _schema_version(
            feature_schema_version,
            "scaling feature schema_version",
        )
        schema = _schema_version(
            payload.get("schema_version", feature_schema_version),
            "scaling payload schema_version",
        )
        if schema != "1":
            raise RouterInputError(f"unsupported scaling schema: {schema}")
        _require_schema_match(schema, feature_schema_version, "scaling")
        _reject_unknown(
            payload,
            {
                "schema_version",
                "adjustment_id",
                "position_id",
                "symbol",
                "side",
                "action",
                "direction",
                "approved_quantity",
                "desired_price",
                "risk_request_id",
                "reason_code",
                "approved_at",
                "plan",
                "risk_decision",
            },
            "approved adjustment",
            strict_mode=self.strict_mode,
        )
        plan = _mapping(payload.get("plan", {}), "plan")
        _reject_unknown(
            plan,
            {
                "schema_version",
                "adjustment_id",
                "position_id",
                "setup_id",
                "symbol",
                "side",
                "action",
                "direction",
                "scaling_mode",
                "stage",
                "decision_time",
                "decision_index",
                "desired_entry_kind",
                "desired_price",
                "trigger_price",
                "desired_quantity",
                "maximum_quantity",
                "risk_budget",
                "notional_budget",
                "current_average_price",
                "expected_average_price",
                "current_quantity",
                "expected_total_quantity",
                "current_total_risk",
                "projected_total_risk",
                "maximum_total_risk",
                "stop_price",
                "invalidation_price",
                "expires_at",
                "expires_index",
                "reason_code",
                "risk_request_id",
                "metadata",
                "source_feature_ids",
                "config_fingerprint",
            },
            "embedded scaling plan",
            strict_mode=self.strict_mode,
        )
        if (
            "schema_version" in plan
            and _schema_version(
                plan["schema_version"],
                "embedded scaling plan schema_version",
            )
            != "1"
        ):
            raise RouterInputError("unsupported embedded scaling plan schema")
        payload_symbol = _text(payload.get("symbol"), "symbol", default=symbol)
        if payload_symbol != symbol:
            raise RouterInputError("scaling payload symbol conflicts with feature symbol")
        try:
            action = ScalingAction(_text(payload.get("action"), "action").lower())
        except ValueError as exc:
            raise RouterInputError("unsupported scaling action") from exc
        direction_raw = payload.get("direction")
        if direction_raw is None:
            direction = {
                ScalingAction.ADD: ScalingDirection.INCREASE,
                ScalingAction.REDUCE: ScalingDirection.DECREASE,
                ScalingAction.HOLD: ScalingDirection.NONE,
                ScalingAction.STOP_SCALING: ScalingDirection.NONE,
            }.get(action)
            if direction is None:
                raise RouterInputError("rebalance scaling requires explicit direction")
        else:
            try:
                direction = ScalingDirection(_text(direction_raw, "direction").lower())
            except ValueError as exc:
                raise RouterInputError("unsupported scaling direction") from exc
        created_at = (
            _optional_datetime(payload.get("approved_at"), "approved_at") or feature_created_at
        )
        if payload.get("approved_at") is not None and created_at != feature_created_at:
            raise RouterInputError("scaling payload approved_at conflicts with feature.created_at")
        adjustment_id = _text(payload.get("adjustment_id"), "adjustment_id")
        position_id = _text(payload.get("position_id"), "position_id")
        top_side = _side(payload.get("side"))
        for key, expected in (
            ("adjustment_id", adjustment_id),
            ("position_id", position_id),
            ("symbol", symbol),
        ):
            if key in plan and _text(plan.get(key), f"plan.{key}") != expected:
                raise RouterInputError(f"scaling payload disagrees with embedded plan {key}")
        if "side" in plan and _side(plan.get("side")) is not top_side:
            raise RouterInputError("scaling payload disagrees with embedded plan side")
        if "action" in plan:
            try:
                embedded_action = ScalingAction(_text(plan.get("action"), "plan.action").lower())
            except ValueError as exc:
                raise RouterInputError("unsupported embedded scaling action") from exc
            if embedded_action is not action:
                raise RouterInputError("scaling payload disagrees with embedded plan action")
        if "direction" in plan:
            try:
                embedded_direction = ScalingDirection(
                    _text(plan.get("direction"), "plan.direction").lower()
                )
            except ValueError as exc:
                raise RouterInputError("unsupported embedded scaling direction") from exc
            if embedded_direction is not direction:
                raise RouterInputError("scaling payload disagrees with embedded plan direction")
        if "desired_price" in plan and _decimal(
            plan.get("desired_price"), "plan.desired_price"
        ) != _decimal(payload.get("desired_price"), "desired_price"):
            raise RouterInputError("scaling payload disagrees with embedded plan desired_price")
        if "risk_request_id" in plan and plan.get("risk_request_id") != payload.get(
            "risk_request_id"
        ):
            raise RouterInputError("scaling payload disagrees with embedded plan risk_request_id")
        risk_request_id = _optional_text(payload.get("risk_request_id"))
        reason_code = _text(payload.get("reason_code"), "reason_code")
        approved_quantity = _decimal(
            payload.get("approved_quantity", "0"),
            "approved_quantity",
        )
        risk_decision = payload.get("risk_decision")
        if risk_decision is not None:
            risk_mapping = _mapping(risk_decision, "risk_decision")
            if (
                _schema_version(
                    risk_mapping.get("schema_version", "1"),
                    "embedded scaling risk decision schema_version",
                )
                != "1"
            ):
                raise RouterInputError("unsupported embedded risk decision schema")
            risk_status = risk_mapping.get("status")
            if (
                risk_mapping.get("approved") is not True
                or not isinstance(risk_status, str)
                or risk_status != "approved"
            ):
                raise RouterInputError("embedded risk decision must be approved")
            if payload.get("risk_request_id") is not None and risk_mapping.get(
                "request_id"
            ) != payload.get("risk_request_id"):
                raise RouterInputError(
                    "embedded risk decision request_id conflicts with scaling payload"
                )
            if risk_mapping.get("symbol") not in {None, symbol}:
                raise RouterInputError(
                    "embedded risk decision symbol conflicts with scaling payload"
                )
            if plan.get("setup_id") is not None and risk_mapping.get("setup_id") not in {
                None,
                plan.get("setup_id"),
            }:
                raise RouterInputError(
                    "embedded risk decision setup_id conflicts with scaling plan"
                )
        if direction is ScalingDirection.INCREASE:
            if risk_request_id is None or risk_decision is None:
                raise RouterInputError(
                    "increase scaling requires risk_request_id and risk_decision"
                )
            if reason_code != "risk_approved":
                raise RouterInputError("increase scaling requires risk_approved reason_code")
        elif risk_request_id is not None or risk_decision is not None:
            raise RouterInputError("non-increase scaling cannot carry risk reservation data")
        elif reason_code != "reduction_approved":
            raise RouterInputError("non-increase scaling requires reduction_approved reason_code")
        desired_quantity = _optional_decimal(plan.get("desired_quantity"), "plan.desired_quantity")
        maximum_quantity = _optional_decimal(plan.get("maximum_quantity"), "plan.maximum_quantity")
        caps = tuple(value for value in (desired_quantity, maximum_quantity) if value is not None)
        if caps and approved_quantity > min(caps):
            raise RouterInputError("approved scaling quantity exceeds embedded plan cap")
        if direction is ScalingDirection.INCREASE and risk_decision is not None:
            risk_quantity = _optional_decimal(
                _mapping(risk_decision, "risk_decision").get("quantity"),
                "risk_decision.quantity",
            )
            if risk_quantity is not None:
                expected_quantity = min((risk_quantity, *caps)) if caps else risk_quantity
                if approved_quantity != expected_quantity:
                    raise RouterInputError(
                        "approved scaling quantity disagrees with capped risk decision"
                    )
        return ScalingDecision(
            decision_id=adjustment_id,
            position_id=position_id,
            setup_id=_optional_text(plan.get("setup_id")),
            symbol=symbol,
            side=top_side,
            action=action,
            approved_quantity=approved_quantity,
            desired_price=_decimal(payload.get("desired_price"), "desired_price"),
            created_at=created_at,
            order_type=_text(
                plan.get("desired_entry_kind"), "plan.desired_entry_kind", default="limit"
            ).lower(),
            trigger_price=_optional_decimal(plan.get("trigger_price"), "plan.trigger_price"),
            direction=direction,
            risk_request_id=risk_request_id,
            expires_at=_optional_datetime(plan.get("expires_at"), "expires_at")
            or feature_valid_until,
            stage=_integer(plan.get("stage"), "stage", default=1, minimum=1),
            source_decision_id=feature_id or adjustment_id,
            metadata={
                "reason_code": reason_code,
                "risk_decision": risk_decision,
                "embedded_plan_hash": _stable_payload_hash("scaling embedded plan", plan),
            },
        )

    def normalize_portfolio(
        self,
        payload: Mapping[str, Any],
        *,
        feature_created_at: datetime,
    ) -> PortfolioSnapshot:
        payload = _mapping(payload, "portfolio payload")
        feature_created_at = _datetime(feature_created_at, "portfolio feature created_at")
        schema = _schema_version(
            payload.get("schema_version", "1"),
            "portfolio payload schema_version",
        )
        if schema != "1":
            raise RouterInputError(f"unsupported portfolio snapshot schema: {schema}")
        _reject_unknown(
            payload,
            {
                "schema_version",
                "portfolio_id",
                "version",
                "snapshot_version",
                "timestamp",
                "snapshot_timestamp",
                "equity",
                "available_balance",
                "used_margin",
                "free_margin",
                "risk_already_reserved",
                "available_risk_budget",
                "position_state_version",
                "order_state_version",
                "risk_reservation_version",
                "positions",
                "pending_orders",
                "active_plan_ids",
                "instrument_locks",
                "strategy_locks",
                "portfolio_locked",
                "daily_loss_limit_active",
                "drawdown_limit_active",
                "emergency_state",
                "circuit_breaker_active",
                "current_drawdown",
                "metadata",
            },
            "portfolio snapshot",
            strict_mode=self.strict_mode,
        )
        positions_raw = payload.get("positions", ())
        pending_raw = payload.get("pending_orders", ())
        if not isinstance(positions_raw, (tuple, list)):
            raise RouterInputError("portfolio positions must be an array")
        if not isinstance(pending_raw, (tuple, list)):
            raise RouterInputError("portfolio pending_orders must be an array")
        if len(positions_raw) > self.max_portfolio_positions:
            raise RouterInputError(
                "portfolio positions exceed configured input limit",
                ReasonCode.INPUT_LIMIT_EXCEEDED,
            )
        if len(pending_raw) > self.max_pending_orders:
            raise RouterInputError(
                "portfolio pending orders exceed configured input limit",
                ReasonCode.INPUT_LIMIT_EXCEEDED,
            )
        positions = tuple(self._position(_mapping(row, "position")) for row in positions_raw)
        pending = tuple(self._pending_order(_mapping(row, "pending_order")) for row in pending_raw)
        version = _text(payload.get("version", payload.get("snapshot_version")), "version")
        return PortfolioSnapshot(
            portfolio_id=_text(payload.get("portfolio_id"), "portfolio_id"),
            version=version,
            timestamp=_optional_datetime(
                payload.get("timestamp", payload.get("snapshot_timestamp")), "timestamp"
            )
            or feature_created_at,
            equity=_decimal(payload.get("equity", "0"), "equity"),
            available_balance=_decimal(payload.get("available_balance", "0"), "available_balance"),
            used_margin=_decimal(payload.get("used_margin", "0"), "used_margin"),
            free_margin=_decimal(payload.get("free_margin", "0"), "free_margin"),
            risk_already_reserved=_decimal(
                payload.get("risk_already_reserved", "0"), "risk_already_reserved"
            ),
            available_risk_budget=_decimal(
                payload.get("available_risk_budget", "0"), "available_risk_budget"
            ),
            position_state_version=_text(
                payload.get("position_state_version", version), "position_state_version"
            ),
            order_state_version=_text(
                payload.get("order_state_version", version), "order_state_version"
            ),
            risk_reservation_version=_text(
                payload.get("risk_reservation_version", version), "risk_reservation_version"
            ),
            positions=positions,
            pending_orders=pending,
            active_plan_ids=_tuple_strings(payload.get("active_plan_ids"), "active_plan_ids"),
            instrument_locks=_tuple_strings(payload.get("instrument_locks"), "instrument_locks"),
            strategy_locks=_tuple_strings(payload.get("strategy_locks"), "strategy_locks"),
            portfolio_locked=_boolean(payload.get("portfolio_locked"), "portfolio_locked"),
            daily_loss_limit_active=_boolean(
                payload.get("daily_loss_limit_active"), "daily_loss_limit_active"
            ),
            drawdown_limit_active=_boolean(
                payload.get("drawdown_limit_active"), "drawdown_limit_active"
            ),
            emergency_state=_boolean(payload.get("emergency_state"), "emergency_state"),
            circuit_breaker_active=_boolean(
                payload.get("circuit_breaker_active"), "circuit_breaker_active"
            ),
            current_drawdown=_decimal(payload.get("current_drawdown", "0"), "current_drawdown"),
            metadata=_mapping(payload.get("metadata", {}), "metadata"),
        )

    def _position(self, row: Mapping[str, Any]) -> PositionState:
        _reject_unknown(
            row,
            {
                "position_id",
                "id",
                "symbol",
                "side",
                "quantity",
                "average_entry",
                "entry_price",
                "version",
                "market",
                "strategy_id",
                "setup_id",
                "initial_risk",
                "remaining_risk",
                "reserved_risk",
                "opened_at",
                "scaling_stage",
            },
            "portfolio position",
            strict_mode=self.strict_mode,
        )
        return PositionState(
            position_id=_text(row.get("position_id", row.get("id")), "position_id"),
            symbol=_text(row.get("symbol"), "symbol"),
            side=_side(row.get("side")),
            quantity=_decimal(row.get("quantity"), "quantity"),
            average_entry=_decimal(
                row.get("average_entry", row.get("entry_price")), "average_entry"
            ),
            version=_text(row.get("version"), "position.version"),
            market=_optional_text(row.get("market")),
            strategy_id=_optional_text(row.get("strategy_id")),
            setup_id=_optional_text(row.get("setup_id")),
            initial_risk=_decimal(row.get("initial_risk", "0"), "initial_risk"),
            remaining_risk=_decimal(row.get("remaining_risk", "0"), "remaining_risk"),
            reserved_risk=_decimal(row.get("reserved_risk", "0"), "reserved_risk"),
            opened_at=_optional_datetime(row.get("opened_at"), "opened_at"),
            scaling_stage=_integer(row.get("scaling_stage"), "scaling_stage", default=0, minimum=0),
        )

    def _pending_order(self, row: Mapping[str, Any]) -> PendingOrderState:
        _reject_unknown(
            row,
            {
                "order_intent_id",
                "id",
                "plan_id",
                "symbol",
                "side",
                "order_type",
                "quantity",
                "status",
                "version",
                "created_at",
                "price",
                "stop_price",
                "reduce_only",
                "post_only",
                "reserved_risk",
                "strategy_id",
                "setup_id",
                "expires_at",
            },
            "portfolio pending order",
            strict_mode=self.strict_mode,
        )
        return PendingOrderState(
            order_intent_id=_text(row.get("order_intent_id", row.get("id")), "order_intent_id"),
            plan_id=_optional_text(row.get("plan_id")),
            symbol=_text(row.get("symbol"), "symbol"),
            side=_side(row.get("side")),
            order_type=_text(row.get("order_type"), "order_type"),
            quantity=_decimal(row.get("quantity"), "quantity"),
            status=_text(row.get("status"), "status"),
            version=_text(row.get("version"), "order.version"),
            created_at=_datetime(row.get("created_at"), "created_at"),
            price=_optional_decimal(row.get("price"), "price"),
            stop_price=_optional_decimal(row.get("stop_price"), "stop_price"),
            reduce_only=_boolean(row.get("reduce_only"), "reduce_only"),
            post_only=_boolean(row.get("post_only"), "post_only"),
            reserved_risk=_decimal(row.get("reserved_risk", "0"), "reserved_risk"),
            strategy_id=_optional_text(row.get("strategy_id")),
            setup_id=_optional_text(row.get("setup_id")),
            expires_at=_optional_datetime(row.get("expires_at"), "expires_at"),
        )
