"""Read-only Paper/Core attempt evidence and an opt-in durable pre-dispatch link.

This module does not dispatch, replay, cancel, accept, unblock, or repair an
attempt. A link can be appended only after an exact persisted Paper intent and
complete durable Core submit request have been independently read and matched.
The correspondence check covers plan ID, plan content hash, intent ID, and
client-order ID only. It does not verify side, quantity, prices, normalization,
metadata, mark, time, or index correspondence to the original plan. A Core
receipt match therefore says nothing about Paper plan acceptance.
The default Paper coordinator does not call this helper.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from modular_core.events import EventEnvelope
from modular_core.lifecycle import (
    CoreEffectLookupEvidence,
    CoreEffectLookupStatus,
    CoreLifecycleEffect,
)
from modular_core.lifecycle_venue import DurableCorePaperEffectSource

from .journal import ExecutionEventJournal


class PaperAttemptInspectionStatus(StrEnum):
    CORE_RECEIPT_MATCHED = "CORE_RECEIPT_MATCHED"
    ABSENT_AT_READ = "ABSENT_AT_READ"
    CONFLICT = "CONFLICT"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED_LEGACY = "BLOCKED_LEGACY"
    BLOCKED_INVALID_LINK = "BLOCKED_INVALID_LINK"


class PaperIntentCorrespondence(StrEnum):
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class PaperPendingAttemptLink:
    plan_id: str
    plan_content_hash: str
    intent_id: str
    effect_id: str
    request_digest: str

    def __post_init__(self) -> None:
        _text(self.plan_id, "plan_id")
        _digest(self.plan_content_hash, "plan_content_hash")
        _text(self.intent_id, "intent_id")
        _text(self.effect_id, "effect_id")
        _digest(self.request_digest, "request_digest")


@dataclass(frozen=True, slots=True)
class PaperAttemptInspection:
    status: PaperAttemptInspectionStatus
    plan_id: str | None
    plan_content_hash: str | None
    intent_id: str | None
    effect_id: str | None
    request_digest: str | None
    observed_commit_sequence: int | None
    observed_event_sequence: int | None
    reason: str
    intent_correspondence: PaperIntentCorrespondence = PaperIntentCorrespondence.UNVERIFIED


class _EffectLookupPort(Protocol):
    def lookup_effect(self, request: CoreLifecycleEffect) -> CoreEffectLookupEvidence: ...


def persist_pending_attempt_link(
    journal: ExecutionEventJournal,
    effect_source: DurableCorePaperEffectSource,
    request: CoreLifecycleEffect,
    *,
    plan_id: str,
    plan_content_hash: str,
    intent_id: str,
) -> EventEnvelope:
    """Durably link one saved Paper submit intent to its pre-saved Core request.

    Only an fsync-backed JSONL event journal is accepted here. The helper is an
    explicit seam for a trusted caller; it is intentionally not wired into the
    default coordinator or any Runner/Paper production composition.
    """
    if type(journal) is not ExecutionEventJournal:
        raise TypeError("journal must be ExecutionEventJournal")
    from modular_core.events import JsonLinesEventStore

    if type(journal.store) is not JsonLinesEventStore or journal.store.fsync is not True:
        raise ValueError("pending attempt links require an fsync-backed JSONL event store")
    if not journal.verify_chain():
        raise ValueError("Paper event journal chain is invalid")
    if type(request) is not CoreLifecycleEffect or request.operation != "submit_order":
        raise ValueError("only a complete saved Core submit_order request can be linked")
    request.__post_init__()
    link = PaperPendingAttemptLink(
        plan_id, plan_content_hash, intent_id, request.effect_id, request.request_digest
    )
    saved = effect_source.load_effect(request.effect_id)
    if type(saved) is not CoreLifecycleEffect or saved != request:
        raise ValueError("durable Core effect request does not exactly match supplied request")

    events = tuple(journal.store.read())
    intent_event, client_order_id = _find_saved_intent(
        events, plan_id=plan_id, content_hash=plan_content_hash, intent_id=intent_id
    )
    _require_request_matches_intent(request, client_order_id)

    existing = tuple(
        event for event in events
        if event.event_type == "execution.core_effect_pending"
        and event.payload.get("plan_id") == plan_id
        and event.payload.get("intent_id") == intent_id
    )
    if existing:
        if len(existing) == 1 and _link_from_event(existing[0]) == link:
            return existing[0]
        raise ValueError("Paper intent already has a conflicting pending Core link")
    if any(
        event.sequence > intent_event.sequence
        and event.payload.get("plan_id") == plan_id
        and event.event_type != "execution.core_effect_pending"
        for event in events
    ):
        raise ValueError("Paper execution advanced before the durable Core link")

    return journal.append(
        timestamp=intent_event.timestamp,
        event_type="execution.core_effect_pending",
        aggregate_id=plan_id,
        payload={
            "plan_id": link.plan_id,
            "plan_content_hash": link.plan_content_hash,
            "intent_id": link.intent_id,
            "effect_id": link.effect_id,
            "request_digest": link.request_digest,
        },
        correlation_id=plan_id,
        causation_id=intent_event.event_id,
        symbol=intent_event.symbol,
    )


def inspect_pending_attempts(
    journal: ExecutionEventJournal,
    effect_source: DurableCorePaperEffectSource,
    core_port: _EffectLookupPort,
) -> tuple[PaperAttemptInspection, ...]:
    """Inspect links and Core receipts without changing either store."""
    if type(journal) is not ExecutionEventJournal:
        raise TypeError("journal must be ExecutionEventJournal")
    if not callable(getattr(effect_source, "load_effect", None)):
        raise TypeError("effect_source must expose load_effect()")
    if not callable(getattr(core_port, "lookup_effect", None)):
        raise TypeError("core_port must expose read-only lookup_effect()")
    if not journal.verify_chain():
        return (_inspection(None, None, None, None, None, "Paper journal chain is invalid"),)

    events = tuple(journal.store.read())
    intents: dict[tuple[str, str], tuple[str, EventEnvelope, str | None]] = {}
    terminal_events: dict[tuple[str, str], set[str]] = {}
    malformed: list[PaperAttemptInspection] = []
    for event in events:
        if event.event_type in {"plan.accepted", "execution.failed"}:
            try:
                plan_id = _text_value(event.payload.get("plan_id"), "plan_id")
                content_hash = _digest_value(
                    event.payload.get("content_hash"), "content_hash"
                )
                if (
                    event.payload.get("aggregate_id") != plan_id
                    or event.correlation_id != plan_id
                ):
                    raise ValueError("terminal Paper event envelope does not match payload")
                terminal_events.setdefault((plan_id, content_hash), set()).add(
                    event.event_type
                )
            except (TypeError, ValueError) as exc:
                malformed.append(_inspection(None, None, None, None, None, str(exc)))
            continue
        if event.event_type != "execution.intent_recorded":
            continue
        try:
            plan_id = _text_value(event.payload.get("plan_id"), "plan_id")
            content_hash = _digest_value(event.payload.get("content_hash"), "content_hash")
            rows = event.payload.get("intents")
            if not isinstance(rows, (list, tuple)):
                raise ValueError("saved intent rows are unavailable")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("saved intent row is malformed")
                intent_id = _text_value(row.get("intent_id"), "intent_id")
                raw_client_id = row.get("client_order_id")
                client_id = (
                    None
                    if raw_client_id is None
                    else _text_value(raw_client_id, "client_order_id")
                )
                key = (plan_id, intent_id)
                if key in intents:
                    raise ValueError("duplicate saved Paper intent identity")
                intents[key] = (content_hash, event, client_id)
        except (TypeError, ValueError) as exc:
            malformed.append(_inspection(None, None, None, None, None, str(exc)))

    links: dict[tuple[str, str], list[tuple[PaperPendingAttemptLink, EventEnvelope]]] = {}
    for event in events:
        if event.event_type != "execution.core_effect_pending":
            continue
        try:
            link = _link_from_event(event)
            if (
                event.payload.get("aggregate_id") != link.plan_id
                or event.correlation_id != link.plan_id
            ):
                raise ValueError("pending link event envelope does not match its payload")
            links.setdefault((link.plan_id, link.intent_id), []).append((link, event))
        except (TypeError, ValueError) as exc:
            malformed.append(_inspection(None, None, None, None, None, str(exc)))

    results = malformed
    for key, (content_hash, intent_event, client_id) in intents.items():
        terminal_types = terminal_events.get((key[0], content_hash), set())
        if not terminal_types:
            is_pending = True
        elif len(terminal_types) == 1:
            is_pending = False
        else:
            results.append(
                _inspection(
                    key[0], content_hash, key[1], None, None,
                    "Paper plan has conflicting terminal outcomes",
                    status=PaperAttemptInspectionStatus.CONFLICT,
                )
            )
            continue
        matching_links = links.pop(key, [])
        if not matching_links:
            if not is_pending:
                continue
            results.append(
                PaperAttemptInspection(
                    PaperAttemptInspectionStatus.BLOCKED_LEGACY,
                    key[0], content_hash, key[1], None, None,
                    None, None, "saved Paper intent has no durable Core attempt link",
                )
            )
            continue
        if len(matching_links) != 1:
            results.append(
                _inspection(
                    key[0], content_hash, key[1], matching_links[0][0].effect_id,
                    matching_links[0][0].request_digest,
                    "Paper intent has multiple pending Core links",
                    status=PaperAttemptInspectionStatus.CONFLICT,
                )
            )
            continue
        link, link_event = matching_links[0]
        if (
            link.plan_content_hash != content_hash
            or link_event.causation_id != intent_event.event_id
            or client_id is None
        ):
            results.append(
                _inspection(
                    key[0], content_hash, key[1], link.effect_id, link.request_digest,
                    "pending Core link does not match the saved Paper intent",
                    status=PaperAttemptInspectionStatus.CONFLICT,
                )
            )
            continue
        try:
            request = effect_source.load_effect(link.effect_id)
        except Exception as exc:
            results.append(
                _inspection(
                    key[0], content_hash, key[1], link.effect_id, link.request_digest,
                    f"saved Core request unavailable ({type(exc).__name__})",
                    status=PaperAttemptInspectionStatus.UNAVAILABLE,
                )
            )
            continue
        try:
            if (
                type(request) is not CoreLifecycleEffect
                or request.effect_id != link.effect_id
                or request.operation != "submit_order"
                or request.request_digest != link.request_digest
            ):
                raise ValueError("saved Core request contradicts pending link")
            request.__post_init__()
            _require_request_matches_intent(request, client_id)
        except (TypeError, ValueError) as exc:
            results.append(
                _inspection(
                    key[0], content_hash, key[1], link.effect_id, link.request_digest,
                    f"saved Core request conflicts with pending link ({type(exc).__name__})",
                    status=PaperAttemptInspectionStatus.CONFLICT,
                )
            )
            continue
        evidence = core_port.lookup_effect(request)
        status = {
            CoreEffectLookupStatus.MATCHED: PaperAttemptInspectionStatus.CORE_RECEIPT_MATCHED,
            CoreEffectLookupStatus.ABSENT_AT_READ: PaperAttemptInspectionStatus.ABSENT_AT_READ,
            CoreEffectLookupStatus.CONFLICT: PaperAttemptInspectionStatus.CONFLICT,
            CoreEffectLookupStatus.UNAVAILABLE: PaperAttemptInspectionStatus.UNAVAILABLE,
        }[evidence.status]
        if (
            (
                "execution.failed" in terminal_types
                and status is PaperAttemptInspectionStatus.CORE_RECEIPT_MATCHED
            )
            or (
                "plan.accepted" in terminal_types
                and status is PaperAttemptInspectionStatus.ABSENT_AT_READ
            )
        ):
            status = PaperAttemptInspectionStatus.CONFLICT
            reason = "Core receipt evidence contradicts the terminal Paper outcome"
        else:
            reason = evidence.reason
        results.append(
            PaperAttemptInspection(
                status, key[0], content_hash, key[1], link.effect_id, link.request_digest,
                evidence.observed_commit_sequence, evidence.observed_event_sequence,
                reason,
            )
        )

    for key, extras in links.items():
        for link, _event in extras:
            results.append(
                _inspection(
                    link.plan_id, link.plan_content_hash, link.intent_id,
                    link.effect_id, link.request_digest,
                    "pending Core link has no matching saved Paper intent",
                    status=PaperAttemptInspectionStatus.BLOCKED_INVALID_LINK,
                )
            )
    return tuple(results)


def _find_saved_intent(
    events: tuple[EventEnvelope, ...], *, plan_id: str, content_hash: str, intent_id: str
) -> tuple[EventEnvelope, str]:
    matches: list[tuple[EventEnvelope, str]] = []
    for event in events:
        if event.event_type != "execution.intent_recorded":
            continue
        payload = event.payload
        if (
            payload.get("plan_id") != plan_id
            or payload.get("content_hash") != content_hash
            or payload.get("aggregate_id") != plan_id
            or event.correlation_id != plan_id
        ):
            continue
        rows = payload.get("intents")
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if isinstance(row, Mapping) and row.get("intent_id") == intent_id:
                matches.append((event, _text_value(row.get("client_order_id"), "client_order_id")))
    if len(matches) != 1:
        raise ValueError("exactly one saved Paper intent must match the link")
    return matches[0]


def _require_request_matches_intent(request: CoreLifecycleEffect, client_order_id: str) -> None:
    payload = json.loads(request.canonical_payload)
    if (
        not isinstance(payload, dict)
        or payload.get("operation") != "submit_order"
        or not isinstance(payload.get("request"), dict)
        or payload["request"].get("client_order_id") != client_order_id
        or request.target_ids != (client_order_id,)
    ):
        raise ValueError("complete Core request does not match saved Paper client target")


def _link_from_event(event: EventEnvelope) -> PaperPendingAttemptLink:
    payload = event.payload
    return PaperPendingAttemptLink(
        _text_value(payload.get("plan_id"), "plan_id"),
        _digest_value(payload.get("plan_content_hash"), "plan_content_hash"),
        _text_value(payload.get("intent_id"), "intent_id"),
        _text_value(payload.get("effect_id"), "effect_id"),
        _digest_value(payload.get("request_digest"), "request_digest"),
    )


def _inspection(
    plan_id: str | None,
    content_hash: str | None,
    intent_id: str | None,
    effect_id: str | None,
    request_digest: str | None,
    reason: str,
    *,
    status: PaperAttemptInspectionStatus = PaperAttemptInspectionStatus.BLOCKED_INVALID_LINK,
) -> PaperAttemptInspection:
    return PaperAttemptInspection(
        status, plan_id, content_hash, intent_id, effect_id, request_digest,
        None, None, reason[:200],
    )


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-blank text without surrounding whitespace")


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _text_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    _text(value, name)
    return value


def _digest_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    _digest(value, name)
    return value
