from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from modular_core import EventEnvelope, EventStore, InMemoryEventStore

from .canonical import canonical_json, canonical_sha256, require_json_mapping
from .errors import JournalIntegrityError

_MAX_EVENTS = 1_000_000
_RESERVED_FIELDS = frozenset({"aggregate_id", "payload_hash", "previous_hash", "event_hash"})
_SAFE_CHECKPOINT_EXTENSION_TYPES = frozenset(
    {
        "factual_event.processing_started",
        "factual_event.processed",
        "runtime.configuring",
        "runtime.recovery_completed",
        "runtime.stopped",
        "paper.reconciliation_completed",
        "paper.reconciliation_blocked",
        "notification.failed",
        "notification.dropped",
        "market_cycle.completed",
    }
)


def _identifier(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 256:
        raise ValueError(f"{name} exceeds maximum length")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _attestation_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JournalIntegrityError(f"journal attestation {name} must be non-negative int")
    return value


class RuntimeEventJournal:
    """Runner-owned append-only hash chain stored through the Core EventStore."""

    def __init__(self, *, run_id: str, store: EventStore | None = None) -> None:
        normalized_run_id = _identifier(run_id, "run_id")
        assert normalized_run_id is not None
        self.run_id = normalized_run_id
        self.store = store or InMemoryEventStore()
        events = tuple(self.store.read())
        if len(events) > _MAX_EVENTS:
            raise JournalIntegrityError("runtime journal exceeds event limit")
        if events and not self.verify_chain():
            raise JournalIntegrityError("existing runtime journal hash chain is invalid")
        self._sequence = events[-1].sequence if events else 0
        self._previous_hash = str(events[-1].payload["event_hash"]) if events else ""
        self._last_timestamp = events[-1].timestamp if events else None

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def previous_hash(self) -> str:
        return self._previous_hash

    def attestation(self) -> Mapping[str, object]:
        return {
            "run_id": self.run_id,
            "sequence": self._sequence,
            "event_count": len(tuple(self.store.read())),
            "last_event_hash": self._previous_hash,
        }

    def verify_attestation(self, state: Mapping[str, object]) -> None:
        """Verify an exact attestation or a safe, fully chained recovery-only extension."""
        if not isinstance(state, Mapping):
            raise TypeError("journal attestation must be a mapping")
        if not self.verify_chain():
            raise JournalIntegrityError("runtime journal hash chain is invalid")
        expected_run_id = state.get("run_id")
        if expected_run_id != self.run_id:
            raise JournalIntegrityError("journal attestation run_id mismatch")
        expected_sequence = _attestation_int(state.get("sequence"), "sequence")
        expected_count = _attestation_int(state.get("event_count"), "event_count")
        expected_hash = state.get("last_event_hash")
        if not isinstance(expected_hash, str):
            raise JournalIntegrityError("journal attestation last_event_hash must be str")
        if expected_sequence != expected_count:
            raise JournalIntegrityError("journal attestation sequence/count mismatch")
        events = tuple(self.store.read())
        if expected_sequence > len(events):
            raise JournalIntegrityError("journal attestation is ahead of persisted event store")
        if expected_sequence == 0:
            if expected_hash:
                raise JournalIntegrityError("empty journal attestation has a non-empty hash")
        elif events[expected_sequence - 1].payload.get("event_hash") != expected_hash:
            raise JournalIntegrityError("journal attestation prefix hash mismatch")
        extensions = events[expected_sequence:]
        unsafe_types = sorted(
            {
                event.event_type
                for event in extensions
                if event.event_type not in _SAFE_CHECKPOINT_EXTENSION_TYPES
            }
        )
        if unsafe_types:
            raise JournalIntegrityError(
                "journal attestation has unsafe extension event types: " + ", ".join(unsafe_types)
            )

    def append(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        symbol: str | None = None,
    ) -> EventEnvelope:
        moment = _utc(timestamp, "event timestamp")
        if self._last_timestamp is not None and moment < self._last_timestamp:
            raise JournalIntegrityError("event timestamp precedes previous journal event")
        normalized_event_type = _identifier(event_type, "event_type")
        normalized_aggregate_id = _identifier(aggregate_id, "aggregate_id")
        normalized_correlation_id = _identifier(
            correlation_id,
            "correlation_id",
            optional=True,
        )
        normalized_causation_id = _identifier(
            causation_id,
            "causation_id",
            optional=True,
        )
        normalized_symbol = _identifier(symbol, "symbol", optional=True)
        assert normalized_event_type is not None
        assert normalized_aggregate_id is not None
        if self._sequence >= _MAX_EVENTS:
            raise JournalIntegrityError("runtime journal exceeds event limit")
        normalized_payload = dict(require_json_mapping(payload, "journal payload"))
        reserved = sorted(set(normalized_payload).intersection(_RESERVED_FIELDS))
        if reserved:
            raise ValueError("journal payload contains reserved field(s): " + ", ".join(reserved))
        payload_hash = canonical_sha256(normalized_payload)
        sequence = self._sequence + 1
        event_hash = self._event_hash(
            run_id=self.run_id,
            sequence=sequence,
            timestamp=moment,
            event_type=normalized_event_type,
            aggregate_id=normalized_aggregate_id,
            payload_hash=payload_hash,
            previous_hash=self._previous_hash,
            correlation_id=normalized_correlation_id,
            causation_id=normalized_causation_id,
            symbol=normalized_symbol,
        )
        envelope = EventEnvelope(
            event_id=f"trr-{sequence:012d}-{event_hash[:16]}",
            run_id=self.run_id,
            sequence=sequence,
            timestamp=moment,
            event_type=normalized_event_type,
            source="trading_runner",
            payload={
                **normalized_payload,
                "aggregate_id": normalized_aggregate_id,
                "payload_hash": payload_hash,
                "previous_hash": self._previous_hash,
                "event_hash": event_hash,
            },
            schema_version="1",
            correlation_id=normalized_correlation_id,
            causation_id=normalized_causation_id,
            symbol=normalized_symbol,
        )
        self.store.append(envelope)
        self._sequence = sequence
        self._previous_hash = event_hash
        self._last_timestamp = moment
        return envelope

    def verify_chain(self) -> bool:
        previous_hash = ""
        previous_timestamp: datetime | None = None
        try:
            events = tuple(self.store.read())
            if len(events) > _MAX_EVENTS:
                return False
            for expected_sequence, event in enumerate(events, start=1):
                if (
                    event.run_id != self.run_id
                    or event.sequence != expected_sequence
                    or event.source != "trading_runner"
                    or event.schema_version != "1"
                ):
                    return False
                moment = _utc(event.timestamp, "event timestamp")
                if previous_timestamp is not None and moment < previous_timestamp:
                    return False
                payload = dict(event.payload)
                aggregate_id = payload.pop("aggregate_id")
                payload_hash = payload.pop("payload_hash")
                stored_previous_hash = payload.pop("previous_hash")
                event_hash = payload.pop("event_hash")
                if not all(
                    isinstance(item, str) and item
                    for item in (aggregate_id, payload_hash, event_hash)
                ):
                    return False
                if stored_previous_hash != previous_hash:
                    return False
                if canonical_sha256(payload) != payload_hash:
                    return False
                expected_hash = self._event_hash(
                    run_id=event.run_id,
                    sequence=event.sequence,
                    timestamp=moment,
                    event_type=event.event_type,
                    aggregate_id=aggregate_id,
                    payload_hash=payload_hash,
                    previous_hash=previous_hash,
                    correlation_id=event.correlation_id,
                    causation_id=event.causation_id,
                    symbol=event.symbol,
                )
                if event_hash != expected_hash:
                    return False
                if event.event_id != f"trr-{event.sequence:012d}-{expected_hash[:16]}":
                    return False
                previous_hash = event_hash
                previous_timestamp = moment
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def read(self, event_type: str | None = None) -> tuple[EventEnvelope, ...]:
        events = tuple(self.store.read())
        if event_type is None:
            return events
        normalized = _identifier(event_type, "event_type")
        assert normalized is not None
        return tuple(event for event in events if event.event_type == normalized)

    @staticmethod
    def _event_hash(
        *,
        run_id: str,
        sequence: int,
        timestamp: datetime,
        event_type: str,
        aggregate_id: str,
        payload_hash: str,
        previous_hash: str,
        correlation_id: str | None,
        causation_id: str | None,
        symbol: str | None,
    ) -> str:
        material = canonical_json(
            {
                "run_id": run_id,
                "sequence": sequence,
                "timestamp": timestamp,
                "event_type": event_type,
                "source": "trading_runner",
                "aggregate_id": aggregate_id,
                "payload_hash": payload_hash,
                "previous_hash": previous_hash,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "symbol": symbol,
            }
        )
        return sha256(material.encode("utf-8")).hexdigest()
