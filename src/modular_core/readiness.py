from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .models import Candle
from .validation import require_nonblank


@dataclass(frozen=True, slots=True)
class ReadinessView:
    is_ready: bool
    available_bars: Mapping[str, int]
    required_bars: Mapping[str, int]
    missing_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.is_ready, bool):
            raise TypeError("is_ready must be bool")
        available = _freeze_counts(self.available_bars, "available_bars")
        required = _freeze_counts(self.required_bars, "required_bars")
        missing = _canonical_string_tuple(self.missing_requirements, "missing_requirements")
        if set(available) != set(required):
            raise ValueError("available_bars and required_bars must use the same keys")
        expected_missing = tuple(
            f"{timeframe}:{available[timeframe]}/{count}"
            for timeframe, count in required.items()
            if available[timeframe] < count
        )
        if missing != expected_missing:
            raise ValueError("missing_requirements is inconsistent with bar counts")
        if self.is_ready != (not expected_missing):
            raise ValueError("is_ready is inconsistent with missing_requirements")
        object.__setattr__(self, "available_bars", MappingProxyType(available))
        object.__setattr__(self, "required_bars", MappingProxyType(required))
        object.__setattr__(self, "missing_requirements", missing)


def calculate_readiness(
    *,
    base_timeframe: str,
    base_bars: Sequence[Candle],
    timeframe_bars: Mapping[str, Sequence[Candle]],
    warmup_bars: Mapping[str, int],
    required_timeframes: Sequence[str] = (),
) -> ReadinessView:
    base_timeframe = _canonical_string(base_timeframe, "base_timeframe")
    _require_sequence(base_bars, "base_bars")
    if not isinstance(timeframe_bars, Mapping):
        raise TypeError("timeframe_bars must be a mapping")
    normalized_timeframes: dict[str, Sequence[Candle]] = {}
    for raw_timeframe, series in timeframe_bars.items():
        timeframe = _canonical_string(raw_timeframe, "timeframe_bars key")
        _require_sequence(series, f"timeframe_bars[{timeframe!r}]")
        normalized_timeframes[timeframe] = series
    required = _freeze_counts(warmup_bars, "warmup_bars")
    requested = _canonical_string_tuple(required_timeframes, "required_timeframes")
    if len(requested) != len(set(requested)):
        raise ValueError("required_timeframes contains duplicates")
    for timeframe in requested:
        required.setdefault(timeframe, 1)
    available: dict[str, int] = {}
    missing: list[str] = []
    for timeframe, count in required.items():
        series = (
            base_bars if timeframe == base_timeframe else normalized_timeframes.get(timeframe, ())
        )
        available[timeframe] = len(series)
        if len(series) < count:
            missing.append(f"{timeframe}:{len(series)}/{count}")
    return ReadinessView(
        is_ready=not missing,
        available_bars=available,
        required_bars=required,
        missing_requirements=tuple(missing),
    )


def _freeze_counts(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    result: dict[str, int] = {}
    for raw_timeframe, raw_count in value.items():
        timeframe = _canonical_string(raw_timeframe, f"{name} key")
        if timeframe in result:
            raise ValueError(f"{name} contains duplicate keys")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            raise TypeError(f"{name} must contain integer counts")
        if raw_count < 0:
            raise ValueError(f"{name} counts cannot be negative")
        result[timeframe] = raw_count
    return result


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    require_nonblank(value, name)
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    return value


def _canonical_string_tuple(value: object, name: str) -> tuple[str, ...]:
    result = _require_sequence(value, name)
    return tuple(_canonical_string(item, name) for item in result)


def _require_sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    return value
