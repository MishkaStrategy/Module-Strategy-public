from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .data import timeframe_to_seconds
from .models import Candle
from .series import ReadOnlySeries
from .validation import require_nonblank, utc_datetime


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)
_CALENDAR_ANCHOR_MICROSECONDS = 4 * 86_400 * 1_000_000


def _require_positive_int(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")


def _require_optional_positive_int(value: object, name: str) -> None:
    if value is None:
        return
    _require_positive_int(value, name)


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")


def _require_optional_positive_interval(value: object, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, timedelta):
        raise TypeError(f"{name} must be timedelta or None")
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    require_nonblank(value, name)
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    return value


def _validate_timezone(value: object) -> str:
    timezone_name = _canonical_string(value, "alignment_timezone")
    ZoneInfo(timezone_name)
    return timezone_name


def _validate_output_duration(
    output_timeframe: str,
    ratio: int,
    expected_base_interval: timedelta | None,
) -> None:
    if expected_base_interval is None:
        return
    output_seconds = timeframe_to_seconds(output_timeframe)
    if output_seconds is None:
        return
    expected_seconds = expected_base_interval.total_seconds() * ratio
    if expected_seconds != output_seconds:
        raise ValueError("output timeframe duration must equal ratio * expected_base_interval")


def _utc_microseconds(value: datetime) -> int:
    delta = value - _EPOCH_UTC
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class AggregatedCandle(Candle):
    """Core-produced Candle carrying the actual close of consumed base data."""

    closed_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        Candle.__post_init__(self)
        closed_at = utc_datetime(self.closed_at, "AggregatedCandle closed_at")
        if closed_at <= self.timestamp:
            raise ValueError("AggregatedCandle closed_at must be after timestamp")
        object.__setattr__(self, "closed_at", closed_at)

    @property
    def close_time(self) -> datetime:
        return self.closed_at


@dataclass(frozen=True, slots=True)
class TimeframeSpec:
    ratio: int
    output_timeframe: str
    gap_safe: bool = True
    align_to_clock: bool = False
    max_history: int | None = None
    alignment_timezone: str = "UTC"

    def __post_init__(self) -> None:
        _require_positive_int(self.ratio, "ratio")
        output_timeframe = _canonical_string(self.output_timeframe, "output_timeframe")
        _require_bool(self.gap_safe, "gap_safe")
        _require_bool(self.align_to_clock, "align_to_clock")
        _require_optional_positive_int(self.max_history, "max_history")
        alignment_timezone = _validate_timezone(self.alignment_timezone)
        object.__setattr__(self, "output_timeframe", output_timeframe)
        object.__setattr__(self, "alignment_timezone", alignment_timezone)


@dataclass(slots=True)
class TimeframeAggregator:
    """Closed-bar aggregator with gap reset and DST-aware calendar boundaries."""

    ratio: int
    output_timeframe: str
    expected_base_interval: timedelta | None = None
    gap_safe: bool = True
    align_to_clock: bool = False
    alignment_timezone: str = "UTC"
    _buffer: list[Candle] = field(default_factory=list, init=False, repr=False)
    _last_timestamp: datetime | None = field(default=None, init=False, repr=False)
    _active_bucket: datetime | None = field(default=None, init=False, repr=False)
    _symbol: str | None = field(default=None, init=False, repr=False)
    _base_timeframe: str | None = field(default=None, init=False, repr=False)
    _alignment_zone: ZoneInfo = field(init=False, repr=False)
    _output_microseconds: int | None = field(default=None, init=False, repr=False)
    _utc_fast_path: bool = field(default=False, init=False, repr=False)
    gaps_detected: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        _require_positive_int(self.ratio, "ratio")
        self.output_timeframe = _canonical_string(self.output_timeframe, "output_timeframe")
        _require_optional_positive_interval(self.expected_base_interval, "expected_base_interval")
        _require_bool(self.gap_safe, "gap_safe")
        _require_bool(self.align_to_clock, "align_to_clock")
        if self.align_to_clock and self.expected_base_interval is None:
            raise ValueError("clock alignment requires expected_base_interval")
        self.alignment_timezone = _validate_timezone(self.alignment_timezone)
        self._alignment_zone = ZoneInfo(self.alignment_timezone)
        self._utc_fast_path = self.alignment_timezone == "UTC"
        if self.expected_base_interval is not None:
            self._output_microseconds = (
                int(self.expected_base_interval.total_seconds()) * self.ratio * 1_000_000
            )
        _validate_output_duration(
            self.output_timeframe,
            self.ratio,
            self.expected_base_interval,
        )

    def validate_push(self, candle: Candle) -> None:
        if not isinstance(candle, Candle):
            raise TypeError("timeframe aggregator requires Candle")
        if self._symbol is not None:
            if candle.symbol != self._symbol:
                raise ValueError("timeframe aggregator cannot mix symbols")
            if candle.timeframe != self._base_timeframe:
                raise ValueError("timeframe aggregator cannot mix base timeframes")
        if self._last_timestamp is not None:
            if candle.timestamp == self._last_timestamp:
                raise ValueError("duplicate candle timestamp")
            if candle.timestamp < self._last_timestamp:
                raise ValueError("candles must be strictly chronological")

    def push(self, candle: Candle) -> Candle | None:
        self.validate_push(candle)
        self._bind_stream(candle)
        if self._last_timestamp is not None and self.expected_base_interval is not None:
            actual = candle.timestamp - self._last_timestamp
            if actual != self.expected_base_interval:
                self.gaps_detected += 1
                if self.gap_safe:
                    self._reset_bucket()
        self._last_timestamp = candle.timestamp

        if self.align_to_clock:
            return self._push_calendar_aligned(candle)
        return self._push_fixed_ratio(candle)

    def _push_fixed_ratio(self, candle: Candle) -> Candle | None:
        self._buffer.append(candle)
        if len(self._buffer) < self.ratio:
            return None
        if len(self._buffer) > self.ratio:
            self._buffer = self._buffer[-self.ratio :]
        group = tuple(self._buffer)
        self._buffer.clear()
        return self._aggregate(group)

    def _push_calendar_aligned(self, candle: Candle) -> Candle | None:
        if self.expected_base_interval is None:
            raise RuntimeError("clock-aligned aggregator missing expected_base_interval")
        bucket = self._bucket_start(candle.timestamp)
        if not self._buffer:
            if candle.timestamp != bucket:
                self._active_bucket = bucket
                return None
            self._active_bucket = bucket
        elif bucket != self._active_bucket:
            self._reset_bucket()
            if candle.timestamp != bucket:
                self._active_bucket = bucket
                return None
            self._active_bucket = bucket

        self._buffer.append(candle)
        next_open = candle.timestamp + self.expected_base_interval
        if self._bucket_start(next_open) == bucket:
            return None
        group = tuple(self._buffer)
        self._buffer.clear()
        self._active_bucket = None
        return self._aggregate(group)

    def _bucket_start(self, timestamp: datetime) -> datetime:
        if self.expected_base_interval is None or self._output_microseconds is None:
            raise RuntimeError("clock-aligned aggregator missing expected_base_interval")
        if self._utc_fast_path:
            elapsed = _utc_microseconds(timestamp)
            bucket_units = (elapsed - _CALENDAR_ANCHOR_MICROSECONDS) // self._output_microseconds
            bucket = _CALENDAR_ANCHOR_MICROSECONDS + bucket_units * self._output_microseconds
            return _EPOCH_UTC + timedelta(microseconds=bucket)
        local = timestamp.astimezone(self._alignment_zone)
        local_naive = local.replace(tzinfo=None)
        anchor = datetime(1970, 1, 5)
        elapsed_delta = local_naive - anchor
        elapsed_microseconds = (
            elapsed_delta.days * 86_400 + elapsed_delta.seconds
        ) * 1_000_000 + elapsed_delta.microseconds
        bucket_units = elapsed_microseconds // self._output_microseconds
        start_naive = anchor + timedelta(microseconds=bucket_units * self._output_microseconds)
        return start_naive.replace(tzinfo=self._alignment_zone).astimezone(timestamp.tzinfo)

    def _aggregate(self, group: tuple[Candle, ...]) -> Candle:
        timestamp = group[0].timestamp
        open_price = group[0].open
        high = max(c.high for c in group)
        low = min(c.low for c in group)
        close = group[-1].close
        volume = sum(c.volume for c in group)
        symbol = group[0].symbol
        closed_at = self._actual_close_time(group)
        if closed_at is None:
            return Candle(
                timestamp,
                open_price,
                high,
                low,
                close,
                volume,
                symbol,
                self.output_timeframe,
            )
        return AggregatedCandle(
            timestamp,
            open_price,
            high,
            low,
            close,
            volume,
            symbol,
            self.output_timeframe,
            closed_at=closed_at,
        )

    def _actual_close_time(self, group: tuple[Candle, ...]) -> datetime | None:
        if self.expected_base_interval is not None:
            return group[-1].timestamp + self.expected_base_interval
        derived = group[-1].close_time
        return derived if derived > group[-1].timestamp else None

    def reset(self) -> None:
        self._reset_bucket()
        self._last_timestamp = None
        self._symbol = None
        self._base_timeframe = None

    def _reset_bucket(self) -> None:
        self._buffer.clear()
        self._active_bucket = None

    def _bind_stream(self, candle: Candle) -> None:
        if self._symbol is None:
            self._symbol = candle.symbol
            self._base_timeframe = candle.timeframe


class TimeframeStore:
    def __init__(
        self,
        specs: Iterable[TimeframeSpec],
        *,
        expected_base_interval: timedelta | None,
    ) -> None:
        if isinstance(specs, (str, bytes)) or not isinstance(specs, Iterable):
            raise TypeError("timeframe specs must be an iterable")
        self._specs = tuple(specs)
        if any(not isinstance(spec, TimeframeSpec) for spec in self._specs):
            raise TypeError("timeframe specs must contain TimeframeSpec values")
        _require_optional_positive_interval(expected_base_interval, "expected_base_interval")
        names = [spec.output_timeframe for spec in self._specs]
        if len(names) != len(set(names)):
            raise ValueError("timeframe output names must be unique")
        self._aggregators = {
            spec.output_timeframe: TimeframeAggregator(
                ratio=spec.ratio,
                output_timeframe=spec.output_timeframe,
                expected_base_interval=expected_base_interval,
                gap_safe=spec.gap_safe,
                align_to_clock=spec.align_to_clock,
                alignment_timezone=spec.alignment_timezone,
            )
            for spec in self._specs
        }
        self._bars: dict[str, list[Candle]] = {name: [] for name in names}
        self._max_history = {spec.output_timeframe: spec.max_history for spec in self._specs}
        self._trim_threshold = {
            spec.output_timeframe: None
            if spec.max_history is None
            else spec.max_history + max(64, min(1_024, max(1, spec.max_history // 4)))
            for spec in self._specs
        }
        self._last_gap_counts = {name: 0 for name in names}
        self._snapshot_view: Mapping[str, ReadOnlySeries[Candle]] | None = None

    def push(self, candle: Candle) -> bool:
        if not isinstance(candle, Candle):
            raise TypeError("timeframe store requires Candle")
        for aggregator in self._aggregators.values():
            aggregator.validate_push(candle)
        gap_detected = False
        snapshot_changed = False
        for spec in self._specs:
            aggregator = self._aggregators[spec.output_timeframe]
            output = aggregator.push(candle)
            previous_gap_count = self._last_gap_counts[spec.output_timeframe]
            if aggregator.gaps_detected > previous_gap_count:
                gap_detected = True
                self._last_gap_counts[spec.output_timeframe] = aggregator.gaps_detected
            if output is not None:
                name = spec.output_timeframe
                series = self._bars[name]
                series.append(output)
                threshold = self._trim_threshold[name]
                if threshold is not None and len(series) > threshold:
                    max_history = self._max_history[name]
                    if max_history is None:
                        raise RuntimeError("timeframe history limit disappeared")
                    self._bars[name] = series[-max_history:]
                snapshot_changed = True
        if snapshot_changed:
            self._snapshot_view = None
        return gap_detected

    def snapshot_view(self) -> Mapping[str, ReadOnlySeries[Candle]]:
        if self._snapshot_view is None:
            snapshots: dict[str, ReadOnlySeries[Candle]] = {}
            for name, series in self._bars.items():
                max_history = self._max_history[name]
                start = 0 if max_history is None else max(0, len(series) - max_history)
                snapshots[name] = ReadOnlySeries(series, start=start)
            self._snapshot_view = MappingProxyType(snapshots)
        return self._snapshot_view

    def snapshot(self) -> dict[str, ReadOnlySeries[Candle]]:
        return dict(self.snapshot_view())

    def retention_counts(self) -> dict[str, int]:
        """Return actual underlying retained candle counts for diagnostics."""
        return {name: len(series) for name, series in self._bars.items()}
