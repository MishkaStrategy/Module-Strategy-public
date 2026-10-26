from __future__ import annotations

import json
from collections import OrderedDict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from modular_core import Candle

from .errors import CriticalQueueOverflow, MarketDataRejected, MarketIdentityConflict

if TYPE_CHECKING:
    from .config import TradingRunnerConfig


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    return value


def _unique_text_tuple(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(_text(item, f"{name} item") for item in value)
    if not result:
        raise ValueError(f"{name} cannot be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} cannot contain duplicates")
    return result


class MarketEventStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    GAP = "gap"


@dataclass(frozen=True, slots=True)
class MarketDataPolicy:
    market: str
    instruments: tuple[str, ...]
    sources: tuple[str, ...]
    timeframes: tuple[str, ...]
    max_event_age_seconds: float
    max_future_skew_seconds: float
    max_tracked_events: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", _text(self.market, "market"))
        object.__setattr__(
            self,
            "instruments",
            _unique_text_tuple(self.instruments, "instruments"),
        )
        object.__setattr__(self, "sources", _unique_text_tuple(self.sources, "sources"))
        object.__setattr__(
            self,
            "timeframes",
            _unique_text_tuple(self.timeframes, "timeframes"),
        )
        for name in ("max_event_age_seconds", "max_future_skew_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            numeric = float(value)
            if numeric < 0 or not numeric < float("inf"):
                raise ValueError(f"{name} must be non-negative and finite")
            object.__setattr__(self, name, numeric)
        if isinstance(self.max_tracked_events, bool) or not isinstance(
            self.max_tracked_events, int
        ):
            raise TypeError("max_tracked_events must be int")
        if self.max_tracked_events < 1:
            raise ValueError("max_tracked_events must be positive")

    @classmethod
    def from_runner_config(cls, config: TradingRunnerConfig) -> MarketDataPolicy:
        return cls(
            market=config.market,
            instruments=config.instruments,
            sources=config.market_data_sources,
            timeframes=(config.base_timeframe,),
            max_event_age_seconds=config.stale_after_seconds,
            max_future_skew_seconds=config.max_future_skew_seconds,
            max_tracked_events=config.max_tracked_market_events,
        )


@dataclass(frozen=True, slots=True)
class MarketEventEnvelope:
    event_id: str
    source_id: str
    market: str
    sequence: int
    available_at: datetime
    received_at: datetime
    candle: Candle
    is_closed: bool = True
    metadata_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("event_id", "source_id", "market", "metadata_version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be int")
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        if not isinstance(self.candle, Candle):
            raise TypeError("candle must be modular_core.Candle")
        if not isinstance(self.is_closed, bool):
            raise TypeError("is_closed must be bool")
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if self.available_at < self.candle.close_time:
            raise ValueError("available_at cannot precede candle close_time")
        if self.received_at < self.available_at:
            raise ValueError("received_at cannot precede available_at")

    @property
    def stream_key(self) -> tuple[str, str, str, str]:
        return (
            self.source_id,
            self.market,
            self.candle.symbol,
            self.candle.timeframe,
        )

    @property
    def payload_hash(self) -> str:
        payload: dict[str, object] = {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "market": self.market,
            "sequence": self.sequence,
            "available_at": self.available_at.isoformat(),
            "is_closed": self.is_closed,
            "metadata_version": self.metadata_version,
            "candle": {
                "timestamp": self.candle.timestamp.isoformat(),
                "open": format(self.candle.open, ".17g"),
                "high": format(self.candle.high, ".17g"),
                "low": format(self.candle.low, ".17g"),
                "close": format(self.candle.close, ".17g"),
                "volume": format(self.candle.volume, ".17g"),
                "symbol": self.candle.symbol,
                "timeframe": self.candle.timeframe,
            },
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketValidationResult:
    status: MarketEventStatus
    data_gap: bool
    sequence_gap: int
    time_gap: bool


@dataclass(frozen=True, slots=True)
class _StreamCursor:
    sequence: int
    event_id: str
    payload_hash: str
    candle_open_time: datetime
    candle_close_time: datetime


class MarketEventValidator:
    def __init__(self, policy: MarketDataPolicy) -> None:
        if not isinstance(policy, MarketDataPolicy):
            raise TypeError("policy must be MarketDataPolicy")
        self.policy = policy
        self._streams: dict[tuple[str, str, str, str], _StreamCursor] = {}
        self._event_hashes: OrderedDict[str, str] = OrderedDict()

    def validate_freshness(self, event: MarketEventEnvelope, *, now: datetime) -> None:
        """Validate the supplied envelope's configured identity and timestamps only.

        This does not prove a fresh owner snapshot, source authenticity, sequence
        completeness, or RESUME admission, and it does not mutate validator state.
        """
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        now = _utc(now, "now")
        self._validate_identity(event)
        self._validate_time(event, now)

    def inspect(self, event: MarketEventEnvelope, *, now: datetime) -> MarketValidationResult:
        """Validate without advancing durable in-process ordering evidence."""
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        now = _utc(now, "now")
        self._validate_identity(event)
        duplicate = self._inspect_existing_identity(event)
        if duplicate is not None:
            return duplicate
        self._validate_time(event, now)
        return self._inspect_new_ordering(event)

    def commit(self, event: MarketEventEnvelope) -> None:
        """Advance ordering evidence only after the downstream pipeline succeeds."""
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        duplicate = self._inspect_existing_identity(event)
        if duplicate is not None:
            return
        self._inspect_new_ordering(event)
        self._streams[event.stream_key] = _StreamCursor(
            sequence=event.sequence,
            event_id=event.event_id,
            payload_hash=event.payload_hash,
            candle_open_time=event.candle.timestamp,
            candle_close_time=event.candle.close_time,
        )
        self._event_hashes[event.event_id] = event.payload_hash
        self._event_hashes.move_to_end(event.event_id)
        while len(self._event_hashes) > self.policy.max_tracked_events:
            self._event_hashes.popitem(last=False)

    def validate(self, event: MarketEventEnvelope, *, now: datetime) -> MarketValidationResult:
        """Combined helper for callers that have no downstream transactional work."""
        result = self.inspect(event, now=now)
        if result.status is not MarketEventStatus.DUPLICATE:
            self.commit(event)
        return result

    def _inspect_existing_identity(
        self,
        event: MarketEventEnvelope,
    ) -> MarketValidationResult | None:
        existing_event_hash = self._event_hashes.get(event.event_id)
        if existing_event_hash is None:
            return None
        if existing_event_hash != event.payload_hash:
            raise MarketIdentityConflict(
                "market event_id was reused with different canonical content",
                reason_code="event_identity_conflict",
            )
        return MarketValidationResult(
            status=MarketEventStatus.DUPLICATE,
            data_gap=False,
            sequence_gap=0,
            time_gap=False,
        )

    def _inspect_new_ordering(self, event: MarketEventEnvelope) -> MarketValidationResult:
        cursor = self._streams.get(event.stream_key)
        sequence_gap = 0
        time_gap = False
        if cursor is not None:
            if event.sequence <= cursor.sequence:
                raise MarketDataRejected(
                    "market event sequence is out of order",
                    reason_code="out_of_order_sequence",
                )
            if event.candle.timestamp <= cursor.candle_open_time:
                raise MarketDataRejected(
                    "candle open time did not advance",
                    reason_code="out_of_order_candle",
                )
            sequence_gap = max(0, event.sequence - cursor.sequence - 1)
            time_gap = event.candle.timestamp != cursor.candle_close_time

        data_gap = sequence_gap > 0 or time_gap
        return MarketValidationResult(
            status=MarketEventStatus.GAP if data_gap else MarketEventStatus.ACCEPTED,
            data_gap=data_gap,
            sequence_gap=sequence_gap,
            time_gap=time_gap,
        )

    def _validate_identity(self, event: MarketEventEnvelope) -> None:
        if event.metadata_version != "1":
            raise MarketDataRejected(
                "unsupported market metadata version",
                reason_code="unsupported_metadata_version",
            )
        if event.market != self.policy.market:
            raise MarketDataRejected("market mismatch", reason_code="market_mismatch")
        if event.source_id not in self.policy.sources:
            raise MarketDataRejected(
                "market source is not allowed",
                reason_code="source_mismatch",
            )
        if event.candle.symbol not in self.policy.instruments:
            raise MarketDataRejected(
                "instrument is not allowed",
                reason_code="instrument_mismatch",
            )
        if event.candle.timeframe not in self.policy.timeframes:
            raise MarketDataRejected(
                "timeframe is not allowed",
                reason_code="timeframe_mismatch",
            )
        if not event.is_closed:
            raise MarketDataRejected(
                "open candle is forbidden",
                reason_code="candle_not_closed",
            )

    def _validate_time(self, event: MarketEventEnvelope, now: datetime) -> None:
        future_limit = now + timedelta(seconds=self.policy.max_future_skew_seconds)
        if event.received_at > future_limit or event.available_at > future_limit:
            raise MarketDataRejected(
                "market event timestamp is in the future",
                reason_code="future_timestamp",
            )
        age = (now - event.available_at).total_seconds()
        if age > self.policy.max_event_age_seconds:
            raise MarketDataRejected("market event is stale", reason_code="stale_event")


class BoundedCriticalQueue[T]:
    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity must be int")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: deque[T] = deque()
        self._high_water_mark = 0

    def put(self, item: T) -> None:
        if len(self._items) >= self.capacity:
            raise CriticalQueueOverflow(
                "critical market queue capacity exceeded",
                reason_code="critical_queue_overflow",
            )
        self._items.append(item)
        self._high_water_mark = max(self._high_water_mark, len(self._items))

    def get(self) -> T:
        if not self._items:
            raise IndexError("critical market queue is empty")
        return self._items.popleft()

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def high_water_mark(self) -> int:
        return self._high_water_mark

    def snapshot(self) -> tuple[T, ...]:
        return tuple(self._items)
