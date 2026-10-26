from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .config import LiquiditySweepConfig
from .constants import STATE_SCHEMA_VERSION
from .models import BarSnapshot, LiquidityLevel, LiquiditySide

BAR_STATE_KEYS = frozenset(
    {"timestamp", "close_time", "symbol", "timeframe", "open", "high", "low", "close"}
)
LEVEL_STATE_KEYS = frozenset(
    {
        "level_id",
        "feature_id",
        "side",
        "price",
        "symbol",
        "timeframe",
        "pivot_time",
        "confirmed_at",
        "confirmed_sequence",
    }
)
STAT_KEYS = (
    "processed_bars",
    "levels_created",
    "levels_swept",
    "levels_invalidated",
    "levels_expired",
    "levels_evicted",
    "rejected_candidates",
    "gap_resets",
    "stream_resets",
)


class StateOwner(Protocol):
    config: LiquiditySweepConfig
    _config_fingerprint: str
    _bars: deque[BarSnapshot]
    _levels: dict[str, LiquidityLevel]
    _sequence: int
    _last_bar: BarSnapshot | None
    _stats: dict[str, int]
    _needs_feature_republish: set[str]


def empty_stats() -> dict[str, int]:
    return {key: 0 for key in STAT_KEYS}


def snapshot_state(owner: StateOwner) -> Mapping[str, object]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "config_fingerprint": owner._config_fingerprint,
        "sequence": owner._sequence,
        "stats": dict(owner._stats),
        "bars": [bar.to_state() for bar in owner._bars],
        "levels": [level.to_state() for level in ordered_levels(owner._levels)],
        "last_bar": owner._last_bar.to_state() if owner._last_bar is not None else None,
    }


def restore_state(owner: StateOwner, state: Mapping[str, object]) -> None:
    if not isinstance(state, Mapping):
        raise ValueError("liquidity_sweep state must be a mapping")
    if not state:
        owner._bars.clear()
        owner._levels.clear()
        owner._sequence = 0
        owner._last_bar = None
        owner._stats = empty_stats()
        owner._needs_feature_republish.clear()
        return
    _require_string_keys(state, "liquidity_sweep state")

    allowed_keys = {
        "schema_version",
        "config_fingerprint",
        "sequence",
        "stats",
        "bars",
        "levels",
        "last_bar",
    }
    unknown_keys = sorted(set(state) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"unknown liquidity_sweep state keys: {', '.join(unknown_keys)}")
    missing_keys = sorted(allowed_keys - set(state))
    if missing_keys:
        raise ValueError(f"missing liquidity_sweep state keys: {', '.join(missing_keys)}")
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported liquidity_sweep state schema: {state.get('schema_version')!r}"
        )
    if state.get("config_fingerprint") != owner._config_fingerprint:
        raise ValueError("liquidity_sweep state configuration does not match module parameters")

    sequence = state.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("state.sequence must be a non-negative integer")
    stats = _parse_stats(state.get("stats"), sequence)

    raw_bars = _parse_sequence(state.get("bars"), "bars")
    raw_levels = _parse_sequence(state.get("levels"), "levels")
    if len(raw_bars) > owner.config.pivot_window_bars:
        raise ValueError("state contains more buffered bars than the configured pivot window")
    if len(raw_bars) > sequence:
        raise ValueError("state cannot contain more buffered bars than processed bars")
    if len(raw_levels) > owner.config.max_active_levels:
        raise ValueError("state contains more active levels than configured capacity")

    bars = [_bar_from_state(item) for item in raw_bars]
    levels = [_level_from_state(item) for item in raw_levels]
    if any(level.confirmed_sequence > sequence for level in levels):
        raise ValueError("level confirmed_sequence cannot exceed state.sequence")
    if len({level.level_id for level in levels}) != len(levels):
        raise ValueError("duplicate level_id in state")
    if levels and not bars:
        raise ValueError("active levels require buffered bars and last_bar in checkpoint state")
    closed_levels = (
        stats["levels_swept"]
        + stats["levels_invalidated"]
        + stats["levels_expired"]
        + stats["levels_evicted"]
    )
    if len(levels) > stats["levels_created"]:
        raise ValueError("active level count cannot exceed levels_created")
    if closed_levels + len(levels) > stats["levels_created"]:
        raise ValueError("level lifecycle counters exceed levels_created")

    raw_last_bar = state.get("last_bar")
    last_bar = None if raw_last_bar is None else _bar_from_state(raw_last_bar)
    if (bars or levels) and last_bar is None:
        raise ValueError("state.last_bar is required when buffered bars or levels exist")
    if last_bar is not None and sequence == 0:
        raise ValueError("state.sequence must be positive when last_bar exists")
    _validate_stream_state(bars, levels, last_bar)
    if (
        owner.config.timeframe != "base"
        and last_bar is not None
        and last_bar.timeframe != owner.config.timeframe
    ):
        raise ValueError("state timeframe does not match configured timeframe")

    ordered = ordered_levels({level.level_id: level for level in levels})
    owner._bars = deque(bars, maxlen=owner.config.pivot_window_bars)
    owner._levels = {level.level_id: level for level in ordered}
    owner._sequence = sequence
    owner._last_bar = last_bar
    owner._stats = stats
    owner._needs_feature_republish = set(owner._levels)


def ordered_levels(levels: Mapping[str, LiquidityLevel]) -> list[LiquidityLevel]:
    return sorted(
        levels.values(),
        key=lambda level: (
            level.confirmed_sequence,
            level.confirmed_at.isoformat(),
            level.level_id,
        ),
    )


def _parse_stats(value: object, sequence: int) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("state.stats must be a mapping")
    _require_string_keys(value, "state.stats")
    if set(value) != set(STAT_KEYS):
        raise ValueError("state.stats keys do not match the current statistics schema")
    stats: dict[str, int] = {}
    for key in STAT_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"state.stats.{key} must be a non-negative integer")
        stats[key] = item
    if stats["processed_bars"] != sequence:
        raise ValueError("state.stats.processed_bars must equal state.sequence")
    return stats


def _parse_sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"state.{field_name} must be a sequence")
    return value


def _validate_stream_state(
    bars: list[BarSnapshot],
    levels: list[LiquidityLevel],
    last_bar: BarSnapshot | None,
) -> None:
    if bars:
        for previous, current in zip(bars, bars[1:]):
            if current.timestamp <= previous.timestamp:
                raise ValueError("state buffered bars must be strictly time-ordered")
            if (current.symbol, current.timeframe) != (previous.symbol, previous.timeframe):
                raise ValueError("state buffered bars must share symbol and timeframe")
        if bars[-1] != last_bar:
            raise ValueError("state.last_bar must equal the latest buffered bar")
    if last_bar is not None:
        for level in levels:
            if (level.symbol, level.timeframe) != (last_bar.symbol, last_bar.timeframe):
                raise ValueError("state levels must match last_bar symbol and timeframe")
            if level.confirmed_at > last_bar.close_time:
                raise ValueError("level confirmation cannot be after state.last_bar")


def _validate_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    _require_string_keys(value, f"{label} state")
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown or missing:
        raise ValueError(f"invalid {label} state keys; missing={missing}, unknown={unknown}")


def _require_string_keys(value: Mapping[Any, Any], label: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")


def _parse_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal in state: {field_name}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite decimal in state: {field_name}")
    return parsed


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"invalid datetime in state: {field_name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid datetime in state: {field_name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _bar_from_state(value: object) -> BarSnapshot:
    if not isinstance(value, Mapping):
        raise ValueError("bar state must be a mapping")
    _validate_keys(value, BAR_STATE_KEYS, "bar")
    return BarSnapshot(
        timestamp=_parse_datetime(value.get("timestamp"), "bar.timestamp"),
        close_time=_parse_datetime(value.get("close_time"), "bar.close_time"),
        symbol=_parse_identifier(value.get("symbol"), "bar.symbol"),
        timeframe=_parse_timeframe(value.get("timeframe"), "bar.timeframe"),
        open=_parse_decimal(value.get("open"), "bar.open"),
        high=_parse_decimal(value.get("high"), "bar.high"),
        low=_parse_decimal(value.get("low"), "bar.low"),
        close=_parse_decimal(value.get("close"), "bar.close"),
    )


def _level_from_state(value: object) -> LiquidityLevel:
    if not isinstance(value, Mapping):
        raise ValueError("level state must be a mapping")
    _validate_keys(value, LEVEL_STATE_KEYS, "level")
    side_raw = value.get("side")
    if not isinstance(side_raw, str):
        raise ValueError("level.side must be a string")
    try:
        side = LiquiditySide(side_raw)
    except ValueError as exc:
        raise ValueError("invalid level side in state") from exc
    confirmed_sequence = value.get("confirmed_sequence")
    if (
        isinstance(confirmed_sequence, bool)
        or not isinstance(confirmed_sequence, int)
        or confirmed_sequence <= 0
    ):
        raise ValueError("level.confirmed_sequence must be a positive integer")
    level_id = _parse_identifier(value.get("level_id"), "level.level_id")
    feature_id = _parse_identifier(value.get("feature_id"), "level.feature_id")
    symbol = _parse_identifier(value.get("symbol"), "level.symbol")
    timeframe = _parse_timeframe(value.get("timeframe"), "level.timeframe")
    return LiquidityLevel(
        level_id=level_id,
        feature_id=feature_id,
        side=side,
        price=_parse_decimal(value.get("price"), "level.price"),
        symbol=symbol,
        timeframe=timeframe,
        pivot_time=_parse_datetime(value.get("pivot_time"), "level.pivot_time"),
        confirmed_at=_parse_datetime(value.get("confirmed_at"), "level.confirmed_at"),
        confirmed_sequence=confirmed_sequence,
    )


def _parse_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a trimmed non-blank string")
    return value


def _parse_timeframe(value: object, field_name: str) -> str:
    return _parse_identifier(value, field_name).lower()
