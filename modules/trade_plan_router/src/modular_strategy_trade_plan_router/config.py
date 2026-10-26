from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any
import unicodedata

from .canonical import has_control_characters, sha256_hex
from .decimal_math import DecimalDomainError, validate_decimal_domain
from .enums import (
    ActivePlanPolicy,
    ExistingPositionPolicy,
    LongShortConflictPolicy,
    MultiSetupPolicy,
    PendingOrderPolicy,
    TradeSide,
)
from .errors import RouterConfigurationError
from .limits import MAX_ROUTER_COLLECTION_ITEMS


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise RouterConfigurationError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RouterConfigurationError(f"{name} must be numeric") from exc
    try:
        validate_decimal_domain(result, name)
    except DecimalDomainError as exc:
        raise RouterConfigurationError(str(exc)) from exc
    if result < 0:
        raise RouterConfigurationError(f"{name} must be finite and non-negative")
    return result


MAX_CONFIGURATION_KEY_LENGTH = 512
MAX_CONFIGURATION_FIELD_LENGTH = 128
MAX_CONFIGURATION_FIELDS = 64
MAX_PRIORITY_ENTRIES = 10_000
MAX_PRIORITY_ABS = 2_147_483_647
MAX_CONFIGURATION_POSITIVE_INT = 2_147_483_647


def _configuration_field_key(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RouterConfigurationError(f"{name} keys must be exact non-blank strings")
    if len(value) > MAX_CONFIGURATION_FIELD_LENGTH:
        raise RouterConfigurationError(
            f"{name} key exceeds maximum length {MAX_CONFIGURATION_FIELD_LENGTH}"
        )
    if unicodedata.normalize("NFC", value) != value:
        raise RouterConfigurationError(f"{name} keys must use NFC normalization")
    if has_control_characters(value):
        raise RouterConfigurationError(f"{name} keys cannot contain control characters")
    return value


def _mapping_snapshot(
    value: Any,
    name: str,
    *,
    max_entries: int,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RouterConfigurationError(f"{name} must be a mapping")
    result: dict[str, Any] = {}
    for index, row in enumerate(value.items()):
        if index >= max_entries:
            raise RouterConfigurationError(f"{name} exceeds maximum entry count {max_entries}")
        try:
            raw_key, member = row
        except (TypeError, ValueError) as exc:
            raise RouterConfigurationError(f"{name} items must be key/value pairs") from exc
        key = _configuration_field_key(raw_key, name)
        if key in result:
            raise RouterConfigurationError(f"{name} contains duplicate key {key!r}")
        result[key] = member
    return MappingProxyType(result)


def _configuration_key(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouterConfigurationError(f"{name} keys must be non-blank strings")
    normalized = unicodedata.normalize("NFC", value.strip())
    if len(normalized) > MAX_CONFIGURATION_KEY_LENGTH:
        raise RouterConfigurationError(
            f"{name} key exceeds maximum length {MAX_CONFIGURATION_KEY_LENGTH}"
        )
    if has_control_characters(normalized):
        raise RouterConfigurationError(f"{name} keys cannot contain control characters")
    return normalized


def _priority_map(value: Any, name: str) -> Mapping[str, int]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise RouterConfigurationError(f"{name} must be a mapping")
    result: dict[str, int] = {}
    for index, row in enumerate(value.items()):
        if index >= MAX_PRIORITY_ENTRIES:
            raise RouterConfigurationError(
                f"{name} exceeds maximum entry count {MAX_PRIORITY_ENTRIES}"
            )
        try:
            key, raw = row
        except (TypeError, ValueError) as exc:
            raise RouterConfigurationError(f"{name} items must be key/value pairs") from exc
        normalized_key = _configuration_key(key, name)
        if normalized_key in result:
            raise RouterConfigurationError(
                f"{name} contains duplicate normalized key {normalized_key!r}"
            )
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise RouterConfigurationError(f"{name}[{normalized_key!r}] must be integer")
        if not -MAX_PRIORITY_ABS <= raw <= MAX_PRIORITY_ABS:
            raise RouterConfigurationError(
                f"{name}[{normalized_key!r}] must be in signed 32-bit range"
            )
        result[normalized_key] = raw
    return MappingProxyType(dict(sorted(result.items())))


def _positive_int(
    value: Any,
    name: str,
    default: int,
    *,
    maximum: int = MAX_CONFIGURATION_POSITIVE_INT,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RouterConfigurationError(f"{name} must be a positive integer")
    if value > maximum:
        raise RouterConfigurationError(f"{name} must be in range [1, {maximum}]")
    return value


def _enum(enum_type: Any, value: Any, name: str, default: str) -> Any:
    raw = default if value is None else value
    if not isinstance(raw, str):
        raise RouterConfigurationError(f"{name} must be a string")
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise RouterConfigurationError(f"unsupported {name}: {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class RouterConfiguration:
    schema_version: str = "1"
    long_short_policy: LongShortConflictPolicy = LongShortConflictPolicy.REJECT_ALL
    multi_setup_policy: MultiSetupPolicy = MultiSetupPolicy.HIGHEST_PRIORITY
    existing_position_policy: ExistingPositionPolicy = ExistingPositionPolicy.REJECT
    pending_order_policy: PendingOrderPolicy = PendingOrderPolicy.REJECT
    active_plan_policy: ActivePlanPolicy = ActivePlanPolicy.REJECT_NEW_EXPOSURE
    strict_mode: bool = True
    require_portfolio_snapshot: bool = False
    allow_risk_reducing_when_locked: bool = True
    max_decision_age_seconds: int = 86_400
    max_snapshot_age_seconds: int = 300
    plan_ttl_seconds: int = 300
    max_portfolio_risk_fraction: Decimal = Decimal("0.03")
    max_symbol_notional_fraction: Decimal = Decimal("3")
    max_entry_candidates: int = 1_000
    max_decisions_per_type: int = 2_000
    max_portfolio_positions: int = 5_000
    max_pending_orders: int = 10_000
    max_targets_per_plan: int = 100
    portfolio_priorities: Mapping[str, int] = field(default_factory=dict)
    strategy_priorities: Mapping[str, int] = field(default_factory=dict)
    setup_priorities: Mapping[str, int] = field(default_factory=dict)
    module_priorities: Mapping[str, int] = field(default_factory=dict)
    side_priorities: Mapping[str, int] = field(default_factory=dict)
    warning_escalation: bool = False
    audit_level: str = "full"

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, str) or self.schema_version != "1":
            raise RouterConfigurationError("unsupported Router configuration schema")
        for name, enum_type in (
            ("long_short_policy", LongShortConflictPolicy),
            ("multi_setup_policy", MultiSetupPolicy),
            ("existing_position_policy", ExistingPositionPolicy),
            ("pending_order_policy", PendingOrderPolicy),
            ("active_plan_policy", ActivePlanPolicy),
        ):
            if not isinstance(getattr(self, name), enum_type):
                raise RouterConfigurationError(f"{name} must be {enum_type.__name__}")
        for name in (
            "strict_mode",
            "require_portfolio_snapshot",
            "allow_risk_reducing_when_locked",
            "warning_escalation",
        ):
            if not isinstance(getattr(self, name), bool):
                raise RouterConfigurationError(f"{name} must be boolean")
        collection_limit_fields = {
            "max_entry_candidates",
            "max_decisions_per_type",
            "max_portfolio_positions",
            "max_pending_orders",
            "max_targets_per_plan",
        }
        for name in (
            "max_decision_age_seconds",
            "max_snapshot_age_seconds",
            "plan_ttl_seconds",
            *sorted(collection_limit_fields),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RouterConfigurationError(f"{name} must be a positive integer")
            maximum = (
                MAX_ROUTER_COLLECTION_ITEMS
                if name in collection_limit_fields
                else MAX_CONFIGURATION_POSITIVE_INT
            )
            if value > maximum:
                raise RouterConfigurationError(f"{name} must be in range [1, {maximum}]")
        risk_fraction = _decimal(
            self.max_portfolio_risk_fraction,
            "max_portfolio_risk_fraction",
        )
        symbol_fraction = _decimal(
            self.max_symbol_notional_fraction,
            "max_symbol_notional_fraction",
        )
        object.__setattr__(self, "max_portfolio_risk_fraction", risk_fraction)
        object.__setattr__(self, "max_symbol_notional_fraction", symbol_fraction)
        if not 0 < risk_fraction <= 1:
            raise RouterConfigurationError("max_portfolio_risk_fraction must be in (0, 1]")
        if symbol_fraction <= 0:
            raise RouterConfigurationError("max_symbol_notional_fraction must be positive")
        if not isinstance(self.audit_level, str) or self.audit_level not in {"minimal", "full"}:
            raise RouterConfigurationError("audit_level must be 'minimal' or 'full'")
        for name in (
            "portfolio_priorities",
            "strategy_priorities",
            "setup_priorities",
            "module_priorities",
            "side_priorities",
        ):
            object.__setattr__(self, name, _priority_map(getattr(self, name), name))
        invalid_side_keys = sorted(set(self.side_priorities) - {side.value for side in TradeSide})
        if invalid_side_keys:
            raise RouterConfigurationError(
                "side_priorities contains unsupported keys: " + ", ".join(invalid_side_keys)
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RouterConfiguration":
        if value is None:
            return cls()
        snapshot = _mapping_snapshot(
            value,
            "Router configuration",
            max_entries=MAX_CONFIGURATION_FIELDS,
        )
        allowed = {
            "schema_version",
            "long_short_policy",
            "multi_setup_policy",
            "existing_position_policy",
            "pending_order_policy",
            "active_plan_policy",
            "strict_mode",
            "require_portfolio_snapshot",
            "allow_risk_reducing_when_locked",
            "max_decision_age_seconds",
            "max_snapshot_age_seconds",
            "plan_ttl_seconds",
            "max_portfolio_risk_fraction",
            "max_symbol_notional_fraction",
            "portfolio_priorities",
            "strategy_priorities",
            "setup_priorities",
            "module_priorities",
            "side_priorities",
            "warning_escalation",
            "audit_level",
            "max_entry_candidates",
            "max_decisions_per_type",
            "max_portfolio_positions",
            "max_pending_orders",
            "max_targets_per_plan",
        }
        unknown = sorted(set(snapshot) - allowed)
        if unknown:
            raise RouterConfigurationError(
                f"unknown Router configuration fields: {', '.join(unknown)}"
            )
        bool_fields = {
            "strict_mode",
            "require_portfolio_snapshot",
            "allow_risk_reducing_when_locked",
            "warning_escalation",
        }
        for name in bool_fields:
            if name in snapshot and not isinstance(snapshot[name], bool):
                raise RouterConfigurationError(f"{name} must be boolean")
        schema_version = snapshot.get("schema_version", "1")
        if not isinstance(schema_version, str):
            raise RouterConfigurationError("schema_version must be a string")
        audit_level = snapshot.get("audit_level", "full")
        if not isinstance(audit_level, str):
            raise RouterConfigurationError("audit_level must be a string")
        return cls(
            schema_version=schema_version,
            long_short_policy=_enum(
                LongShortConflictPolicy,
                snapshot.get("long_short_policy"),
                "long_short_policy",
                LongShortConflictPolicy.REJECT_ALL.value,
            ),
            multi_setup_policy=_enum(
                MultiSetupPolicy,
                snapshot.get("multi_setup_policy"),
                "multi_setup_policy",
                MultiSetupPolicy.HIGHEST_PRIORITY.value,
            ),
            existing_position_policy=_enum(
                ExistingPositionPolicy,
                snapshot.get("existing_position_policy"),
                "existing_position_policy",
                ExistingPositionPolicy.REJECT.value,
            ),
            pending_order_policy=_enum(
                PendingOrderPolicy,
                snapshot.get("pending_order_policy"),
                "pending_order_policy",
                PendingOrderPolicy.REJECT.value,
            ),
            active_plan_policy=_enum(
                ActivePlanPolicy,
                snapshot.get("active_plan_policy"),
                "active_plan_policy",
                ActivePlanPolicy.REJECT_NEW_EXPOSURE.value,
            ),
            strict_mode=bool(snapshot.get("strict_mode", True)),
            require_portfolio_snapshot=bool(snapshot.get("require_portfolio_snapshot", False)),
            allow_risk_reducing_when_locked=bool(
                snapshot.get("allow_risk_reducing_when_locked", True)
            ),
            max_decision_age_seconds=_positive_int(
                snapshot.get("max_decision_age_seconds"), "max_decision_age_seconds", 86_400
            ),
            max_snapshot_age_seconds=_positive_int(
                snapshot.get("max_snapshot_age_seconds"), "max_snapshot_age_seconds", 300
            ),
            plan_ttl_seconds=_positive_int(
                snapshot.get("plan_ttl_seconds"), "plan_ttl_seconds", 300
            ),
            max_portfolio_risk_fraction=_decimal(
                snapshot.get("max_portfolio_risk_fraction", "0.03"), "max_portfolio_risk_fraction"
            ),
            max_symbol_notional_fraction=_decimal(
                snapshot.get("max_symbol_notional_fraction", "3"), "max_symbol_notional_fraction"
            ),
            max_entry_candidates=_positive_int(
                snapshot.get("max_entry_candidates"),
                "max_entry_candidates",
                1_000,
                maximum=MAX_ROUTER_COLLECTION_ITEMS,
            ),
            max_decisions_per_type=_positive_int(
                snapshot.get("max_decisions_per_type"),
                "max_decisions_per_type",
                2_000,
                maximum=MAX_ROUTER_COLLECTION_ITEMS,
            ),
            max_portfolio_positions=_positive_int(
                snapshot.get("max_portfolio_positions"),
                "max_portfolio_positions",
                5_000,
                maximum=MAX_ROUTER_COLLECTION_ITEMS,
            ),
            max_pending_orders=_positive_int(
                snapshot.get("max_pending_orders"),
                "max_pending_orders",
                10_000,
                maximum=MAX_ROUTER_COLLECTION_ITEMS,
            ),
            max_targets_per_plan=_positive_int(
                snapshot.get("max_targets_per_plan"),
                "max_targets_per_plan",
                100,
                maximum=MAX_ROUTER_COLLECTION_ITEMS,
            ),
            portfolio_priorities=_priority_map(
                snapshot.get("portfolio_priorities"), "portfolio_priorities"
            ),
            strategy_priorities=_priority_map(
                snapshot.get("strategy_priorities"), "strategy_priorities"
            ),
            setup_priorities=_priority_map(snapshot.get("setup_priorities"), "setup_priorities"),
            module_priorities=_priority_map(snapshot.get("module_priorities"), "module_priorities"),
            side_priorities=_priority_map(snapshot.get("side_priorities"), "side_priorities"),
            warning_escalation=bool(snapshot.get("warning_escalation", False)),
            audit_level=audit_level,
        )

    def canonical_mapping(self, *, include_observability: bool = True) -> Mapping[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "long_short_policy": self.long_short_policy.value,
            "multi_setup_policy": self.multi_setup_policy.value,
            "existing_position_policy": self.existing_position_policy.value,
            "pending_order_policy": self.pending_order_policy.value,
            "active_plan_policy": self.active_plan_policy.value,
            "strict_mode": self.strict_mode,
            "require_portfolio_snapshot": self.require_portfolio_snapshot,
            "allow_risk_reducing_when_locked": self.allow_risk_reducing_when_locked,
            "max_decision_age_seconds": self.max_decision_age_seconds,
            "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
            "plan_ttl_seconds": self.plan_ttl_seconds,
            "max_portfolio_risk_fraction": self.max_portfolio_risk_fraction,
            "max_symbol_notional_fraction": self.max_symbol_notional_fraction,
            "max_entry_candidates": self.max_entry_candidates,
            "max_decisions_per_type": self.max_decisions_per_type,
            "max_portfolio_positions": self.max_portfolio_positions,
            "max_pending_orders": self.max_pending_orders,
            "max_targets_per_plan": self.max_targets_per_plan,
            "portfolio_priorities": self.portfolio_priorities,
            "strategy_priorities": self.strategy_priorities,
            "setup_priorities": self.setup_priorities,
            "module_priorities": self.module_priorities,
            "side_priorities": self.side_priorities,
            "warning_escalation": self.warning_escalation,
        }
        if include_observability:
            result["audit_level"] = self.audit_level
        return MappingProxyType(result)

    @property
    def fingerprint(self) -> str:
        """Full configuration fingerprint, including observability settings."""
        return sha256_hex(self.canonical_mapping(include_observability=True))

    @property
    def semantic_fingerprint(self) -> str:
        """Trading-semantic fingerprint; audit verbosity is intentionally excluded."""
        return sha256_hex(self.canonical_mapping(include_observability=False))

    def priority_for(
        self, *, portfolio_id: str, strategy_id: str, setup_id: str, module_id: str, side: TradeSide
    ) -> tuple[int, int, int, int, int]:
        return (
            self.portfolio_priorities.get(portfolio_id, 0),
            self.strategy_priorities.get(strategy_id, 0),
            self.setup_priorities.get(setup_id, 0),
            self.module_priorities.get(module_id, 0),
            self.side_priorities.get(side.value, 0),
        )
