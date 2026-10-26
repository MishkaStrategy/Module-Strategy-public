from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType

SCHEMA_VERSION = "1"
MAX_ID_CHARS = 256
MAX_SOURCE_STATES = 128


class SafetyState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    HALT = "halt"
    MANUAL_REVIEW = "manual_review"


class RiskGuardCode(StrEnum):
    SUPERVISOR_UNINITIALIZED = "supervisor_uninitialized"
    STATE_INVALID = "state_invalid"
    SNAPSHOT_ID_CONTENT_MISMATCH = "snapshot_id_content_mismatch"
    SNAPSHOT_OUT_OF_ORDER = "snapshot_out_of_order"
    CLOCK_ANOMALY = "clock_anomaly"
    EVIDENCE_STALE = "evidence_stale"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DRAWDOWN_LIMIT = "drawdown_limit"
    EXPOSURE_NOTIONAL_LIMIT = "exposure_notional_limit"
    LEVERAGE_LIMIT = "leverage_limit"
    MARKET_DATA_STALE = "market_data_stale"
    CONSUMER_STALLED = "consumer_stalled"
    RECONCILIATION_BLOCKED = "reconciliation_blocked"
    RECONCILIATION_LOSS = "reconciliation_loss"
    REJECTION_RATE_LIMIT = "rejection_rate_limit"
    CANCEL_FAILURE_RATE_LIMIT = "cancel_failure_rate_limit"
    SLIPPAGE_LIMIT = "slippage_limit"
    FILL_QUALITY_LIMIT = "fill_quality_limit"
    CHECKPOINT_INTEGRITY_FAILURE = "checkpoint_integrity_failure"
    RESOURCE_PRESSURE = "resource_pressure"
    TRANSPORT_UNHEALTHY = "transport_unhealthy"
    EXECUTION_LATENCY_LIMIT = "execution_latency_limit"
    OPERATOR_EMERGENCY_HALT = "operator_emergency_halt"
    RECOVERY_REQUIRED = "recovery_required"
    OPERATOR_AUTH_FAILED = "operator_auth_failed"
    OPERATOR_COMMAND_REPLAY_MISMATCH = "operator_command_replay_mismatch"


class OperatorAction(StrEnum):
    HALT = "halt"
    RECOVER = "recover"


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def decimal(value: object, name: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be decimal-compatible")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def nonnegative_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if result < 0 or result == float("inf"):
        raise ValueError(f"{name} must be non-negative and finite")
    return result


def identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip() or len(value) > MAX_ID_CHARS:
        raise ValueError(f"{name} must be normalized and <= {MAX_ID_CHARS} chars")
    return value


def canonical_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskSupervisorPolicy:
    maximum_daily_loss_fraction: Decimal = Decimal("0.03")
    maximum_drawdown_fraction: Decimal = Decimal("0.06")
    maximum_exposure_notional: Decimal = Decimal("1000000")
    maximum_gross_leverage: Decimal = Decimal("3")
    maximum_rejection_rate_fraction: Decimal = Decimal("0.20")
    maximum_cancel_failure_rate_fraction: Decimal = Decimal("0.10")
    minimum_execution_samples: int = 10
    maximum_mean_abs_slippage_bps: Decimal = Decimal("25")
    minimum_fill_quality_fraction: Decimal = Decimal("0.80")
    maximum_evidence_age_seconds: float = 15.0
    maximum_market_data_age_seconds: float = 15.0
    maximum_consumer_stall_seconds: float = 30.0
    maximum_execution_latency_seconds: float = 5.0
    minimum_disk_free_bytes: int = 1_073_741_824
    maximum_future_skew_seconds: float = 2.0
    mandatory_guards: frozenset[RiskGuardCode] = field(
        default_factory=lambda: frozenset(
            {
                RiskGuardCode.EVIDENCE_STALE,
                RiskGuardCode.DAILY_LOSS_LIMIT,
                RiskGuardCode.DRAWDOWN_LIMIT,
                RiskGuardCode.EXPOSURE_NOTIONAL_LIMIT,
                RiskGuardCode.LEVERAGE_LIMIT,
                RiskGuardCode.MARKET_DATA_STALE,
                RiskGuardCode.CONSUMER_STALLED,
                RiskGuardCode.RECONCILIATION_BLOCKED,
                RiskGuardCode.RECONCILIATION_LOSS,
                RiskGuardCode.REJECTION_RATE_LIMIT,
                RiskGuardCode.CANCEL_FAILURE_RATE_LIMIT,
                RiskGuardCode.SLIPPAGE_LIMIT,
                RiskGuardCode.FILL_QUALITY_LIMIT,
                RiskGuardCode.CHECKPOINT_INTEGRITY_FAILURE,
                RiskGuardCode.RESOURCE_PRESSURE,
                RiskGuardCode.TRANSPORT_UNHEALTHY,
                RiskGuardCode.EXECUTION_LATENCY_LIMIT,
            }
        )
    )

    def __post_init__(self) -> None:
        fraction_names = (
            "maximum_daily_loss_fraction",
            "maximum_drawdown_fraction",
            "maximum_rejection_rate_fraction",
            "maximum_cancel_failure_rate_fraction",
            "minimum_fill_quality_fraction",
        )
        for name in fraction_names:
            fraction_value = decimal(getattr(self, name), name, minimum=Decimal(0))
            if fraction_value > 1:
                raise ValueError(f"{name} must be <= 1")
            object.__setattr__(self, name, fraction_value)
        for name in (
            "maximum_exposure_notional",
            "maximum_gross_leverage",
            "maximum_mean_abs_slippage_bps",
        ):
            positive_decimal_value = decimal(getattr(self, name), name, minimum=Decimal(0))
            if positive_decimal_value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, positive_decimal_value)
        minimum_execution_samples = nonnegative_int(
            self.minimum_execution_samples, "minimum_execution_samples"
        )
        if minimum_execution_samples < 1:
            raise ValueError("minimum_execution_samples must be positive")
        object.__setattr__(self, "minimum_execution_samples", minimum_execution_samples)
        disk = nonnegative_int(self.minimum_disk_free_bytes, "minimum_disk_free_bytes")
        if disk < 1:
            raise ValueError("minimum_disk_free_bytes must be positive")
        object.__setattr__(self, "minimum_disk_free_bytes", disk)
        for name in (
            "maximum_evidence_age_seconds",
            "maximum_market_data_age_seconds",
            "maximum_consumer_stall_seconds",
            "maximum_execution_latency_seconds",
        ):
            positive_float_value = nonnegative_float(getattr(self, name), name)
            if positive_float_value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, positive_float_value)
        object.__setattr__(
            self,
            "maximum_future_skew_seconds",
            nonnegative_float(self.maximum_future_skew_seconds, "maximum_future_skew_seconds"),
        )
        mandatory = frozenset(self.mandatory_guards)
        if any(not isinstance(code, RiskGuardCode) for code in mandatory):
            raise TypeError("mandatory_guards must contain RiskGuardCode values")
        object.__setattr__(self, "mandatory_guards", mandatory)


@dataclass(frozen=True, slots=True)
class AuthoritativeRiskEvidence:
    snapshot_id: str
    observed_at: datetime
    source_state_ids: Mapping[str, str]
    session_start_equity: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    aggregate_exposure_notional: Decimal
    market_data_observed_at: datetime
    consumer_progress_at: datetime
    reconciliation_blocked_scopes: int
    reconciliation_loss_count: int
    execution_attempts: int
    execution_rejections: int
    cancel_attempts: int
    cancel_failures: int
    fill_samples: int
    mean_abs_slippage_bps: Decimal
    fill_quality_fraction: Decimal
    checkpoint_integrity_ok: bool
    disk_free_bytes: int
    transport_healthy: bool
    execution_latency_seconds: float
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifier(self.snapshot_id, "snapshot_id")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported evidence schema_version")
        object.__setattr__(self, "observed_at", utc(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "market_data_observed_at",
            utc(self.market_data_observed_at, "market_data_observed_at"),
        )
        object.__setattr__(
            self,
            "consumer_progress_at",
            utc(self.consumer_progress_at, "consumer_progress_at"),
        )
        if not isinstance(self.source_state_ids, Mapping) or not self.source_state_ids:
            raise ValueError("source_state_ids must be a non-empty mapping")
        if len(self.source_state_ids) > MAX_SOURCE_STATES:
            raise ValueError("source_state_ids exceeds item limit")
        state_ids: dict[str, str] = {}
        for key, source_value in self.source_state_ids.items():
            state_ids[identifier(key, "source_state_ids key")] = identifier(
                source_value, "source_state_ids value"
            )
        object.__setattr__(self, "source_state_ids", MappingProxyType(state_ids))
        for name in ("session_start_equity", "current_equity", "peak_equity"):
            equity_value = decimal(getattr(self, name), name)
            if equity_value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, equity_value)
        if self.peak_equity < self.current_equity:
            raise ValueError("peak_equity cannot be below current_equity")
        object.__setattr__(
            self,
            "aggregate_exposure_notional",
            decimal(
                self.aggregate_exposure_notional,
                "aggregate_exposure_notional",
                minimum=Decimal(0),
            ),
        )
        for name in (
            "reconciliation_blocked_scopes",
            "reconciliation_loss_count",
            "execution_attempts",
            "execution_rejections",
            "cancel_attempts",
            "cancel_failures",
            "fill_samples",
            "disk_free_bytes",
        ):
            object.__setattr__(self, name, nonnegative_int(getattr(self, name), name))
        if self.execution_rejections > self.execution_attempts:
            raise ValueError("execution_rejections cannot exceed execution_attempts")
        if self.cancel_failures > self.cancel_attempts:
            raise ValueError("cancel_failures cannot exceed cancel_attempts")
        object.__setattr__(
            self,
            "mean_abs_slippage_bps",
            decimal(self.mean_abs_slippage_bps, "mean_abs_slippage_bps", minimum=Decimal(0)),
        )
        fill_quality = decimal(
            self.fill_quality_fraction,
            "fill_quality_fraction",
            minimum=Decimal(0),
        )
        if fill_quality > 1:
            raise ValueError("fill_quality_fraction must be <= 1")
        object.__setattr__(self, "fill_quality_fraction", fill_quality)
        if not isinstance(self.checkpoint_integrity_ok, bool):
            raise TypeError("checkpoint_integrity_ok must be bool")
        if not isinstance(self.transport_healthy, bool):
            raise TypeError("transport_healthy must be bool")
        object.__setattr__(
            self,
            "execution_latency_seconds",
            nonnegative_float(self.execution_latency_seconds, "execution_latency_seconds"),
        )

    @property
    def gross_leverage(self) -> Decimal:
        return self.aggregate_exposure_notional / self.current_equity

    def canonical_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "snapshot_id": self.snapshot_id,
                "observed_at": self.observed_at.isoformat(),
                "source_state_ids": dict(sorted(self.source_state_ids.items())),
                "session_start_equity": str(self.session_start_equity),
                "current_equity": str(self.current_equity),
                "peak_equity": str(self.peak_equity),
                "aggregate_exposure_notional": str(self.aggregate_exposure_notional),
                "market_data_observed_at": self.market_data_observed_at.isoformat(),
                "consumer_progress_at": self.consumer_progress_at.isoformat(),
                "reconciliation_blocked_scopes": self.reconciliation_blocked_scopes,
                "reconciliation_loss_count": self.reconciliation_loss_count,
                "execution_attempts": self.execution_attempts,
                "execution_rejections": self.execution_rejections,
                "cancel_attempts": self.cancel_attempts,
                "cancel_failures": self.cancel_failures,
                "fill_samples": self.fill_samples,
                "mean_abs_slippage_bps": str(self.mean_abs_slippage_bps),
                "fill_quality_fraction": str(self.fill_quality_fraction),
                "checkpoint_integrity_ok": self.checkpoint_integrity_ok,
                "disk_free_bytes": self.disk_free_bytes,
                "transport_healthy": self.transport_healthy,
                "execution_latency_seconds": self.execution_latency_seconds,
            }
        )

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.canonical_mapping())


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    sequence: int
    state: SafetyState
    evaluated_at: datetime
    guard_codes: tuple[RiskGuardCode, ...]
    snapshot_id: str | None
    snapshot_hash: str | None
    source_state_ids: Mapping[str, str]
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        nonnegative_int(self.sequence, "sequence")
        object.__setattr__(self, "evaluated_at", utc(self.evaluated_at, "evaluated_at"))
        if any(not isinstance(code, RiskGuardCode) for code in self.guard_codes):
            raise TypeError("guard_codes must contain RiskGuardCode values")
        source_ids = dict(self.source_state_ids)
        object.__setattr__(self, "source_state_ids", MappingProxyType(source_ids))
        payload = {
            "sequence": self.sequence,
            "state": self.state.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "guard_codes": [code.value for code in self.guard_codes],
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "source_state_ids": dict(sorted(source_ids.items())),
        }
        object.__setattr__(self, "decision_hash", canonical_hash(payload))

    @property
    def allow_risk_increase(self) -> bool:
        return self.state is SafetyState.HEALTHY


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    command_id: str
    runtime_id: str
    action: OperatorAction
    issued_at: datetime
    signature: str

    def __post_init__(self) -> None:
        identifier(self.command_id, "command_id")
        identifier(self.runtime_id, "runtime_id")
        object.__setattr__(self, "issued_at", utc(self.issued_at, "issued_at"))
        if not isinstance(self.signature, str) or len(self.signature) != 64:
            raise ValueError("signature must be a SHA-256 HMAC hex digest")
        try:
            int(self.signature, 16)
        except ValueError as exc:
            raise ValueError("signature must be hexadecimal") from exc

    def signing_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "command_id": self.command_id,
                "runtime_id": self.runtime_id,
                "action": self.action.value,
                "issued_at": self.issued_at.isoformat(),
            }
        )
