from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .decimal_utils import deterministic_decimal, parse_canonical_decimal_text
from .timeframes import as_utc, canonical_timeframe, is_period_aligned, parse_canonical_utc_datetime, period_close
from .models_base import (
    FvgDirection,
    FvgFilterDecision,
    _FILTER_TRACE_ORDER,
    _require_canonical_mapping_keys,
    _require_canonical_symbol,
)
from .models_filters import FvgFilterTrace, FvgMetrics


class FvgPreStatus(str, Enum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    NOT_DUE = "not_due"
    UNAVAILABLE_WITH_CURRENT_DATA = "unavailable_with_current_data"


@dataclass(frozen=True, slots=True)
class FvgPreCandidate:
    candidate_id: str
    symbol: str
    timeframe: str
    direction: FvgDirection
    lower: Decimal
    upper: Decimal
    source_a_open_time: datetime
    source_b_open_time: datetime
    source_c_open_time: datetime
    source_c_expected_close_time: datetime
    evaluated_at: datetime
    processed_at: datetime
    remaining_seconds: int
    metrics: FvgMetrics
    filters: tuple[FvgFilterTrace, ...] = ()
    disappearance_probability: Decimal | None = None
    probability_reason: str = "no_calibrated_probability_model"

    @deterministic_decimal
    def __post_init__(self) -> None:
        for name in ("candidate_id", "symbol", "timeframe", "probability_reason"):
            if type(getattr(self, name)) is not str:
                raise TypeError(f"{name} must be a canonical string")
        if not isinstance(self.direction, FvgDirection):
            raise TypeError("direction must be an FvgDirection")
        for name in ("lower", "upper"):
            if type(getattr(self, name)) is not Decimal:
                raise TypeError(f"{name} must be a Decimal")
        if type(self.remaining_seconds) is not int:
            raise TypeError("remaining_seconds must be a canonical int")
        if not isinstance(self.metrics, FvgMetrics):
            raise TypeError("metrics must be FvgMetrics")
        if type(self.filters) is not tuple or any(not isinstance(item, FvgFilterTrace) for item in self.filters):
            raise TypeError("filters must be a tuple of FvgFilterTrace")
        if tuple(trace.name for trace in self.filters) != _FILTER_TRACE_ORDER:
            raise ValueError("pre-FVG candidate filter trace set or order is invalid")
        if any(trace.decision not in {FvgFilterDecision.PASSED, FvgFilterDecision.SKIPPED} for trace in self.filters):
            raise ValueError("pre-FVG candidate filter trace decisions must be passed or skipped")
        if self.disappearance_probability is not None and type(self.disappearance_probability) is not Decimal:
            raise TypeError("disappearance_probability must be a Decimal or null")
        for name in (
            "source_a_open_time",
            "source_b_open_time",
            "source_c_open_time",
            "source_c_expected_close_time",
            "evaluated_at",
            "processed_at",
        ):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime")
            object.__setattr__(self, name, as_utc(value, name))
        if not self.candidate_id or not self.symbol or not self.timeframe:
            raise ValueError("candidate identity, symbol and timeframe are required")
        _require_canonical_symbol(self.symbol)
        if canonical_timeframe(self.timeframe) != self.timeframe:
            raise ValueError("timeframe must be canonical")
        for name in (
            "source_a_open_time",
            "source_b_open_time",
            "source_c_open_time",
            "source_c_expected_close_time",
        ):
            if not is_period_aligned(getattr(self, name), self.timeframe):
                raise ValueError(f"{name} must align to timeframe")
        if self.source_c_expected_close_time != period_close(self.source_c_open_time, self.timeframe):
            raise ValueError("pre-FVG candidate expected close must match the timeframe close")
        if not self.lower.is_finite() or not self.upper.is_finite():
            raise ValueError("pre-FVG boundaries must be finite")
        if self.lower <= 0 or self.upper <= 0 or self.lower >= self.upper:
            raise ValueError("pre-FVG boundaries must be positive and ordered")
        if self.metrics.gap_size != self.size:
            raise ValueError("pre-FVG metrics do not match candidate geometry")
        if not (
            self.source_a_open_time
            < self.source_b_open_time
            < self.source_c_open_time
            < self.evaluated_at
            <= self.processed_at
            < self.source_c_expected_close_time
        ):
            raise ValueError("pre-FVG source/evaluation times are invalid")
        expected_remaining = int((self.source_c_expected_close_time - self.evaluated_at).total_seconds())
        if self.remaining_seconds != expected_remaining or self.remaining_seconds <= 0:
            raise ValueError("remaining_seconds must match the target close")
        if self.disappearance_probability is not None:
            raise ValueError("disappearance_probability is unavailable without a calibrated probability model")
        if self.probability_reason != "no_calibrated_probability_model":
            raise ValueError("probability_reason must declare that no calibrated probability model is available")

    @property
    @deterministic_decimal
    def midpoint(self) -> Decimal:
        try:
            return (self.lower + self.upper) / Decimal("2")
        except DecimalException as exc:
            raise ValueError("pre-FVG geometry arithmetic exceeds supported decimal range") from exc

    @property
    @deterministic_decimal
    def size(self) -> Decimal:
        try:
            return self.upper - self.lower
        except DecimalException as exc:
            raise ValueError("pre-FVG geometry arithmetic exceeds supported decimal range") from exc

    def to_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "candidate_id": self.candidate_id,
                "event_type": "pre_fvg",
                "provisional": True,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "direction": self.direction.value,
                "lower": str(self.lower),
                "upper": str(self.upper),
                "midpoint": str(self.midpoint),
                "gap_size": str(self.size),
                "source_a_open_time": self.source_a_open_time.isoformat(),
                "source_b_open_time": self.source_b_open_time.isoformat(),
                "source_c_open_time": self.source_c_open_time.isoformat(),
                "source_c_expected_close_time": self.source_c_expected_close_time.isoformat(),
                "evaluated_at": self.evaluated_at.isoformat(),
                "processed_at": self.processed_at.isoformat(),
                "remaining_seconds": self.remaining_seconds,
                "metrics": dict(self.metrics.to_value()),
                "filters": tuple(dict(item.to_value()) for item in self.filters),
                "disappearance_probability": (
                    None if self.disappearance_probability is None else str(self.disappearance_probability)
                ),
                "probability_reason": self.probability_reason,
            }
        )

    def to_state(self) -> Mapping[str, Any]:
        return self.to_value()

    @classmethod
    def from_state(cls, payload: Mapping[str, object]) -> "FvgPreCandidate":
        payload = _require_canonical_mapping_keys(payload, "pre-FVG candidate")
        required = {
            "candidate_id",
            "event_type",
            "provisional",
            "symbol",
            "timeframe",
            "direction",
            "lower",
            "upper",
            "midpoint",
            "gap_size",
            "source_a_open_time",
            "source_b_open_time",
            "source_c_open_time",
            "source_c_expected_close_time",
            "evaluated_at",
            "processed_at",
            "remaining_seconds",
            "metrics",
            "filters",
            "disappearance_probability",
            "probability_reason",
        }
        legacy_required = required - {"processed_at"}
        if (
            frozenset(payload) not in {frozenset(required), frozenset(legacy_required)}
            or type(payload["event_type"]) is not str
        ):
            raise ValueError("invalid pre-FVG candidate state")
        if payload["event_type"] != "pre_fvg" or payload["provisional"] is not True:
            raise ValueError("invalid pre-FVG candidate state")
        if not isinstance(payload["metrics"], Mapping) or not isinstance(payload["filters"], (list, tuple)):
            raise ValueError("invalid pre-FVG candidate metrics or filters")
        metrics = FvgMetrics.from_value(payload["metrics"])
        filters = tuple(FvgFilterTrace.from_value(item) for item in payload["filters"] if isinstance(item, Mapping))
        if len(filters) != len(payload["filters"]):
            raise ValueError("invalid pre-FVG filter row")
        for name in (
            "candidate_id",
            "symbol",
            "timeframe",
            "direction",
            "lower",
            "upper",
            "midpoint",
            "gap_size",
            "source_a_open_time",
            "source_b_open_time",
            "source_c_open_time",
            "source_c_expected_close_time",
            "evaluated_at",
            "probability_reason",
        ):
            if type(payload[name]) is not str:
                raise ValueError(f"pre-FVG field {name} must be a canonical string")
        if "processed_at" in payload and type(payload["processed_at"]) is not str:
            raise ValueError("pre-FVG field processed_at must be a canonical string")
        if type(payload["remaining_seconds"]) is not int:
            raise ValueError("pre-FVG remaining_seconds must be a canonical int")
        if payload["disappearance_probability"] is not None and type(payload["disappearance_probability"]) is not str:
            raise ValueError("pre-FVG disappearance_probability must be a canonical string or null")
        lower = parse_canonical_decimal_text(payload["lower"], "pre-FVG lower")
        upper = parse_canonical_decimal_text(payload["upper"], "pre-FVG upper")
        disappearance_probability = (
            None
            if payload["disappearance_probability"] is None
            else parse_canonical_decimal_text(
                payload["disappearance_probability"],
                "pre-FVG disappearance_probability",
            )
        )
        candidate = cls(
            candidate_id=payload["candidate_id"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            direction=FvgDirection(payload["direction"]),
            lower=lower,
            upper=upper,
            source_a_open_time=parse_canonical_utc_datetime(payload["source_a_open_time"], "source_a_open_time"),
            source_b_open_time=parse_canonical_utc_datetime(payload["source_b_open_time"], "source_b_open_time"),
            source_c_open_time=parse_canonical_utc_datetime(payload["source_c_open_time"], "source_c_open_time"),
            source_c_expected_close_time=parse_canonical_utc_datetime(
                payload["source_c_expected_close_time"], "source_c_expected_close_time"
            ),
            evaluated_at=parse_canonical_utc_datetime(payload["evaluated_at"], "evaluated_at"),
            processed_at=parse_canonical_utc_datetime(
                payload.get("processed_at", payload["evaluated_at"]), "processed_at"
            ),
            remaining_seconds=payload["remaining_seconds"],
            metrics=metrics,
            filters=filters,
            disappearance_probability=disappearance_probability,
            probability_reason=payload["probability_reason"],
        )
        if str(candidate.midpoint) != payload["midpoint"] or str(candidate.size) != payload["gap_size"]:
            raise ValueError("pre-FVG candidate derived fields are inconsistent")
        return candidate


@dataclass(frozen=True, slots=True)
class FvgPreEvaluation:
    status: FvgPreStatus
    candidate: FvgPreCandidate | None = None
    reason: str = ""
    target_evaluation_time: datetime | None = None
    processed_at: datetime | None = None
    metrics: FvgMetrics | None = None
    filters: tuple[FvgFilterTrace, ...] = ()

    @deterministic_decimal
    def __post_init__(self) -> None:
        if not isinstance(self.status, FvgPreStatus):
            raise TypeError("status must be an FvgPreStatus")
        if self.candidate is not None and not isinstance(self.candidate, FvgPreCandidate):
            raise TypeError("candidate must be an FvgPreCandidate or null")
        if self.metrics is not None and not isinstance(self.metrics, FvgMetrics):
            raise TypeError("metrics must be FvgMetrics or null")
        if type(self.filters) is not tuple or any(not isinstance(item, FvgFilterTrace) for item in self.filters):
            raise TypeError("filters must be a tuple of FvgFilterTrace")
        if type(self.reason) is not str:
            raise TypeError("reason must be a canonical string")
        if self.status is FvgPreStatus.CANDIDATE and self.candidate is None:
            raise ValueError("candidate status requires a candidate")
        if self.status is not FvgPreStatus.CANDIDATE and self.candidate is not None:
            raise ValueError("non-candidate status cannot contain a candidate")
        for name in ("target_evaluation_time", "processed_at"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, datetime):
                    raise TypeError(f"{name} must be a datetime or null")
                object.__setattr__(self, name, as_utc(value, name))
        if self.candidate is not None and self.metrics is not None and self.candidate.metrics != self.metrics:
            raise ValueError("candidate and evaluation metrics must match")
        if self.metrics is None and self.filters:
            raise ValueError("pre-FVG evaluation without metrics cannot contain filter traces")
        if self.metrics is not None and not self.filters:
            raise ValueError("pre-FVG evaluation metrics require filter traces")
        if self.status is FvgPreStatus.CANDIDATE:
            if self.candidate is None:
                raise ValueError("candidate status requires a candidate")
            if self.metrics is None:
                raise ValueError("candidate evaluation requires metrics")
            if self.filters != self.candidate.filters:
                raise ValueError("candidate evaluation filters must match the candidate")
            if self.target_evaluation_time != self.candidate.evaluated_at:
                raise ValueError("candidate target evaluation time must match the candidate cutoff")
            if self.processed_at is None:
                raise ValueError("candidate evaluation requires processed_at")
            if self.processed_at < self.candidate.evaluated_at:
                raise ValueError("candidate processed_at cannot precede the target evaluation")
            if self.processed_at >= self.candidate.source_c_expected_close_time:
                raise ValueError("candidate processed_at must precede the source C close")
            if self.processed_at != self.candidate.processed_at:
                raise ValueError("candidate processed_at must match the evaluation")
        elif self.metrics is not None:
            if self.status is FvgPreStatus.NOT_DUE:
                raise ValueError("not_due pre-FVG evaluation cannot contain metrics")
            required_decision = (
                FvgFilterDecision.FAILED if self.status is FvgPreStatus.REJECTED else FvgFilterDecision.UNAVAILABLE
            )
            if not any(trace.decision is required_decision for trace in self.filters):
                raise ValueError(
                    f"{self.status.value} pre-FVG evaluation requires {required_decision.value} filter trace"
                )
            if self.status is FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA and any(
                trace.decision is FvgFilterDecision.FAILED for trace in self.filters
            ):
                raise ValueError("unavailable pre-FVG evaluation cannot contain failed filter traces")
        if self.metrics is not None and tuple(trace.name for trace in self.filters) != _FILTER_TRACE_ORDER:
            raise ValueError("pre-FVG evaluation filter trace set or order is invalid")
        if not self.reason:
            object.__setattr__(self, "reason", self.status.value)

    def to_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "status": self.status.value,
                "reason": self.reason,
                "event_type": "pre_fvg",
                "target_evaluation_time": (
                    None if self.target_evaluation_time is None else self.target_evaluation_time.isoformat()
                ),
                "processed_at": None if self.processed_at is None else self.processed_at.isoformat(),
                "metrics": None if self.metrics is None else dict(self.metrics.to_value()),
                "filters": tuple(dict(item.to_value()) for item in self.filters),
                "candidate": None if self.candidate is None else dict(self.candidate.to_value()),
            }
        )
