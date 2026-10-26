from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

from .decimal_utils import deterministic_decimal, parse_canonical_decimal_text
from .timeframes import as_utc
from .models_base import (
    FvgEvaluationStatus,
    FvgFilterDecision,
    FvgZone,
    _FILTER_TRACE_ORDER,
    _require_canonical_mapping_keys,
)


@dataclass(frozen=True, slots=True)
class FvgFilterTrace:
    name: str
    decision: FvgFilterDecision
    reason: str
    observed: Decimal | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    metadata: Mapping[str, Any] = MappingProxyType({})

    @deterministic_decimal
    def __post_init__(self) -> None:
        if type(self.name) is not str or type(self.reason) is not str or not self.name or not self.reason:
            raise ValueError("filter trace name and reason must be canonical non-empty strings")
        if not isinstance(self.decision, FvgFilterDecision):
            raise TypeError("filter trace decision must be an FvgFilterDecision")
        for field_name in ("observed", "minimum", "maximum"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not Decimal:
                raise TypeError(f"{field_name} must be a Decimal or null")
            if value is not None and not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("filter trace metadata must be a mapping")
        metadata: dict[str, object] = {}
        for key, value in self.metadata.items():
            if type(key) is not str or not key:
                raise ValueError("filter trace metadata keys must be canonical non-empty strings")
            if value is not None and type(value) not in {str, int}:
                raise ValueError("filter trace metadata values must be canonical strings, ints or null")
            metadata[key] = value
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    def to_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "name": self.name,
                "decision": self.decision.value,
                "reason": self.reason,
                "observed": None if self.observed is None else str(self.observed),
                "minimum": None if self.minimum is None else str(self.minimum),
                "maximum": None if self.maximum is None else str(self.maximum),
                "metadata": dict(self.metadata),
            }
        )

    @classmethod
    def from_value(cls, payload: Mapping[str, object]) -> "FvgFilterTrace":
        payload = _require_canonical_mapping_keys(payload, "filter trace")
        required = {"name", "decision", "reason", "observed", "minimum", "maximum", "metadata"}
        if set(payload) != required or not isinstance(payload["metadata"], Mapping):
            raise ValueError("invalid filter trace payload")
        for name in ("name", "decision", "reason"):
            if type(payload[name]) is not str:
                raise ValueError(f"filter trace field {name} must be a canonical string")
        for name in ("observed", "minimum", "maximum"):
            if payload[name] is not None and type(payload[name]) is not str:
                raise ValueError(f"filter trace field {name} must be a canonical string or null")
        observed = (
            None
            if payload["observed"] is None
            else parse_canonical_decimal_text(payload["observed"], "filter trace observed")
        )
        minimum = (
            None
            if payload["minimum"] is None
            else parse_canonical_decimal_text(payload["minimum"], "filter trace minimum")
        )
        maximum = (
            None
            if payload["maximum"] is None
            else parse_canonical_decimal_text(payload["maximum"], "filter trace maximum")
        )
        return cls(
            name=payload["name"],
            decision=FvgFilterDecision(payload["decision"]),
            reason=payload["reason"],
            observed=observed,
            minimum=minimum,
            maximum=maximum,
            metadata=payload["metadata"],
        )


@dataclass(frozen=True, slots=True)
class FvgMetrics:
    gap_size: Decimal
    gap_fraction: Decimal
    gap_bps: Decimal
    gap_ticks: Decimal | None = None
    gap_atr: Decimal | None = None
    gap_b_range: Decimal | None = None
    gap_average_range: Decimal | None = None
    candle_b_body: Decimal | None = None
    candle_b_body_fraction: Decimal | None = None
    candle_b_direction: str = "neutral"
    upper_wick_fraction: Decimal | None = None
    lower_wick_fraction: Decimal | None = None
    displacement: Decimal | None = None
    impulse_efficiency: Decimal | None = None
    close_strength: Decimal | None = None
    volume_ratio: Decimal | None = None
    distance_from_reference: Decimal | None = None
    utc_session: str = "unknown"

    @deterministic_decimal
    def __post_init__(self) -> None:
        for field_name in ("candle_b_direction", "utc_session"):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(f"{field_name} must be a canonical string")
        for field_name in (
            "gap_size",
            "gap_fraction",
            "gap_bps",
            "gap_ticks",
            "gap_atr",
            "gap_b_range",
            "gap_average_range",
            "candle_b_body",
            "candle_b_body_fraction",
            "upper_wick_fraction",
            "lower_wick_fraction",
            "displacement",
            "impulse_efficiency",
            "close_strength",
            "volume_ratio",
            "distance_from_reference",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not Decimal:
                raise TypeError(f"{field_name} must be a Decimal or null")
            if value is not None and not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
        if self.gap_size <= 0 or self.gap_fraction <= 0 or self.gap_bps <= 0:
            raise ValueError("gap metrics must be positive")
        try:
            expected_bps = self.gap_fraction * Decimal("10000")
        except DecimalException as exc:
            raise ValueError("gap metric arithmetic exceeds supported decimal range") from exc
        if self.gap_bps != expected_bps:
            raise ValueError("gap_bps must equal gap_fraction multiplied by 10000")
        for field_name in ("gap_ticks", "gap_atr", "gap_b_range", "gap_average_range"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive when available")
        if self.candle_b_body is not None and self.candle_b_body < 0:
            raise ValueError("candle_b_body must be non-negative")
        for field_name in (
            "candle_b_body_fraction",
            "upper_wick_fraction",
            "lower_wick_fraction",
            "close_strength",
        ):
            value = getattr(self, field_name)
            if value is not None and not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{field_name} must be in [0, 1]")
        for field_name in ("impulse_efficiency", "volume_ratio", "distance_from_reference"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.candle_b_direction not in {"bullish", "bearish", "neutral", "unknown"}:
            raise ValueError("candle_b_direction is invalid")
        if self.utc_session not in {"asia", "europe", "us", "unknown"}:
            raise ValueError("utc_session is invalid")

    def to_value(self) -> Mapping[str, Any]:
        values: dict[str, Any] = {}
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            values[field_name] = str(value) if isinstance(value, Decimal) else value
        return MappingProxyType(values)

    @classmethod
    def from_value(cls, payload: Mapping[str, object]) -> "FvgMetrics":
        payload = _require_canonical_mapping_keys(payload, "FVG metrics")
        if set(payload) != set(cls.__dataclass_fields__):
            raise ValueError("invalid FVG metrics payload")
        decimal_fields = {
            name for name in cls.__dataclass_fields__ if name not in {"candle_b_direction", "utc_session"}
        }
        values: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            raw = payload[name]
            if name in decimal_fields:
                if raw is not None and type(raw) is not str:
                    raise ValueError(f"metrics field {name} must be a canonical string or null")
                try:
                    values[name] = None if raw is None else parse_canonical_decimal_text(raw, f"metrics field {name}")
                except InvalidOperation as exc:
                    raise ValueError(f"metrics field {name} must be decimal-compatible") from exc
            else:
                if type(raw) is not str:
                    raise ValueError(f"metrics field {name} must be a canonical string")
                values[name] = raw
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FvgEvaluation:
    status: FvgEvaluationStatus
    zone: FvgZone | None
    metrics: FvgMetrics | None
    filters: tuple[FvgFilterTrace, ...] = ()
    reason: str = ""
    event_type: str = "confirmed"
    evaluated_at: datetime | None = None

    @deterministic_decimal
    def __post_init__(self) -> None:
        if not isinstance(self.status, FvgEvaluationStatus):
            raise TypeError("status must be an FvgEvaluationStatus")
        if self.zone is not None and not isinstance(self.zone, FvgZone):
            raise TypeError("zone must be an FvgZone or null")
        if self.metrics is not None and not isinstance(self.metrics, FvgMetrics):
            raise TypeError("metrics must be FvgMetrics or null")
        if type(self.filters) is not tuple or any(not isinstance(item, FvgFilterTrace) for item in self.filters):
            raise TypeError("filters must be a tuple of FvgFilterTrace")
        if type(self.reason) is not str:
            raise TypeError("reason must be a canonical string")
        if type(self.event_type) is not str:
            raise TypeError("event_type must be a canonical string")
        if self.status is FvgEvaluationStatus.ACCEPTED and (self.zone is None or self.metrics is None):
            raise ValueError("accepted evaluation requires zone and metrics")
        if self.status is not FvgEvaluationStatus.ACCEPTED and self.zone is not None:
            raise ValueError("non-accepted evaluation cannot contain a zone")
        if self.zone is not None and self.metrics is not None and self.metrics.gap_size != self.zone.size:
            raise ValueError("evaluation metrics do not match zone geometry")
        if self.event_type != "confirmed":
            raise ValueError("FvgEvaluation is reserved for confirmed FVG events")
        if self.evaluated_at is not None:
            if not isinstance(self.evaluated_at, datetime):
                raise TypeError("evaluated_at must be a datetime or null")
            object.__setattr__(self, "evaluated_at", as_utc(self.evaluated_at, "evaluated_at"))
        if self.metrics is None and self.filters:
            raise ValueError("evaluation without metrics cannot contain filter traces")
        if self.metrics is not None and not self.filters:
            raise ValueError("evaluation metrics require filter traces")
        if self.status is FvgEvaluationStatus.ACCEPTED:
            if self.evaluated_at is None:
                raise ValueError("accepted evaluation requires evaluated_at")
            if self.zone is None:
                raise ValueError("accepted evaluation requires zone and metrics")
            if self.evaluated_at < self.zone.created_at:
                raise ValueError("accepted evaluation cannot precede the source C close")
            if any(
                trace.decision in {FvgFilterDecision.FAILED, FvgFilterDecision.UNAVAILABLE} for trace in self.filters
            ):
                raise ValueError("accepted evaluation filter decisions must be passed or skipped")
        elif self.metrics is not None:
            required_decision = (
                FvgFilterDecision.FAILED
                if self.status is FvgEvaluationStatus.REJECTED
                else FvgFilterDecision.UNAVAILABLE
            )
            if not any(trace.decision is required_decision for trace in self.filters):
                raise ValueError(f"{self.status.value} evaluation requires {required_decision.value} filter trace")
            if self.status is FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA and any(
                trace.decision is FvgFilterDecision.FAILED for trace in self.filters
            ):
                raise ValueError("unavailable evaluation cannot contain failed filter traces")
        if self.metrics is not None and tuple(trace.name for trace in self.filters) != _FILTER_TRACE_ORDER:
            raise ValueError("confirmed evaluation filter trace set or order is invalid")
        if not self.reason:
            object.__setattr__(self, "reason", self.status.value)

    def to_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "status": self.status.value,
                "reason": self.reason,
                "event_type": self.event_type,
                "evaluated_at": None if self.evaluated_at is None else self.evaluated_at.isoformat(),
                "zone": None if self.zone is None else dict(self.zone.to_feature_value()),
                "metrics": None if self.metrics is None else dict(self.metrics.to_value()),
                "filters": tuple(dict(item.to_value()) for item in self.filters),
            }
        )
