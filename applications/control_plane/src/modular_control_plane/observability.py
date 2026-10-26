from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel, SafeScalar

_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_DETAIL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PUBLIC_DETAILS = 32
_MAX_PUBLIC_DETAIL_STRING_CHARS = 256
_MAX_PUBLIC_DETAIL_INTEGER = 2**53 - 1


class ObservabilityComponentId(StrEnum):
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


class ObservabilityHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class ObservabilityAlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class ObservabilityComponentView(ContractModel):
    component: ObservabilityComponentId
    health: ObservabilityHealth
    observed_at: datetime | None
    evaluated_at: datetime
    age_seconds: float | None = Field(default=None, ge=0.0)
    stale: bool
    progress_at: datetime | None
    progress_age_seconds: float | None = Field(default=None, ge=0.0)
    stuck: bool
    state_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)
    public_details: dict[str, SafeScalar] = Field(default_factory=dict)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at", "evaluated_at", "progress_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observability timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_REASON_RE.fullmatch(reason) is None for reason in value):
            raise ValueError("reason_codes must contain stable lowercase identifiers")
        if len(set(value)) != len(value):
            raise ValueError("reason_codes cannot contain duplicates")
        return tuple(sorted(value))

    @field_validator("correlation_id")
    @classmethod
    def validate_correlation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
        if any(character not in allowed for character in value):
            raise ValueError("correlation_id contains unsupported characters")
        return value

    @field_validator("public_details")
    @classmethod
    def validate_public_details(cls, value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        if len(value) > _MAX_PUBLIC_DETAILS:
            raise ValueError("public_details exceeds bounded item limit")
        normalized: dict[str, SafeScalar] = {}
        for key, item in value.items():
            if _DETAIL_KEY_RE.fullmatch(key) is None:
                raise ValueError("public_details keys must be stable lowercase identifiers")
            if isinstance(item, str):
                if len(item) > _MAX_PUBLIC_DETAIL_STRING_CHARS:
                    raise ValueError("public_details string exceeds bounded length")
                if any(ord(character) < 32 or ord(character) == 127 for character in item):
                    raise ValueError("public_details strings cannot contain control characters")
            elif isinstance(item, bool) or item is None:
                pass
            elif isinstance(item, int):
                if abs(item) > _MAX_PUBLIC_DETAIL_INTEGER:
                    raise ValueError("public_details integer exceeds JSON safe-integer boundary")
            elif isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("public_details float must be finite")
            else:
                raise TypeError("public_details values must be safe scalars")
            normalized[key] = item
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def validate_shape(self) -> ObservabilityComponentView:
        if self.observed_at is None and self.age_seconds is not None:
            raise ValueError("age_seconds requires observed_at")
        if self.observed_at is not None and self.age_seconds is None:
            raise ValueError("observed_at requires age_seconds")
        if self.progress_at is None and self.progress_age_seconds is not None:
            raise ValueError("progress_age_seconds requires progress_at")
        if self.progress_at is not None and self.progress_age_seconds is None:
            raise ValueError("progress_at requires progress_age_seconds")
        if self.stuck and self.progress_at is None:
            raise ValueError("stuck component requires progress_at")
        if self.health is ObservabilityHealth.HEALTHY and self.reason_codes:
            raise ValueError("healthy component cannot carry fault reason_codes")
        if self.health is not ObservabilityHealth.HEALTHY and not self.reason_codes:
            raise ValueError("non-healthy component requires reason_codes")
        return self


class UnifiedOperationalOverview(ContractModel):
    evaluated_at: datetime
    overall_health: ObservabilityHealth
    overview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    components: tuple[ObservabilityComponentView, ...] = Field(min_length=1, max_length=32)

    @field_validator("evaluated_at")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_overview(self) -> UnifiedOperationalOverview:
        expected_components = tuple(ObservabilityComponentId)
        identifiers = tuple(item.component for item in self.components)
        if identifiers != tuple(sorted(expected_components, key=str)):
            raise ValueError("overview must contain the exact sorted component set")
        if any(item.evaluated_at != self.evaluated_at for item in self.components):
            raise ValueError("component evaluated_at must match overview evaluated_at")
        healths = {item.health for item in self.components}
        if ObservabilityHealth.UNAVAILABLE in healths:
            expected_health = ObservabilityHealth.UNAVAILABLE
        elif ObservabilityHealth.BLOCKED in healths:
            expected_health = ObservabilityHealth.BLOCKED
        elif ObservabilityHealth.DEGRADED in healths:
            expected_health = ObservabilityHealth.DEGRADED
        else:
            expected_health = ObservabilityHealth.HEALTHY
        if self.overall_health is not expected_health:
            raise ValueError("overall_health must match component health")
        return self


class ActiveObservabilityAlert(ContractModel):
    alert_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    component: ObservabilityComponentId
    severity: ObservabilityAlertSeverity
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    next_action: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$",
    )
    evaluated_at: datetime
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_REASON_RE.fullmatch(reason) is None for reason in value):
            raise ValueError("reason_codes must contain stable lowercase identifiers")
        if len(set(value)) != len(value):
            raise ValueError("reason_codes cannot contain duplicates")
        return tuple(sorted(value))

    @field_validator("evaluated_at")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value.astimezone(UTC)


class UnifiedObservabilitySnapshot(ContractModel):
    mode: Literal["PAPER"] = "PAPER"
    overview: UnifiedOperationalOverview
    active_alerts: tuple[ActiveObservabilityAlert, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_alerts(self) -> UnifiedObservabilitySnapshot:
        components = {item.component: item for item in self.overview.components}
        identifiers: set[str] = set()
        alert_components: set[ObservabilityComponentId] = set()
        for alert in self.active_alerts:
            if alert.alert_id in identifiers:
                raise ValueError("active_alerts cannot contain duplicate alert_id values")
            if alert.component in alert_components:
                raise ValueError("active_alerts cannot contain duplicate components")
            identifiers.add(alert.alert_id)
            alert_components.add(alert.component)
            component = components[alert.component]
            if component.health is ObservabilityHealth.HEALTHY:
                raise ValueError("healthy component cannot have an active alert")
            if alert.source_observation_hash != component.observation_hash:
                raise ValueError("active alert must reference the current component observation")
            if alert.evaluated_at != component.evaluated_at:
                raise ValueError(
                    "active alert evaluated_at must match current component evaluation"
                )
        return self


def validate_backend_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value
