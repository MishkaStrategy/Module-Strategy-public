from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from .enums import (
    HypothesisPattern,
    HypothesisStatus,
    LifecycleState,
    PivotKind,
    PivotStatus,
    StructureState,
    SwingLabel,
    WaveClassification,
    WaveDirection,
)

ZERO = Decimal("0")
ONE = Decimal("1")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _as_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return cast(Mapping[str, object], value)


def _as_sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return cast(Sequence[object], value)


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _as_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _as_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid ISO datetime") from exc
    return _utc(parsed)


def _require_nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _require_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class BarRecord:
    timestamp: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(self, "close_time", _utc(self.close_time))
        if self.close_time < self.timestamp:
            raise ValueError("bar close_time cannot precede timestamp")
        for name in ("open", "high", "low", "close"):
            _require_decimal(getattr(self, name), f"bar {name}", positive=True)
        _require_decimal(self.volume, "bar volume", nonnegative=True)
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("bar OHLC values are inconsistent")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "close_time": self.close_time.isoformat(),
            "open": _decimal_text(self.open),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "volume": _decimal_text(self.volume),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BarRecord:
        return cls(
            timestamp=datetime.fromisoformat(str(value["timestamp"])),
            close_time=datetime.fromisoformat(str(value["close_time"])),
            open=Decimal(str(value["open"])),
            high=Decimal(str(value["high"])),
            low=Decimal(str(value["low"])),
            close=Decimal(str(value["close"])),
            volume=Decimal(str(value["volume"])),
        )


@dataclass(frozen=True, slots=True)
class PivotPoint:
    pivot_id: str
    symbol: str
    timeframe: str
    kind: PivotKind
    pivot_time: datetime
    confirmed_at: datetime
    price: Decimal
    bar_index: int
    status: PivotStatus = PivotStatus.CONFIRMED
    revision: int = 0
    reason_codes: tuple[str, ...] = ()
    config_fingerprint: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pivot_time", _utc(self.pivot_time))
        object.__setattr__(self, "confirmed_at", _utc(self.confirmed_at))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        for name in ("pivot_id", "symbol", "timeframe"):
            _require_nonempty(getattr(self, name), f"pivot {name}")
        if self.confirmed_at < self.pivot_time:
            raise ValueError("confirmed_at cannot precede pivot_time")
        _require_decimal(self.price, "pivot price", positive=True)
        _require_int(self.bar_index, "pivot bar_index")
        _require_int(self.revision, "pivot revision")
        if self.status is not PivotStatus.CONFIRMED:
            raise ValueError("PivotPoint status must be confirmed")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "pivot_id": self.pivot_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "kind": self.kind.value,
            "pivot_time": self.pivot_time.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "price": _decimal_text(self.price),
            "bar_index": self.bar_index,
            "status": self.status.value,
            "revision": self.revision,
            "reason_codes": list(self.reason_codes),
            "config_fingerprint": self.config_fingerprint,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PivotPoint:
        return cls(
            pivot_id=str(value["pivot_id"]),
            symbol=str(value["symbol"]),
            timeframe=str(value["timeframe"]),
            kind=PivotKind(str(value["kind"])),
            pivot_time=datetime.fromisoformat(str(value["pivot_time"])),
            confirmed_at=datetime.fromisoformat(str(value["confirmed_at"])),
            price=Decimal(str(value["price"])),
            bar_index=_as_int(value["bar_index"], "bar_index"),
            status=PivotStatus(str(value["status"])),
            revision=_as_int(value.get("revision", 0), "revision"),
            reason_codes=tuple(str(item) for item in _as_sequence(value.get("reason_codes", ()), "reason_codes")),
            config_fingerprint=str(value.get("config_fingerprint", "")),
            metadata=dict(_as_mapping(value.get("metadata", {}), "metadata")),
        )


@dataclass(frozen=True, slots=True)
class ProvisionalPivot:
    pivot_id: str
    revision_id: str
    symbol: str
    timeframe: str
    kind: PivotKind
    pivot_time: datetime
    observed_at: datetime
    price: Decimal
    bar_index: int
    revision: int
    threshold: Decimal
    status: PivotStatus = PivotStatus.PROVISIONAL
    config_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pivot_time", _utc(self.pivot_time))
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        for name in ("pivot_id", "revision_id", "symbol", "timeframe"):
            _require_nonempty(getattr(self, name), f"provisional {name}")
        if self.observed_at < self.pivot_time:
            raise ValueError("provisional observed_at cannot precede pivot_time")
        _require_decimal(self.price, "provisional price", positive=True)
        _require_decimal(self.threshold, "provisional threshold", nonnegative=True)
        _require_int(self.bar_index, "provisional bar_index")
        _require_int(self.revision, "provisional revision", minimum=1)
        if self.status is not PivotStatus.PROVISIONAL:
            raise ValueError("ProvisionalPivot status must be provisional")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "pivot_id": self.pivot_id,
            "revision_id": self.revision_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "kind": self.kind.value,
            "pivot_time": self.pivot_time.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "price": _decimal_text(self.price),
            "bar_index": self.bar_index,
            "revision": self.revision,
            "threshold": _decimal_text(self.threshold),
            "status": self.status.value,
            "config_fingerprint": self.config_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ProvisionalPivot:
        return cls(
            pivot_id=str(value["pivot_id"]),
            revision_id=str(value["revision_id"]),
            symbol=str(value["symbol"]),
            timeframe=str(value["timeframe"]),
            kind=PivotKind(str(value["kind"])),
            pivot_time=datetime.fromisoformat(str(value["pivot_time"])),
            observed_at=datetime.fromisoformat(str(value["observed_at"])),
            price=Decimal(str(value["price"])),
            bar_index=_as_int(value["bar_index"], "bar_index"),
            revision=_as_int(value["revision"], "revision"),
            threshold=Decimal(str(value["threshold"])),
            status=PivotStatus(str(value.get("status", PivotStatus.PROVISIONAL.value))),
            config_fingerprint=str(value.get("config_fingerprint", "")),
        )


@dataclass(frozen=True, slots=True)
class WaveLeg:
    wave_id: str
    symbol: str
    timeframe: str
    direction: WaveDirection
    start_pivot_id: str
    end_pivot_id: str
    start_time: datetime
    end_time: datetime
    confirmed_at: datetime
    start_price: Decimal
    end_price: Decimal
    price_change: Decimal
    price_change_pct: Decimal
    duration_bars: int
    duration_seconds: int
    range: Decimal
    atr_normalized_size: Decimal
    volume: Decimal
    relative_volume: Decimal
    body_efficiency: Decimal
    path_efficiency: Decimal
    maximum_adverse_excursion: Decimal
    maximum_favorable_excursion: Decimal
    slope: Decimal
    velocity: Decimal
    acceleration: Decimal | None
    classification: WaveClassification
    classification_score: Decimal
    confidence: Decimal
    status: str
    reason_codes: tuple[str, ...]
    feature_inputs: Mapping[str, object]
    config_fingerprint: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", _utc(self.start_time))
        object.__setattr__(self, "end_time", _utc(self.end_time))
        object.__setattr__(self, "confirmed_at", _utc(self.confirmed_at))
        object.__setattr__(self, "feature_inputs", MappingProxyType(dict(self.feature_inputs)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        for name in ("wave_id", "symbol", "timeframe", "start_pivot_id", "end_pivot_id"):
            _require_nonempty(getattr(self, name), f"wave {name}")
        if not self.start_time <= self.end_time <= self.confirmed_at:
            raise ValueError("wave timestamps must satisfy start <= end <= confirmed_at")
        _require_int(self.duration_bars, "wave duration_bars", minimum=1)
        _require_int(self.duration_seconds, "wave duration_seconds", minimum=1)
        _require_decimal(self.start_price, "wave start_price", positive=True)
        _require_decimal(self.end_price, "wave end_price", positive=True)
        _require_decimal(self.price_change, "wave price_change")
        _require_decimal(self.price_change_pct, "wave price_change_pct")
        if self.price_change != self.end_price - self.start_price:
            raise ValueError("wave price_change is inconsistent")
        if self.price_change_pct != self.price_change / self.start_price:
            raise ValueError("wave price_change_pct is inconsistent")
        if self.direction is WaveDirection.UP and self.price_change <= 0:
            raise ValueError("up wave must have positive price_change")
        if self.direction is WaveDirection.DOWN and self.price_change >= 0:
            raise ValueError("down wave must have negative price_change")
        _require_decimal(self.range, "wave range", nonnegative=True)
        if self.range < abs(self.price_change) and not bool(self.metadata.get("path_truncated", False)):
            raise ValueError(
                "wave range cannot be smaller than endpoint movement unless the retained path is truncated"
            )
        for name in (
            "atr_normalized_size",
            "volume",
            "relative_volume",
            "maximum_adverse_excursion",
            "maximum_favorable_excursion",
        ):
            _require_decimal(getattr(self, name), f"wave {name}", nonnegative=True)
        for name in ("body_efficiency", "path_efficiency", "classification_score", "confidence"):
            value = _require_decimal(getattr(self, name), f"wave {name}", nonnegative=True)
            if value > ONE:
                raise ValueError(f"wave {name} must be in [0, 1]")
        _require_decimal(self.slope, "wave slope")
        _require_decimal(self.velocity, "wave velocity")
        expected_slope = self.price_change / Decimal(self.duration_bars)
        expected_velocity = self.price_change / Decimal(self.duration_seconds)
        tolerance = Decimal("1e-9")
        if abs(self.slope - expected_slope) > tolerance:
            raise ValueError("wave slope is inconsistent")
        if abs(self.velocity - expected_velocity) > tolerance:
            raise ValueError("wave velocity is inconsistent")
        if self.acceleration is not None:
            _require_decimal(self.acceleration, "wave acceleration")
        if self.status != "confirmed":
            raise ValueError("WaveLeg status must be confirmed")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "wave_id": self.wave_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "start_pivot_id": self.start_pivot_id,
            "end_pivot_id": self.end_pivot_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "start_price": _decimal_text(self.start_price),
            "end_price": _decimal_text(self.end_price),
            "price_change": _decimal_text(self.price_change),
            "price_change_pct": _decimal_text(self.price_change_pct),
            "duration_bars": self.duration_bars,
            "duration_seconds": self.duration_seconds,
            "range": _decimal_text(self.range),
            "atr_normalized_size": _decimal_text(self.atr_normalized_size),
            "volume": _decimal_text(self.volume),
            "relative_volume": _decimal_text(self.relative_volume),
            "body_efficiency": _decimal_text(self.body_efficiency),
            "path_efficiency": _decimal_text(self.path_efficiency),
            "maximum_adverse_excursion": _decimal_text(self.maximum_adverse_excursion),
            "maximum_favorable_excursion": _decimal_text(self.maximum_favorable_excursion),
            "slope": _decimal_text(self.slope),
            "velocity": _decimal_text(self.velocity),
            "acceleration": _decimal_text(self.acceleration) if self.acceleration is not None else None,
            "classification": self.classification.value,
            "classification_score": _decimal_text(self.classification_score),
            "confidence": _decimal_text(self.confidence),
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "feature_inputs": dict(self.feature_inputs),
            "config_fingerprint": self.config_fingerprint,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WaveLeg:
        acceleration = value.get("acceleration")
        metadata = dict(_as_mapping(value.get("metadata", {}), "metadata"))
        maximum_adverse_excursion = Decimal(str(value["maximum_adverse_excursion"]))
        maximum_favorable_excursion = Decimal(str(value["maximum_favorable_excursion"]))
        # RC1 could serialize negative excursions when the retained path no longer
        # contained the originating pivot. Normalize only that explicitly marked
        # legacy case; malformed non-truncated states remain fail-closed.
        if bool(metadata.get("path_truncated", False)):
            maximum_adverse_excursion = max(ZERO, maximum_adverse_excursion)
            maximum_favorable_excursion = max(ZERO, maximum_favorable_excursion)
        return cls(
            wave_id=str(value["wave_id"]),
            symbol=str(value["symbol"]),
            timeframe=str(value["timeframe"]),
            direction=WaveDirection(str(value["direction"])),
            start_pivot_id=str(value["start_pivot_id"]),
            end_pivot_id=str(value["end_pivot_id"]),
            start_time=datetime.fromisoformat(str(value["start_time"])),
            end_time=datetime.fromisoformat(str(value["end_time"])),
            confirmed_at=datetime.fromisoformat(str(value["confirmed_at"])),
            start_price=Decimal(str(value["start_price"])),
            end_price=Decimal(str(value["end_price"])),
            price_change=Decimal(str(value["price_change"])),
            price_change_pct=Decimal(str(value["price_change_pct"])),
            duration_bars=_as_int(value["duration_bars"], "duration_bars"),
            duration_seconds=_as_int(value["duration_seconds"], "duration_seconds"),
            range=Decimal(str(value["range"])),
            atr_normalized_size=Decimal(str(value["atr_normalized_size"])),
            volume=Decimal(str(value["volume"])),
            relative_volume=Decimal(str(value["relative_volume"])),
            body_efficiency=Decimal(str(value["body_efficiency"])),
            path_efficiency=Decimal(str(value["path_efficiency"])),
            maximum_adverse_excursion=maximum_adverse_excursion,
            maximum_favorable_excursion=maximum_favorable_excursion,
            slope=Decimal(str(value["slope"])),
            velocity=Decimal(str(value["velocity"])),
            acceleration=None if acceleration is None else Decimal(str(acceleration)),
            classification=WaveClassification(str(value["classification"])),
            classification_score=Decimal(str(value["classification_score"])),
            confidence=Decimal(str(value["confidence"])),
            status=str(value["status"]),
            reason_codes=tuple(str(item) for item in _as_sequence(value.get("reason_codes", ()), "reason_codes")),
            feature_inputs=dict(_as_mapping(value.get("feature_inputs", {}), "feature_inputs")),
            config_fingerprint=str(value["config_fingerprint"]),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    timeframe: str
    state: StructureState
    latest_label: SwingLabel
    internal_state: StructureState
    external_state: StructureState
    break_detected: bool
    continuation: bool
    possible_reversal: bool
    invalidation_price: Decimal | None
    confidence: Decimal
    reason_codes: tuple[str, ...]
    as_of: datetime
    segment_id: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of))
        _require_nonempty(self.timeframe, "structure timeframe")
        for name in ("break_detected", "continuation", "possible_reversal"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"structure {name} must be boolean")
        confidence = _require_decimal(self.confidence, "structure confidence", nonnegative=True)
        if confidence > ONE:
            raise ValueError("structure confidence must be in [0, 1]")
        if self.invalidation_price is not None:
            _require_decimal(self.invalidation_price, "structure invalidation_price", positive=True)
        _require_int(self.segment_id, "structure segment_id")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "timeframe": self.timeframe,
            "state": self.state.value,
            "latest_label": self.latest_label.value,
            "internal_state": self.internal_state.value,
            "external_state": self.external_state.value,
            "break_detected": self.break_detected,
            "continuation": self.continuation,
            "possible_reversal": self.possible_reversal,
            "invalidation_price": _decimal_text(self.invalidation_price)
            if self.invalidation_price is not None
            else None,
            "confidence": _decimal_text(self.confidence),
            "reason_codes": list(self.reason_codes),
            "as_of": self.as_of.isoformat(),
            "segment_id": self.segment_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StructureSnapshot:
        invalidation = value.get("invalidation_price")
        return cls(
            timeframe=str(value["timeframe"]),
            state=StructureState(str(value["state"])),
            latest_label=SwingLabel(str(value["latest_label"])),
            internal_state=StructureState(str(value["internal_state"])),
            external_state=StructureState(str(value["external_state"])),
            break_detected=_as_bool(value["break_detected"], "break_detected"),
            continuation=_as_bool(value["continuation"], "continuation"),
            possible_reversal=_as_bool(value["possible_reversal"], "possible_reversal"),
            invalidation_price=None if invalidation is None else Decimal(str(invalidation)),
            confidence=Decimal(str(value["confidence"])),
            reason_codes=tuple(str(item) for item in _as_sequence(value.get("reason_codes", ()), "reason_codes")),
            as_of=datetime.fromisoformat(str(value["as_of"])),
            segment_id=_as_int(value.get("segment_id", 0), "segment_id"),
        )


@dataclass(frozen=True, slots=True)
class WaveHypothesis:
    hypothesis_id: str
    pattern_type: HypothesisPattern
    direction: WaveDirection
    degree: str
    wave_labels: Mapping[str, str]
    start_time: datetime
    end_time: datetime
    confirmed_at: datetime
    confidence: Decimal
    rule_score: Decimal
    guideline_score: Decimal
    invalidation_price: Decimal
    alternative_rank: int
    status: HypothesisStatus
    reason_codes: tuple[str, ...]
    supporting_wave_ids: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", _utc(self.start_time))
        object.__setattr__(self, "end_time", _utc(self.end_time))
        object.__setattr__(self, "confirmed_at", _utc(self.confirmed_at))
        object.__setattr__(self, "wave_labels", MappingProxyType(dict(self.wave_labels)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        for name in ("hypothesis_id", "degree"):
            _require_nonempty(getattr(self, name), f"hypothesis {name}")
        if not self.start_time <= self.end_time <= self.confirmed_at:
            raise ValueError("hypothesis timestamps must satisfy start <= end <= confirmed_at")
        for name in ("confidence", "rule_score", "guideline_score"):
            value = _require_decimal(getattr(self, name), f"hypothesis {name}", nonnegative=True)
            if value > ONE:
                raise ValueError("hypothesis scores must be in [0, 1]")
        _require_decimal(self.invalidation_price, "hypothesis invalidation_price", positive=True)
        _require_int(self.alternative_rank, "hypothesis alternative_rank")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "pattern_type": self.pattern_type.value,
            "direction": self.direction.value,
            "degree": self.degree,
            "wave_labels": dict(self.wave_labels),
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "confidence": _decimal_text(self.confidence),
            "rule_score": _decimal_text(self.rule_score),
            "guideline_score": _decimal_text(self.guideline_score),
            "invalidation_price": _decimal_text(self.invalidation_price),
            "alternative_rank": self.alternative_rank,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "supporting_wave_ids": list(self.supporting_wave_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WaveHypothesis:
        return cls(
            hypothesis_id=str(value["hypothesis_id"]),
            pattern_type=HypothesisPattern(str(value["pattern_type"])),
            direction=WaveDirection(str(value["direction"])),
            degree=str(value["degree"]),
            wave_labels={str(k): str(v) for k, v in dict(_as_mapping(value["wave_labels"], "wave_labels")).items()},
            start_time=datetime.fromisoformat(str(value["start_time"])),
            end_time=datetime.fromisoformat(str(value["end_time"])),
            confirmed_at=datetime.fromisoformat(str(value["confirmed_at"])),
            confidence=Decimal(str(value["confidence"])),
            rule_score=Decimal(str(value["rule_score"])),
            guideline_score=Decimal(str(value["guideline_score"])),
            invalidation_price=Decimal(str(value["invalidation_price"])),
            alternative_rank=_as_int(value["alternative_rank"], "alternative_rank"),
            status=HypothesisStatus(str(value["status"])),
            reason_codes=tuple(str(item) for item in _as_sequence(value.get("reason_codes", ()), "reason_codes")),
            supporting_wave_ids=tuple(
                str(item) for item in _as_sequence(value.get("supporting_wave_ids", ()), "supporting_wave_ids")
            ),
            metadata=dict(_as_mapping(value.get("metadata", {}), "metadata")),
        )


@dataclass(slots=True)
class TimeframeState:
    timeframe: str
    symbol: str | None = None
    lifecycle: LifecycleState = LifecycleState.INSUFFICIENT_DATA
    bars: list[BarRecord] = field(default_factory=list)
    bar_offset: int = 0
    confirmed_pivots: list[PivotPoint] = field(default_factory=list)
    wave_legs: list[WaveLeg] = field(default_factory=list)
    provisional: ProvisionalPivot | None = None
    structure: StructureSnapshot | None = None
    hypotheses: list[WaveHypothesis] = field(default_factory=list)
    last_open_time: datetime | None = None
    last_fingerprint: str | None = None
    processed_bars: int = 0
    atr: Decimal | None = None
    previous_close: Decimal | None = None
    tr_seed: list[Decimal] = field(default_factory=list)
    atr_values: list[Decimal] = field(default_factory=list)
    zigzag_direction: WaveDirection | None = None
    initial_high: tuple[Decimal, datetime, int] | None = None
    initial_low: tuple[Decimal, datetime, int] | None = None
    gap_blocked: bool = False
    recovery_count: int = 0
    segment_id: int = 0
    revision_counter: int = 0
    publications: int = 0
    duplicate_events: int = 0
    gap_events: int = 0
    published_feature_ids: dict[str, str] = field(default_factory=dict)

    def to_mapping(self) -> Mapping[str, object]:
        def extreme(value: tuple[Decimal, datetime, int] | None) -> object:
            if value is None:
                return None
            return {"price": _decimal_text(value[0]), "time": value[1].isoformat(), "index": value[2]}

        return {
            "timeframe": self.timeframe,
            "symbol": self.symbol,
            "lifecycle": self.lifecycle.value,
            "bars": [dict(item.to_mapping()) for item in self.bars],
            "bar_offset": self.bar_offset,
            "confirmed_pivots": [dict(item.to_mapping()) for item in self.confirmed_pivots],
            "wave_legs": [dict(item.to_mapping()) for item in self.wave_legs],
            "provisional": None if self.provisional is None else dict(self.provisional.to_mapping()),
            "structure": None if self.structure is None else dict(self.structure.to_mapping()),
            "hypotheses": [dict(item.to_mapping()) for item in self.hypotheses],
            "last_open_time": None if self.last_open_time is None else self.last_open_time.isoformat(),
            "last_fingerprint": self.last_fingerprint,
            "processed_bars": self.processed_bars,
            "atr": None if self.atr is None else _decimal_text(self.atr),
            "previous_close": None if self.previous_close is None else _decimal_text(self.previous_close),
            "tr_seed": [_decimal_text(item) for item in self.tr_seed],
            "atr_values": [_decimal_text(item) for item in self.atr_values],
            "zigzag_direction": None if self.zigzag_direction is None else self.zigzag_direction.value,
            "initial_high": extreme(self.initial_high),
            "initial_low": extreme(self.initial_low),
            "gap_blocked": self.gap_blocked,
            "recovery_count": self.recovery_count,
            "segment_id": self.segment_id,
            "revision_counter": self.revision_counter,
            "publications": self.publications,
            "duplicate_events": self.duplicate_events,
            "gap_events": self.gap_events,
            "published_feature_ids": dict(sorted(self.published_feature_ids.items())),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TimeframeState:
        allowed = {
            "timeframe",
            "symbol",
            "lifecycle",
            "bars",
            "bar_offset",
            "confirmed_pivots",
            "wave_legs",
            "provisional",
            "structure",
            "hypotheses",
            "last_open_time",
            "last_fingerprint",
            "processed_bars",
            "atr",
            "previous_close",
            "tr_seed",
            "atr_values",
            "zigzag_direction",
            "initial_high",
            "initial_low",
            "gap_blocked",
            "recovery_count",
            "segment_id",
            "revision_counter",
            "publications",
            "duplicate_events",
            "gap_events",
            "published_feature_ids",
        }
        required = allowed - {"published_feature_ids"}
        unknown = sorted(set(value) - allowed)
        missing = sorted(required - set(value))
        if unknown or missing:
            details = []
            if unknown:
                details.append("unknown keys: " + ", ".join(unknown))
            if missing:
                details.append("missing keys: " + ", ".join(missing))
            raise ValueError("invalid timeframe state shape; " + "; ".join(details))

        def parse_extreme(raw: object) -> tuple[Decimal, datetime, int] | None:
            if raw is None:
                return None
            mapping = dict(_as_mapping(raw, "extreme"))
            return (
                Decimal(str(mapping["price"])),
                datetime.fromisoformat(str(mapping["time"])),
                _as_int(mapping["index"], "extreme.index"),
            )

        state = cls(timeframe=str(value["timeframe"]))
        state.symbol = None if value.get("symbol") is None else str(value["symbol"])
        state.lifecycle = LifecycleState(str(value["lifecycle"]))
        state.bars = [
            BarRecord.from_mapping(_as_mapping(item, "bars item"))
            for item in _as_sequence(value.get("bars", ()), "bars")
        ]
        state.bar_offset = _as_int(value.get("bar_offset", 0), "bar_offset")
        state.confirmed_pivots = [
            PivotPoint.from_mapping(_as_mapping(item, "confirmed_pivots item"))
            for item in _as_sequence(value.get("confirmed_pivots", ()), "confirmed_pivots")
        ]
        state.wave_legs = [
            WaveLeg.from_mapping(_as_mapping(item, "wave_legs item"))
            for item in _as_sequence(value.get("wave_legs", ()), "wave_legs")
        ]
        raw_provisional = value.get("provisional")
        state.provisional = (
            None
            if raw_provisional is None
            else ProvisionalPivot.from_mapping(_as_mapping(raw_provisional, "provisional"))
        )
        raw_structure = value.get("structure")
        state.structure = (
            None if raw_structure is None else StructureSnapshot.from_mapping(_as_mapping(raw_structure, "structure"))
        )
        state.hypotheses = [
            WaveHypothesis.from_mapping(_as_mapping(item, "hypotheses item"))
            for item in _as_sequence(value.get("hypotheses", ()), "hypotheses")
        ]
        last_open = value.get("last_open_time")
        state.last_open_time = None if last_open is None else _as_datetime(last_open, "last_open_time")
        state.last_fingerprint = None if value.get("last_fingerprint") is None else str(value["last_fingerprint"])
        state.processed_bars = _as_int(value.get("processed_bars", 0), "processed_bars")
        state.atr = None if value.get("atr") is None else Decimal(str(value["atr"]))
        state.previous_close = None if value.get("previous_close") is None else Decimal(str(value["previous_close"]))
        state.tr_seed = [Decimal(str(item)) for item in _as_sequence(value.get("tr_seed", ()), "tr_seed")]
        state.atr_values = [Decimal(str(item)) for item in _as_sequence(value.get("atr_values", ()), "atr_values")]
        direction = value.get("zigzag_direction")
        state.zigzag_direction = None if direction is None else WaveDirection(str(direction))
        state.initial_high = parse_extreme(value.get("initial_high"))
        state.initial_low = parse_extreme(value.get("initial_low"))
        state.gap_blocked = _as_bool(value.get("gap_blocked", False), "gap_blocked")
        state.recovery_count = _as_int(value.get("recovery_count", 0), "recovery_count")
        state.segment_id = _as_int(value.get("segment_id", 0), "segment_id")
        state.revision_counter = _as_int(value.get("revision_counter", 0), "revision_counter")
        state.publications = _as_int(value.get("publications", 0), "publications")
        state.duplicate_events = _as_int(value.get("duplicate_events", 0), "duplicate_events")
        state.gap_events = _as_int(value.get("gap_events", 0), "gap_events")
        raw_feature_ids = _as_mapping(value.get("published_feature_ids", {}), "published_feature_ids")
        state.published_feature_ids = {str(key): str(item) for key, item in raw_feature_ids.items()}
        return state
