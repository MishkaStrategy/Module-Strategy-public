from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType

SCHEMA_VERSION = "1"
MAX_REASON_CODES = 32
MAX_CORRELATION_CHARS = 128
MAX_PUBLIC_DETAILS = 32
MAX_PUBLIC_DETAIL_STRING_CHARS = 256
MAX_PUBLIC_DETAIL_INTEGER = 2**63 - 1
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_DETAIL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

type PublicDetailValue = str | int | float | bool | None


class Component(StrEnum):
    MARKET_DATA = "market_data"
    DATASET_REGISTRY = "dataset_registry"
    PORTFOLIO_POSITION = "portfolio_position"
    RISK_SIZING = "risk_sizing"
    RISK_SUPERVISOR = "risk_supervisor"
    TRADING_RUNNER = "trading_runner"
    PAPER_EXECUTION = "paper_execution"
    BACKTEST_RESEARCH = "backtest_research"
    STRATEGY_AUDIT_FACTORY = "strategy_audit_factory"
    SIGNAL_BOT = "signal_bot"
    SYSTEM_ACCEPTANCE_RECOVERY = "system_acceptance_recovery"
    SYSTEM_RESOURCES = "system_resources"


class ComponentHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class AlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class AlertTransition(StrEnum):
    ACTIVE = "active"
    CLEARED = "cleared"


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def reason_code(value: object, name: str = "reason_code") -> str:
    if not isinstance(value, str) or _REASON_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable lowercase identifier")
    return value


def state_ref(value: object, name: str = "state_ref") -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def correlation_id(value: object, name: str = "correlation_id") -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CORRELATION_CHARS:
        raise ValueError(f"{name} must be a non-empty bounded public identifier")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if any(character not in allowed for character in value):
        raise ValueError(f"{name} contains unsafe characters")
    return value


def canonical_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def opaque_state_ref(value: Mapping[str, object]) -> str:
    """Return a public opaque digest without exposing owner-private raw state."""

    return canonical_hash(value)


def _reason_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > MAX_REASON_CODES:
        raise ValueError("reason_codes exceeds bounded item limit")
    normalized = tuple(reason_code(value, "reason_codes item") for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("reason_codes cannot contain duplicates")
    return tuple(sorted(normalized))


def _public_details(
    values: Mapping[str, PublicDetailValue],
) -> Mapping[str, PublicDetailValue]:
    if not isinstance(values, Mapping):
        raise TypeError("public_details must be a mapping")
    if len(values) > MAX_PUBLIC_DETAILS:
        raise ValueError("public_details exceeds bounded item limit")
    normalized: dict[str, PublicDetailValue] = {}
    for key, value in values.items():
        if not isinstance(key, str) or _DETAIL_KEY_RE.fullmatch(key) is None:
            raise ValueError("public_details keys must be stable lowercase identifiers")
        if isinstance(value, bool) or value is None:
            normalized[key] = value
        elif isinstance(value, int):
            if abs(value) > MAX_PUBLIC_DETAIL_INTEGER:
                raise ValueError("public_details integer exceeds signed 64-bit bound")
            normalized[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("public_details float must be finite")
            normalized[key] = value
        elif isinstance(value, str):
            if (
                not value
                or value != value.strip()
                or len(value) > MAX_PUBLIC_DETAIL_STRING_CHARS
                or not value.isprintable()
            ):
                raise ValueError("public_details string must be bounded normalized printable text")
            normalized[key] = value
        else:
            raise TypeError("public_details values must be scalar public values")
    return MappingProxyType(dict(sorted(normalized.items())))


def _positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if result <= 0 or not math.isfinite(result):
        raise ValueError(f"{name} must be positive and finite")
    return result


def _nonnegative_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise ValueError(f"{name} must be non-negative and finite")
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class ObservabilityPolicy:
    freshness_seconds: Mapping[Component, float]
    required_components: frozenset[Component]
    critical_components: frozenset[Component]
    stuck_seconds: Mapping[Component, float] = field(default_factory=dict)
    maximum_future_skew_seconds: float = 2.0
    alert_cooldown_seconds: float = 300.0
    alert_state_capacity: int = 64
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported policy schema_version")
        if not isinstance(self.freshness_seconds, Mapping):
            raise TypeError("freshness_seconds must be a mapping")
        freshness: dict[Component, float] = {}
        for component, value in self.freshness_seconds.items():
            if not isinstance(component, Component):
                raise TypeError("freshness_seconds keys must be Component values")
            freshness[component] = _positive_finite(value, f"freshness_seconds[{component.value}]")
        if not isinstance(self.stuck_seconds, Mapping):
            raise TypeError("stuck_seconds must be a mapping")
        stuck: dict[Component, float] = {}
        for component, value in self.stuck_seconds.items():
            if not isinstance(component, Component):
                raise TypeError("stuck_seconds keys must be Component values")
            stuck[component] = _positive_finite(value, f"stuck_seconds[{component.value}]")
        required = frozenset(self.required_components)
        critical = frozenset(self.critical_components)
        if any(not isinstance(component, Component) for component in required | critical):
            raise TypeError("component sets must contain Component values")
        if not required:
            raise ValueError("required_components cannot be empty")
        if not critical.issubset(required):
            raise ValueError("critical_components must be a subset of required_components")
        missing = required - set(freshness)
        if missing:
            names = ", ".join(sorted(component.value for component in missing))
            raise ValueError(f"freshness policy missing required components: {names}")
        if not set(stuck).issubset(required):
            raise ValueError("stuck_seconds keys must be required components")
        object.__setattr__(self, "freshness_seconds", MappingProxyType(freshness))
        object.__setattr__(self, "stuck_seconds", MappingProxyType(stuck))
        object.__setattr__(self, "required_components", required)
        object.__setattr__(self, "critical_components", critical)
        object.__setattr__(
            self,
            "maximum_future_skew_seconds",
            _nonnegative_finite(self.maximum_future_skew_seconds, "maximum_future_skew_seconds"),
        )
        object.__setattr__(
            self,
            "alert_cooldown_seconds",
            _positive_finite(self.alert_cooldown_seconds, "alert_cooldown_seconds"),
        )
        object.__setattr__(
            self,
            "alert_state_capacity",
            _positive_int(self.alert_state_capacity, "alert_state_capacity"),
        )
        if self.alert_state_capacity < len(required):
            raise ValueError("alert_state_capacity must cover every required component")

    @classmethod
    def paper_default(cls, *, freshness_seconds: float = 30.0) -> ObservabilityPolicy:
        threshold = _positive_finite(freshness_seconds, "freshness_seconds")
        required = frozenset(Component)
        critical = frozenset(
            {
                Component.MARKET_DATA,
                Component.PORTFOLIO_POSITION,
                Component.RISK_SIZING,
                Component.RISK_SUPERVISOR,
                Component.TRADING_RUNNER,
                Component.PAPER_EXECUTION,
                Component.STRATEGY_AUDIT_FACTORY,
                Component.SYSTEM_ACCEPTANCE_RECOVERY,
                Component.SYSTEM_RESOURCES,
            }
        )
        return cls(
            freshness_seconds={component: threshold for component in required},
            stuck_seconds={
                Component.BACKTEST_RESEARCH: threshold * 10,
                Component.STRATEGY_AUDIT_FACTORY: threshold * 10,
            },
            required_components=required,
            critical_components=critical,
        )


@dataclass(frozen=True, slots=True)
class SourceObservation:
    component: Component
    health: ComponentHealth
    observed_at: datetime
    state_ref: str
    reason_codes: tuple[str, ...] = ()
    correlation_id: str | None = None
    progress_at: datetime | None = None
    public_details: Mapping[str, PublicDetailValue] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported source observation schema_version")
        if not isinstance(self.component, Component):
            raise TypeError("component must be Component")
        if not isinstance(self.health, ComponentHealth):
            raise TypeError("health must be ComponentHealth")
        object.__setattr__(self, "observed_at", utc(self.observed_at, "observed_at"))
        if self.progress_at is not None:
            object.__setattr__(self, "progress_at", utc(self.progress_at, "progress_at"))
        state_ref(self.state_ref)
        reasons = _reason_tuple(self.reason_codes)
        if self.health is ComponentHealth.HEALTHY and reasons:
            raise ValueError("healthy source observation cannot carry fault reason codes")
        if self.health is not ComponentHealth.HEALTHY and not reasons:
            raise ValueError("non-healthy source observation requires reason_codes")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "public_details", _public_details(self.public_details))
        if self.correlation_id is not None:
            correlation_id(self.correlation_id)


@dataclass(frozen=True, slots=True)
class SourceRead:
    component: Component
    observation: SourceObservation | None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.component, Component):
            raise TypeError("component must be Component")
        if self.observation is None:
            if self.unavailable_reason is None:
                raise ValueError("missing observation requires unavailable_reason")
            reason_code(self.unavailable_reason, "unavailable_reason")
        else:
            if self.observation.component is not self.component:
                raise ValueError("SourceRead component mismatch")
            if self.unavailable_reason is not None:
                raise ValueError("available observation cannot carry unavailable_reason")


@dataclass(frozen=True, slots=True)
class ComponentObservation:
    component: Component
    health: ComponentHealth
    observed_at: datetime | None
    evaluated_at: datetime
    age_seconds: float | None
    stale: bool
    progress_at: datetime | None
    progress_age_seconds: float | None
    stuck: bool
    state_ref: str | None
    reason_codes: tuple[str, ...]
    correlation_id: str | None = None
    public_details: Mapping[str, PublicDetailValue] = field(default_factory=dict)
    observation_hash: str = field(init=False)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported component observation schema_version")
        if not isinstance(self.component, Component) or not isinstance(
            self.health, ComponentHealth
        ):
            raise TypeError("invalid component observation enum")
        evaluated = utc(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "evaluated_at", evaluated)
        observed = None if self.observed_at is None else utc(self.observed_at, "observed_at")
        progress = None if self.progress_at is None else utc(self.progress_at, "progress_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "progress_at", progress)
        if self.age_seconds is None:
            if observed is not None:
                raise ValueError("age_seconds is required when observed_at is present")
        else:
            age = _nonnegative_finite(self.age_seconds, "age_seconds")
            if observed is None:
                raise ValueError("age_seconds requires observed_at")
            object.__setattr__(self, "age_seconds", age)
        if self.progress_age_seconds is None:
            if progress is not None:
                raise ValueError("progress_age_seconds is required when progress_at is present")
        else:
            progress_age = _nonnegative_finite(self.progress_age_seconds, "progress_age_seconds")
            if progress is None:
                raise ValueError("progress_age_seconds requires progress_at")
            object.__setattr__(self, "progress_age_seconds", progress_age)
        if not isinstance(self.stale, bool) or not isinstance(self.stuck, bool):
            raise TypeError("stale and stuck must be bool")
        if self.stuck and progress is None:
            raise ValueError("stuck observation requires progress_at")
        if self.state_ref is not None:
            state_ref(self.state_ref)
        reasons = _reason_tuple(self.reason_codes)
        if self.health is ComponentHealth.HEALTHY and reasons:
            raise ValueError("healthy observation cannot carry fault reason codes")
        if self.health is not ComponentHealth.HEALTHY and not reasons:
            raise ValueError("non-healthy observation requires reason_codes")
        details = _public_details(self.public_details)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "public_details", details)
        if self.correlation_id is not None:
            correlation_id(self.correlation_id)
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "component": self.component.value,
            "health": self.health.value,
            "observed_at": observed.isoformat() if observed else None,
            "evaluated_at": evaluated.isoformat(),
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "progress_at": progress.isoformat() if progress else None,
            "progress_age_seconds": self.progress_age_seconds,
            "stuck": self.stuck,
            "state_ref": self.state_ref,
            "reason_codes": list(reasons),
            "correlation_id": self.correlation_id,
            "public_details": dict(details),
        }
        object.__setattr__(self, "observation_hash", canonical_hash(payload))


@dataclass(frozen=True, slots=True)
class OperationalOverview:
    evaluated_at: datetime
    overall_health: ComponentHealth
    components: tuple[ComponentObservation, ...]
    overview_hash: str = field(init=False)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported overview schema_version")
        if not isinstance(self.overall_health, ComponentHealth):
            raise TypeError("overall_health must be ComponentHealth")
        evaluated = utc(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "evaluated_at", evaluated)
        components = tuple(self.components)
        if any(not isinstance(item, ComponentObservation) for item in components):
            raise TypeError("components must contain ComponentObservation values")
        if not components:
            raise ValueError("overview must contain at least one component")
        identifiers = [item.component for item in components]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("overview cannot contain duplicate components")
        if identifiers != sorted(identifiers, key=lambda item: item.value):
            raise ValueError("overview components must be sorted")
        if any(item.evaluated_at != evaluated for item in components):
            raise ValueError("component evaluated_at must match overview evaluated_at")
        healths = {item.health for item in components}
        if ComponentHealth.UNAVAILABLE in healths:
            expected_health = ComponentHealth.UNAVAILABLE
        elif ComponentHealth.BLOCKED in healths:
            expected_health = ComponentHealth.BLOCKED
        elif ComponentHealth.DEGRADED in healths:
            expected_health = ComponentHealth.DEGRADED
        else:
            expected_health = ComponentHealth.HEALTHY
        if self.overall_health is not expected_health:
            raise ValueError("overall_health must match component health")
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "evaluated_at": evaluated.isoformat(),
            "overall_health": self.overall_health.value,
            "component_hashes": [item.observation_hash for item in components],
        }
        object.__setattr__(self, "overview_hash", canonical_hash(payload))


@dataclass(frozen=True, slots=True)
class OperatorAlert:
    alert_id: str
    event_id: str
    component: Component
    severity: AlertSeverity
    transition: AlertTransition
    reason_codes: tuple[str, ...]
    next_action: str
    evaluated_at: datetime
    source_observation_hash: str
    correlation_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported alert schema_version")
        state_ref(self.alert_id, "alert_id")
        state_ref(self.event_id, "event_id")
        state_ref(self.source_observation_hash, "source_observation_hash")
        if not isinstance(self.component, Component):
            raise TypeError("component must be Component")
        if not isinstance(self.severity, AlertSeverity) or not isinstance(
            self.transition, AlertTransition
        ):
            raise TypeError("invalid alert enum")
        reasons = _reason_tuple(self.reason_codes)
        if not reasons:
            raise ValueError("alert requires reason_codes")
        object.__setattr__(self, "reason_codes", reasons)
        reason_code(self.next_action, "next_action")
        object.__setattr__(self, "evaluated_at", utc(self.evaluated_at, "evaluated_at"))
        if self.correlation_id is not None:
            correlation_id(self.correlation_id)
