from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .events import EventEnvelope


@dataclass(frozen=True, slots=True)
class ReplayReport:
    valid: bool
    events: int
    run_id: str | None
    first_sequence: int | None
    last_sequence: int | None
    errors: tuple[str, ...]


def verify_event_stream(events: Iterable[EventEnvelope]) -> ReplayReport:
    if isinstance(events, (str, bytes)) or not isinstance(events, Iterable):
        raise TypeError("events must be an iterable of EventEnvelope")
    rows = tuple(events)
    if any(not isinstance(event, EventEnvelope) for event in rows):
        raise TypeError("events must contain EventEnvelope")

    errors: list[str] = []
    ids: set[str] = set()
    known_ids: set[str] = set()
    run_id = rows[0].run_id if rows else None
    previous_sequence: int | None = None
    previous_timestamp = None

    for event in rows:
        if event.run_id != event.run_id.strip():
            errors.append(f"non_canonical_run_id:{event.event_id}")
        if event.event_id in ids:
            errors.append(f"duplicate event_id:{event.event_id}")
        ids.add(event.event_id)
        if run_id is not None and event.run_id != run_id:
            errors.append(f"mixed run_id:{event.event_id}")
        if previous_sequence is not None:
            if event.sequence <= previous_sequence:
                errors.append(f"non_increasing_sequence:{event.event_id}")
            elif event.sequence != previous_sequence + 1:
                errors.append(
                    f"sequence_gap:{event.event_id}:{previous_sequence}->{event.sequence}"
                )
        if previous_timestamp is not None and event.timestamp < previous_timestamp:
            errors.append(f"non_monotonic_timestamp:{event.event_id}")
        if event.causation_id is not None and event.causation_id not in known_ids:
            errors.append(f"unknown_or_future_causation:{event.event_id}:{event.causation_id}")
        known_ids.add(event.event_id)
        previous_sequence = event.sequence
        previous_timestamp = event.timestamp

    first_sequence = rows[0].sequence if rows else None
    last_sequence = rows[-1].sequence if rows else None
    if first_sequence is not None and last_sequence is not None:
        sequence_count = last_sequence - first_sequence + 1
        if sequence_count != len(rows):
            errors.append(f"sequence_count_mismatch:{len(rows)}!={sequence_count}")

    return ReplayReport(
        valid=not errors,
        events=len(rows),
        run_id=run_id,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        errors=tuple(errors),
    )
