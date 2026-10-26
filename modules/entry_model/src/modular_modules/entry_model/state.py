from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

STATE_SCHEMA_VERSION = "2"
_ALLOWED_OUTCOMES = frozenset({"planned", "rejected", "duplicate", "conflict"})
_K = TypeVar("_K")
_V = TypeVar("_V")


@dataclass(frozen=True, slots=True)
class ProcessedRecord:
    fingerprint: str
    outcome: str
    output_id: str | None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    fingerprint: str
    outcome: str


class EntryModelState:
    def __init__(self, max_items: int, config_fingerprint: str) -> None:
        self.max_items = max_items
        self.config_fingerprint = config_fingerprint
        self.processed: OrderedDict[str, ProcessedRecord] = OrderedDict()
        self.seen_features: OrderedDict[str, SourceRecord] = OrderedDict()
        self.bound_symbol: str | None = None
        self.bound_timeframe: str | None = None
        self.last_bar_time: datetime | None = None
        self.last_bar_signature: tuple[str, ...] | None = None
        self.counters: dict[str, int] = {}

    def increment(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def remember(
        self,
        setup_id: str,
        fingerprint: str,
        outcome: str,
        output_id: str | None = None,
    ) -> None:
        self._validate_record(fingerprint, outcome)
        self.processed[setup_id] = ProcessedRecord(fingerprint, outcome, output_id)
        self.processed.move_to_end(setup_id)
        self._trim(self.processed)

    def remember_feature(self, source_id: str, fingerprint: str, outcome: str) -> None:
        self._validate_record(fingerprint, outcome)
        self.seen_features[source_id] = SourceRecord(fingerprint, outcome)
        self.seen_features.move_to_end(source_id)
        self._trim(self.seen_features)

    def snapshot(self) -> Mapping[str, object]:
        return {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": self.config_fingerprint,
            "stream": {
                "symbol": self.bound_symbol,
                "timeframe": self.bound_timeframe,
                "last_bar_time": (
                    self.last_bar_time.astimezone(UTC).isoformat() if self.last_bar_time else None
                ),
                "last_bar_signature": list(self.last_bar_signature)
                if self.last_bar_signature
                else None,
            },
            "processed": [
                {
                    "setup_id": setup_id,
                    "fingerprint": record.fingerprint,
                    "outcome": record.outcome,
                    "output_id": record.output_id,
                }
                for setup_id, record in self.processed.items()
            ],
            "seen_features": [
                {
                    "source_id": source_id,
                    "fingerprint": record.fingerprint,
                    "outcome": record.outcome,
                }
                for source_id, record in self.seen_features.items()
            ],
            "counters": dict(sorted(self.counters.items())),
        }

    def restore(self, state: Mapping[str, object]) -> None:
        allowed = {
            "state_schema_version",
            "config_fingerprint",
            "stream",
            "processed",
            "seen_features",
            "counters",
        }
        unknown = sorted(str(key) for key in set(state) - allowed)
        if unknown:
            raise ValueError(f"unknown entry model state keys: {', '.join(unknown)}")
        if state.get("state_schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("incompatible entry model state schema")
        if state.get("config_fingerprint") != self.config_fingerprint:
            raise ValueError("entry model state config fingerprint mismatch")
        processed = self._restore_processed(state.get("processed", ()))
        seen = self._restore_seen(state.get("seen_features", ()))
        stream = self._restore_stream(state.get("stream", {}))
        counters = self._restore_counters(state.get("counters", {}))
        # Atomic swap after every field has validated.
        self.processed = processed
        self.seen_features = seen
        (
            self.bound_symbol,
            self.bound_timeframe,
            self.last_bar_time,
            self.last_bar_signature,
        ) = stream
        self.counters = counters

    def _restore_processed(self, rows: object) -> OrderedDict[str, ProcessedRecord]:
        sequence = self._require_sequence(rows, "processed")
        if len(sequence) > self.max_items:
            raise ValueError("entry model processed state exceeds configured bound")
        restored: OrderedDict[str, ProcessedRecord] = OrderedDict()
        for row in sequence:
            if not isinstance(row, Mapping):
                raise ValueError("entry model processed row must be a mapping")
            allowed = {"setup_id", "fingerprint", "outcome", "output_id"}
            if set(row) - allowed:
                raise ValueError("unknown fields in entry model processed row")
            setup_id = str(row.get("setup_id", "")).strip()
            fingerprint = str(row.get("fingerprint", "")).strip()
            outcome = str(row.get("outcome", "")).strip()
            output_raw = row.get("output_id")
            output_id = None if output_raw is None else str(output_raw).strip()
            if not setup_id or (output_raw is not None and not output_id):
                raise ValueError("invalid entry model processed row")
            self._validate_record(fingerprint, outcome)
            if setup_id in restored:
                raise ValueError("duplicate setup_id in entry model state")
            restored[setup_id] = ProcessedRecord(fingerprint, outcome, output_id)
        return restored

    def _restore_seen(self, rows: object) -> OrderedDict[str, SourceRecord]:
        sequence = self._require_sequence(rows, "seen_features")
        if len(sequence) > self.max_items:
            raise ValueError("entry model seen-feature state exceeds configured bound")
        restored: OrderedDict[str, SourceRecord] = OrderedDict()
        for row in sequence:
            if not isinstance(row, Mapping):
                raise ValueError("entry model seen-feature row must be a mapping")
            allowed = {"source_id", "fingerprint", "outcome"}
            if set(row) - allowed:
                raise ValueError("unknown fields in entry model seen-feature row")
            source_id = str(row.get("source_id", "")).strip()
            fingerprint = str(row.get("fingerprint", "")).strip()
            outcome = str(row.get("outcome", "")).strip()
            if not source_id:
                raise ValueError("invalid entry model seen-feature row")
            self._validate_record(fingerprint, outcome)
            if source_id in restored:
                raise ValueError("duplicate source_id in entry model state")
            restored[source_id] = SourceRecord(fingerprint, outcome)
        return restored

    @staticmethod
    def _restore_stream(
        value: object,
    ) -> tuple[str | None, str | None, datetime | None, tuple[str, ...] | None]:
        if not isinstance(value, Mapping):
            raise ValueError("entry model stream state must be a mapping")
        allowed = {"symbol", "timeframe", "last_bar_time", "last_bar_signature"}
        if set(value) - allowed:
            raise ValueError("unknown fields in entry model stream state")
        symbol_raw = value.get("symbol")
        timeframe_raw = value.get("timeframe")
        symbol = None if symbol_raw is None else str(symbol_raw).strip()
        timeframe = None if timeframe_raw is None else str(timeframe_raw).strip()
        if (symbol is None) != (timeframe is None):
            raise ValueError("entry model stream symbol/timeframe must be both set or both absent")
        if symbol == "" or timeframe == "":
            raise ValueError("entry model stream values cannot be blank")
        time_raw = value.get("last_bar_time")
        last_bar_time: datetime | None = None
        if time_raw is not None:
            try:
                last_bar_time = datetime.fromisoformat(str(time_raw).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("invalid entry model stream last_bar_time") from exc
            if last_bar_time.tzinfo is None or last_bar_time.utcoffset() is None:
                raise ValueError("entry model stream last_bar_time must be timezone-aware")
            last_bar_time = last_bar_time.astimezone(UTC)
        signature_raw = value.get("last_bar_signature")
        signature: tuple[str, ...] | None
        if signature_raw is None:
            signature = None
        else:
            sequence = EntryModelState._require_sequence(signature_raw, "last_bar_signature")
            signature = tuple(str(item) for item in sequence)
            if len(signature) != 8 or any(not item for item in signature):
                raise ValueError("invalid entry model stream last_bar_signature")
        if (last_bar_time is None) != (signature is None):
            raise ValueError("entry model stream cursor must be complete")
        if last_bar_time is not None and symbol is None:
            raise ValueError("entry model cursor requires bound stream")
        return symbol, timeframe, last_bar_time, signature

    @staticmethod
    def _restore_counters(value: object) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("entry model counters must be a mapping")
        result: dict[str, int] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("entry model counter names must be nonblank strings")
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("entry model counters must be non-negative integers")
            result[key] = item
        return result

    @staticmethod
    def _require_sequence(value: object, name: str) -> Sequence[object]:
        if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
            raise ValueError(f"entry model state {name} must be a sequence")
        return value

    @staticmethod
    def _validate_record(fingerprint: str, outcome: str) -> None:
        if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
            raise ValueError("entry model state fingerprint must be lowercase SHA-256 hex")
        if outcome not in _ALLOWED_OUTCOMES:
            raise ValueError("invalid entry model state outcome")

    def _trim(self, mapping: OrderedDict[_K, _V]) -> None:
        while len(mapping) > self.max_items:
            mapping.popitem(last=False)
