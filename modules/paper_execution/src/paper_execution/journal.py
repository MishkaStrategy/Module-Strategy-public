from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping, overload

from modular_core.events import EventEnvelope, EventStore, InMemoryEventStore

from ._utils import canonical_json, sha256_text, utc

MAX_JOURNAL_PAYLOAD_ITEMS = 1_000_000
MAX_JOURNAL_EVENTS = 1_000_000
MAX_JOURNAL_IDENTIFIER_CHARS = 256
_RESERVED_PAYLOAD_FIELDS = frozenset(
    {
        "aggregate_id",
        "payload_hash",
        "previous_hash",
        "previous_event_hash",
        "event_hash",
    }
)


@overload
def _identifier(
    value: object,
    name: str,
    *,
    optional: Literal[False] = False,
) -> str: ...


@overload
def _identifier(
    value: object,
    name: str,
    *,
    optional: Literal[True],
) -> str | None: ...


def _identifier(
    value: object,
    name: str,
    *,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must be non-blank")
    if len(value) > MAX_JOURNAL_IDENTIFIER_CHARS:
        raise ValueError(f"{name} exceeds maximum length")
    return value


class ExecutionEventJournal:
    def __init__(self, run_id: str, store: EventStore | None = None) -> None:
        self.run_id = _identifier(run_id, "run_id")
        self.store = store or InMemoryEventStore()
        existing = tuple(self.store.read())
        if len(existing) > MAX_JOURNAL_EVENTS:
            raise ValueError("existing execution journal exceeds event limit")
        if existing and not self.verify_chain():
            raise ValueError("existing execution journal is invalid")
        self._sequence = existing[-1].sequence if existing else 0
        self._previous_hash = str(existing[-1].payload["event_hash"]) if existing else ""
        self._last_timestamp = existing[-1].timestamp if existing else None

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
        if not isinstance(state, Mapping):
            raise TypeError("journal attestation must be a mapping")
        expected = self.attestation()
        if dict(state) != dict(expected):
            raise ValueError("journal attestation does not match persisted event store")
        if not self.verify_chain():
            raise ValueError("execution journal hash chain is invalid")

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
        moment = utc(timestamp, "event timestamp")
        if self._last_timestamp is not None and moment < self._last_timestamp:
            raise ValueError("event timestamp precedes previous journal event")
        normalized_event_type = _identifier(event_type, "event_type")
        normalized_aggregate_id = _identifier(
            aggregate_id,
            "aggregate_id",
        )
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
        if not isinstance(payload, Mapping):
            raise TypeError("journal payload must be a mapping")
        if len(payload) > MAX_JOURNAL_PAYLOAD_ITEMS:
            raise ValueError("journal payload exceeds item limit")
        reserved = sorted(set(payload).intersection(_RESERVED_PAYLOAD_FIELDS))
        if reserved:
            raise ValueError("journal payload contains reserved field(s): " + ", ".join(reserved))
        if self._sequence >= MAX_JOURNAL_EVENTS:
            raise ValueError("execution journal exceeds event limit")
        user_payload = dict(payload)
        payload_json = canonical_json(user_payload)
        payload_hash = sha256_text(payload_json)
        next_sequence = self._sequence + 1
        event_hash = self._event_hash_v2(
            run_id=self.run_id,
            sequence=next_sequence,
            timestamp=moment,
            event_type=normalized_event_type,
            source="paper_execution",
            aggregate_id=normalized_aggregate_id,
            payload_hash=payload_hash,
            previous_hash=self._previous_hash,
            correlation_id=normalized_correlation_id,
            causation_id=normalized_causation_id,
            symbol=normalized_symbol,
        )
        event_id = f"pevr-{next_sequence:012d}-{event_hash[:16]}"
        envelope = EventEnvelope(
            event_id=event_id,
            run_id=self.run_id,
            sequence=next_sequence,
            timestamp=moment,
            event_type=normalized_event_type,
            source="paper_execution",
            payload={
                **user_payload,
                "aggregate_id": normalized_aggregate_id,
                "payload_hash": payload_hash,
                "previous_hash": self._previous_hash,
                "event_hash": event_hash,
            },
            schema_version="2",
            correlation_id=normalized_correlation_id,
            causation_id=normalized_causation_id,
            symbol=normalized_symbol,
        )
        self.store.append(envelope)
        self._sequence = next_sequence
        self._previous_hash = event_hash
        self._last_timestamp = moment
        return envelope

    def verify_chain(self) -> bool:
        previous = ""
        previous_timestamp: datetime | None = None
        try:
            events = tuple(self.store.read())
            if len(events) > MAX_JOURNAL_EVENTS:
                return False
            for expected_sequence, event in enumerate(events, start=1):
                if (
                    event.run_id != self.run_id
                    or event.sequence != expected_sequence
                    or event.source != "paper_execution"
                ):
                    return False
                moment = utc(event.timestamp, "event timestamp")
                if previous_timestamp is not None and moment < previous_timestamp:
                    return False
                payload = dict(event.payload)
                aggregate_id = payload.pop("aggregate_id")
                payload_hash = payload.pop("payload_hash")
                event_hash = payload.pop("event_hash")
                if event.schema_version == "1":
                    previous_hash = self._pop_legacy_previous_hash(payload)
                    expected = self._event_hash_v1(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        event_type=event.event_type,
                        aggregate_id=aggregate_id,
                        payload_hash=payload_hash,
                        previous_hash=previous,
                    )
                elif event.schema_version == "2":
                    previous_hash = payload.pop("previous_hash")
                    expected = self._event_hash_v2(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        timestamp=event.timestamp,
                        event_type=event.event_type,
                        source=event.source,
                        aggregate_id=aggregate_id,
                        payload_hash=payload_hash,
                        previous_hash=previous,
                        correlation_id=event.correlation_id,
                        causation_id=event.causation_id,
                        symbol=event.symbol,
                    )
                else:
                    return False
                if not all(
                    isinstance(value, str) and value
                    for value in (aggregate_id, payload_hash, event_hash)
                ):
                    return False
                if not isinstance(previous_hash, str) or previous_hash != previous:
                    return False
                if sha256_text(canonical_json(payload)) != payload_hash:
                    return False
                expected_event_id = f"pevr-{event.sequence:012d}-{expected[:16]}"
                if expected != event_hash or event.event_id != expected_event_id:
                    return False
                previous = event_hash
                previous_timestamp = moment
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _pop_legacy_previous_hash(payload: dict[str, Any]) -> object:
        if "previous_hash" in payload and "previous_event_hash" in payload:
            raise ValueError("legacy event contains duplicate previous hash fields")
        if "previous_hash" in payload:
            return payload.pop("previous_hash")
        return payload.pop("previous_event_hash")

    @staticmethod
    def _event_hash_v1(
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        aggregate_id: str,
        payload_hash: str,
        previous_hash: str,
    ) -> str:
        return sha256_text(
            f"{run_id}|{sequence}|{event_type}|{aggregate_id}|{payload_hash}|{previous_hash}"
        )

    @staticmethod
    def _event_hash_v2(
        *,
        run_id: str,
        sequence: int,
        timestamp: datetime,
        event_type: str,
        source: str,
        aggregate_id: str,
        payload_hash: str,
        previous_hash: str,
        correlation_id: str | None,
        causation_id: str | None,
        symbol: str | None,
    ) -> str:
        return sha256_text(
            canonical_json(
                {
                    "run_id": run_id,
                    "sequence": sequence,
                    "timestamp": timestamp,
                    "event_type": event_type,
                    "source": source,
                    "aggregate_id": aggregate_id,
                    "payload_hash": payload_hash,
                    "previous_hash": previous_hash,
                    "correlation_id": correlation_id,
                    "causation_id": causation_id,
                    "symbol": symbol,
                }
            )
        )
