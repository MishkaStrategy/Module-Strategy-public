from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from modular_core import PositionSide, PositionSnapshot

from .constants import STATE_SCHEMA_VERSION
from .decimal_utils import (
    MAX_DECIMAL_TEXT_CHARS,
    exact_decimal_text,
    optional_exact_decimal_text,
    validate_decimal_bounds,
)

_MAX_STATE_STRING_CHARS = 256


@dataclass(slots=True)
class PositionRuntimeState:
    position_id: str
    setup_id: str | None
    side: PositionSide
    entry_price: Decimal
    entry_time: datetime
    initial_quantity: Decimal
    initial_stop: Decimal | None
    initial_risk_per_unit: Decimal | None
    favorable_extreme: Decimal
    last_seen_quantity: Decimal
    last_stop: Decimal | None
    completed_partial_indices: set[int] = field(default_factory=set)
    pending_partial_index: int | None = None
    pending_partial_target_quantity: Decimal | None = None
    full_close_requested: bool = False
    breakeven_applied: bool = False
    trailing_active: bool = False
    bars_managed: int = 0

    def __post_init__(self) -> None:
        _nonblank(self.position_id, "position_id")
        if self.setup_id is not None:
            _nonblank(self.setup_id, "setup_id")
        self.entry_time = _aware_utc(self.entry_time, "entry_time")
        for name in (
            "entry_price",
            "initial_quantity",
            "favorable_extreme",
            "last_seen_quantity",
        ):
            _positive_decimal(getattr(self, name), name)
        for name in ("initial_stop", "last_stop"):
            value = getattr(self, name)
            if value is not None:
                _positive_decimal(value, name)
        if self.initial_risk_per_unit is not None:
            _positive_decimal(self.initial_risk_per_unit, "initial_risk_per_unit")
        if self.pending_partial_target_quantity is not None:
            _nonnegative_decimal(
                self.pending_partial_target_quantity,
                "pending_partial_target_quantity",
            )
        if self.bars_managed < 0:
            raise ValueError("bars_managed cannot be negative")
        if any(index < 0 for index in self.completed_partial_indices):
            raise ValueError("completed partial indices cannot be negative")
        expected_completed = set(range(len(self.completed_partial_indices)))
        if self.completed_partial_indices != expected_completed:
            raise ValueError("completed partial indices must form a contiguous prefix")
        if self.pending_partial_index is None:
            if self.pending_partial_target_quantity is not None:
                raise ValueError("pending partial target requires pending_partial_index")
        else:
            if self.pending_partial_index < 0:
                raise ValueError("pending_partial_index cannot be negative")
            if self.pending_partial_target_quantity is None:
                raise ValueError("pending partial index requires target quantity")
            if self.pending_partial_index in self.completed_partial_indices:
                raise ValueError("pending partial index cannot already be completed")
            if self.pending_partial_index != len(self.completed_partial_indices):
                raise ValueError("pending partial index must follow completed stages")
            if self.pending_partial_target_quantity >= self.last_seen_quantity:
                raise ValueError("pending partial target must be below last seen quantity")
        for name in ("full_close_requested", "breakeven_applied", "trailing_active"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")

        expected_risk = initial_risk(self.side, self.entry_price, self.initial_stop)
        if self.initial_stop is not None and expected_risk is None:
            raise ValueError("initial_stop must define positive initial risk")
        if self.initial_risk_per_unit != expected_risk:
            raise ValueError("initial risk is inconsistent with side, entry and initial stop")
        if self.side is PositionSide.LONG and self.favorable_extreme < self.entry_price:
            raise ValueError("long favorable_extreme cannot be below entry_price")
        if self.side is PositionSide.SHORT and self.favorable_extreme > self.entry_price:
            raise ValueError("short favorable_extreme cannot be above entry_price")

    @classmethod
    def from_position(
        cls,
        position: PositionSnapshot,
        current_price: Decimal,
    ) -> PositionRuntimeState:
        entry = Decimal(str(position.entry_price))
        quantity = Decimal(str(position.quantity))
        stop = None if position.stop_loss is None else Decimal(str(position.stop_loss))
        extreme = (
            max(entry, current_price)
            if position.side is PositionSide.LONG
            else min(entry, current_price)
        )
        return cls(
            position_id=position.id,
            setup_id=position.setup_id,
            side=position.side,
            entry_price=entry,
            entry_time=position.entry_time,
            initial_quantity=quantity,
            initial_stop=stop if initial_risk(position.side, entry, stop) is not None else None,
            initial_risk_per_unit=initial_risk(position.side, entry, stop),
            favorable_extreme=extreme,
            last_seen_quantity=quantity,
            last_stop=stop,
        )

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "position_id": self.position_id,
            "setup_id": self.setup_id,
            "side": self.side.value,
            "entry_price": decimal_text(self.entry_price),
            "entry_time": self.entry_time.isoformat(),
            "initial_quantity": decimal_text(self.initial_quantity),
            "initial_stop": optional_decimal_text(self.initial_stop),
            "initial_risk_per_unit": optional_decimal_text(self.initial_risk_per_unit),
            "favorable_extreme": decimal_text(self.favorable_extreme),
            "last_seen_quantity": decimal_text(self.last_seen_quantity),
            "last_stop": optional_decimal_text(self.last_stop),
            "completed_partial_indices": tuple(sorted(self.completed_partial_indices)),
            "pending_partial_index": self.pending_partial_index,
            "pending_partial_target_quantity": optional_decimal_text(
                self.pending_partial_target_quantity
            ),
            "full_close_requested": self.full_close_requested,
            "breakeven_applied": self.breakeven_applied,
            "trailing_active": self.trailing_active,
            "bars_managed": self.bars_managed,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        partial_rule_count: int,
    ) -> PositionRuntimeState:
        required = {
            "position_id",
            "setup_id",
            "side",
            "entry_price",
            "entry_time",
            "initial_quantity",
            "initial_stop",
            "initial_risk_per_unit",
            "favorable_extreme",
            "last_seen_quantity",
            "last_stop",
            "completed_partial_indices",
            "pending_partial_index",
            "pending_partial_target_quantity",
            "full_close_requested",
            "breakeven_applied",
            "trailing_active",
            "bars_managed",
        }
        unknown = sorted(set(value) - required)
        missing = sorted(required - set(value))
        if unknown:
            raise ValueError(f"unknown position state keys: {', '.join(unknown)}")
        if missing:
            raise ValueError(f"missing position state keys: {', '.join(missing)}")
        completed_raw = value["completed_partial_indices"]
        if not isinstance(completed_raw, list | tuple):
            raise ValueError("completed_partial_indices must be an array")
        completed_items = tuple(
            _strict_int(item, "completed partial index") for item in completed_raw
        )
        completed = set(completed_items)
        if completed_items != tuple(sorted(completed)):
            raise ValueError("completed_partial_indices must be sorted and unique")
        if any(index >= partial_rule_count for index in completed):
            raise ValueError("completed partial index exceeds configured rule count")
        pending_index = _optional_int(value["pending_partial_index"], "pending_partial_index")
        if pending_index is not None and pending_index >= partial_rule_count:
            raise ValueError("pending partial index exceeds configured rule count")
        return cls(
            position_id=_strict_string(value["position_id"], "position_id"),
            setup_id=(
                None if value["setup_id"] is None else _strict_string(value["setup_id"], "setup_id")
            ),
            side=PositionSide(_strict_string(value["side"], "side")),
            entry_price=_strict_decimal_text(value["entry_price"], "entry_price"),
            entry_time=_strict_datetime(value["entry_time"], "entry_time"),
            initial_quantity=_strict_decimal_text(value["initial_quantity"], "initial_quantity"),
            initial_stop=_optional_decimal(value["initial_stop"], "initial_stop"),
            initial_risk_per_unit=_optional_decimal(
                value["initial_risk_per_unit"], "initial_risk_per_unit"
            ),
            favorable_extreme=_strict_decimal_text(value["favorable_extreme"], "favorable_extreme"),
            last_seen_quantity=_strict_decimal_text(
                value["last_seen_quantity"], "last_seen_quantity"
            ),
            last_stop=_optional_decimal(value["last_stop"], "last_stop"),
            completed_partial_indices=completed,
            pending_partial_index=pending_index,
            pending_partial_target_quantity=_optional_decimal(
                value["pending_partial_target_quantity"],
                "pending_partial_target_quantity",
            ),
            full_close_requested=_strict_bool(
                value["full_close_requested"], "full_close_requested"
            ),
            breakeven_applied=_strict_bool(value["breakeven_applied"], "breakeven_applied"),
            trailing_active=_strict_bool(value["trailing_active"], "trailing_active"),
            bars_managed=_strict_positive_int(value["bars_managed"], "bars_managed"),
        )


@dataclass(slots=True)
class ModuleRuntimeState:
    config_fingerprint: str
    stream_symbol: str | None = None
    stream_timeframe: str | None = None
    last_close_time: datetime | None = None
    last_event_fingerprint: str | None = None
    active_position: PositionRuntimeState | None = None
    cancel_attempts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonblank(self.config_fingerprint, "config_fingerprint")
        if (self.stream_symbol is None) != (self.stream_timeframe is None):
            raise ValueError("stream symbol and timeframe must both be set or both be null")
        if self.stream_symbol is not None:
            _nonblank(self.stream_symbol, "stream_symbol")
            _nonblank(self.stream_timeframe or "", "stream_timeframe")
        if (self.last_close_time is None) != (self.last_event_fingerprint is None):
            raise ValueError(
                "last_close_time and last_event_fingerprint must both be set or both be null"
            )
        if self.last_close_time is not None:
            self.last_close_time = _aware_utc(self.last_close_time, "last_close_time")
        if self.last_event_fingerprint is not None:
            _nonblank(self.last_event_fingerprint, "last_event_fingerprint")
            if len(self.last_event_fingerprint) > 8192:
                raise ValueError("last_event_fingerprint exceeds safety bound")
        if self.stream_symbol is None:
            if (
                self.last_close_time is not None
                or self.active_position is not None
                or self.cancel_attempts
            ):
                raise ValueError("runtime state requires a bound stream")
        elif self.last_close_time is None:
            raise ValueError("bound stream requires an event cursor")
        for order_id, index in self.cancel_attempts.items():
            _nonblank(order_id, "cancel_attempt order_id")
            if index < 0:
                raise ValueError("cancel attempt index cannot be negative")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": self.config_fingerprint,
            "stream_symbol": self.stream_symbol,
            "stream_timeframe": self.stream_timeframe,
            "last_close_time": (
                None if self.last_close_time is None else self.last_close_time.isoformat()
            ),
            "last_event_fingerprint": self.last_event_fingerprint,
            "active_position": (
                None if self.active_position is None else self.active_position.to_mapping()
            ),
            "cancel_attempts": dict(sorted(self.cancel_attempts.items())),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        expected_config_fingerprint: str,
        partial_rule_count: int,
        max_tracked_cancels: int,
    ) -> ModuleRuntimeState:
        required = {
            "schema_version",
            "config_fingerprint",
            "stream_symbol",
            "stream_timeframe",
            "last_close_time",
            "last_event_fingerprint",
            "active_position",
            "cancel_attempts",
        }
        unknown = sorted(set(value) - required)
        missing = sorted(required - set(value))
        if unknown:
            raise ValueError(f"unknown module state keys: {', '.join(unknown)}")
        if missing:
            raise ValueError(f"missing module state keys: {', '.join(missing)}")
        if value["schema_version"] != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported position exit state schema")
        fingerprint = _strict_string(value["config_fingerprint"], "config_fingerprint")
        if fingerprint != expected_config_fingerprint:
            raise ValueError("position exit state config fingerprint mismatch")
        attempts_raw = value["cancel_attempts"]
        if not isinstance(attempts_raw, Mapping):
            raise ValueError("cancel_attempts must be a mapping")
        if len(attempts_raw) > max_tracked_cancels:
            raise ValueError("cancel_attempts exceeds configured bound")
        attempts: dict[str, int] = {}
        for key, item in attempts_raw.items():
            order_id = _strict_string(key, "cancel_attempt order_id")
            attempts[order_id] = _strict_int(item, "cancel attempt index")
        active_raw = value["active_position"]
        if active_raw is not None and not isinstance(active_raw, Mapping):
            raise ValueError("active_position must be a mapping or null")
        return cls(
            config_fingerprint=fingerprint,
            stream_symbol=(
                None
                if value["stream_symbol"] is None
                else _strict_string(value["stream_symbol"], "stream_symbol")
            ),
            stream_timeframe=(
                None
                if value["stream_timeframe"] is None
                else _strict_string(value["stream_timeframe"], "stream_timeframe")
            ),
            last_close_time=(
                None
                if value["last_close_time"] is None
                else _strict_datetime(value["last_close_time"], "last_close_time")
            ),
            last_event_fingerprint=(
                None
                if value["last_event_fingerprint"] is None
                else _strict_string(value["last_event_fingerprint"], "last_event_fingerprint")
            ),
            active_position=(
                None
                if active_raw is None
                else PositionRuntimeState.from_mapping(
                    active_raw,
                    partial_rule_count=partial_rule_count,
                )
            ),
            cancel_attempts=attempts,
        )


def initial_risk(
    side: PositionSide,
    entry_price: Decimal,
    stop_loss: Decimal | None,
) -> Decimal | None:
    if stop_loss is None:
        return None
    risk = entry_price - stop_loss if side is PositionSide.LONG else stop_loss - entry_price
    return risk if risk > 0 and risk.is_finite() else None


def decimal_text(value: Decimal) -> str:
    return exact_decimal_text(value)


def optional_decimal_text(value: Decimal | None) -> str | None:
    return optional_exact_decimal_text(value)


def _strict_decimal_text(value: object, name: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a canonical decimal string")
    if len(value) > MAX_DECIMAL_TEXT_CHARS:
        raise ValueError(f"{name} exceeds supported Decimal text length")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a canonical decimal string") from exc
    validate_decimal_bounds(parsed, name)
    if decimal_text(parsed) != value:
        raise ValueError(f"{name} must be a canonical decimal string")
    return parsed


def _optional_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _strict_decimal_text(value, name)


def _strict_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a canonical UTC ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a canonical UTC ISO-8601 string") from exc
    canonical = _aware_utc(parsed, name)
    if canonical.isoformat() != value:
        raise ValueError(f"{name} must be a canonical UTC ISO-8601 string")
    return canonical


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _strict_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a canonical non-blank string")
    if len(value) > _MAX_STATE_STRING_CHARS:
        raise ValueError(f"{name} exceeds {_MAX_STATE_STRING_CHARS} characters")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _strict_positive_int(value: object, name: str) -> int:
    parsed = _strict_int(value, name)
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _optional_int(value: object, name: str) -> int | None:
    return None if value is None else _strict_int(value, name)


def _nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a canonical non-blank string")
    if len(value) > _MAX_STATE_STRING_CHARS:
        raise ValueError(f"{name} exceeds {_MAX_STATE_STRING_CHARS} characters")


def _positive_decimal(value: Decimal, name: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    validate_decimal_bounds(value, name)


def _nonnegative_decimal(value: Decimal, name: str) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    validate_decimal_bounds(value, name)
