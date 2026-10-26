from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import canonical_sha256
from .errors import FactualEventRecoveryRequired
from .routing import FactualEventEnvelope, _digest, _required_text
from .routing import FactualEventRouter as LegacyFactualEventRouter

_STATE_SCHEMA = "2"
_LEGACY_STATE_SCHEMA = "1"
_MAX_PROCESSED_EVENTS = 1_000_000


def _recovery_error(message: str) -> FactualEventRecoveryRequired:
    return FactualEventRecoveryRequired(
        message,
        reason_code="factual_routing_state_invalid",
    )


def _optional_mapping(value: object, name: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _recovery_error(f"{name} must be a mapping or null")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _recovery_error(f"{name} must be a non-negative int")
    return value


class FactualEventRouter(LegacyFactualEventRouter):
    """Journal-indexed factual routing with bounded checkpoint state.

    Schema v2 keeps the append-only runtime journal as the authoritative
    processed-identity ledger. The mutable checkpoint stores only the durable
    count and the at-most-one pending transaction, avoiding a complete
    processed-map serialization on each routing transition.
    """

    def _state_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": _STATE_SCHEMA,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "module_graph_hash": self.module_graph_hash,
            "processed_count": len(self._processed),
            "pending_processing": self._pending_processing,
            "pending_terminal": self._pending_terminal,
        }
        payload["integrity_hash"] = canonical_sha256(payload)
        return payload

    def _load_state(self) -> None:
        snapshot = self.state_repository.load(self._state_key)
        journal_processed, unresolved_started = self._journal_identities()
        if snapshot is None:
            if unresolved_started:
                raise _recovery_error(
                    "runtime journal contains unfinished factual processing without checkpoint state"
                )
            self._processed = journal_processed
            return

        payload = dict(snapshot)
        try:
            stored_integrity = _digest(payload.pop("integrity_hash", None), "integrity_hash")
        except ValueError as exc:
            raise FactualEventRecoveryRequired(
                "factual routing state integrity digest is invalid",
                reason_code="factual_routing_state_corrupt",
            ) from exc
        if canonical_sha256(payload) != stored_integrity:
            raise FactualEventRecoveryRequired(
                "factual routing state integrity check failed",
                reason_code="factual_routing_state_corrupt",
            )
        if (
            payload.get("run_id") != self.run_id
            or payload.get("config_hash") != self.config_hash
            or payload.get("module_graph_hash") != self.module_graph_hash
        ):
            raise FactualEventRecoveryRequired(
                "factual routing state identity is incompatible",
                reason_code="factual_routing_state_incompatible",
            )

        self._pending_processing = _optional_mapping(
            payload.get("pending_processing"),
            "pending_processing",
        )
        self._pending_terminal = _optional_mapping(
            payload.get("pending_terminal"),
            "pending_terminal",
        )
        active_processed = dict(journal_processed)
        terminal_event = self._pending_terminal_event()
        if terminal_event is not None:
            journal_hash = active_processed.get(terminal_event.event_id)
            if journal_hash is not None:
                if journal_hash != terminal_event.content_hash:
                    raise _recovery_error(
                        "pending terminal identity conflicts with runtime journal"
                    )
                active_processed.pop(terminal_event.event_id)

        schema = payload.get("schema_version")
        if schema == _LEGACY_STATE_SCHEMA:
            legacy = self._legacy_processed(payload.get("processed", {}))
            if legacy != active_processed:
                raise _recovery_error("legacy processed identities do not match runtime journal")
        elif schema == _STATE_SCHEMA:
            if "processed" in payload:
                raise _recovery_error("schema v2 cannot contain a processed mapping")
            processed_count = _non_negative_int(
                payload.get("processed_count"),
                "processed_count",
            )
            if processed_count != len(active_processed):
                raise _recovery_error("processed_count does not match runtime journal")
        else:
            raise FactualEventRecoveryRequired(
                "factual routing state identity is incompatible",
                reason_code="factual_routing_state_incompatible",
            )

        if len(active_processed) > _MAX_PROCESSED_EVENTS:
            raise _recovery_error("processed factual identity limit exceeded")
        self._processed = active_processed

    def _legacy_processed(self, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise _recovery_error("processed factual identities must be a mapping")
        if len(value) > _MAX_PROCESSED_EVENTS:
            raise _recovery_error("processed factual identity limit exceeded")
        try:
            return {
                _required_text(event_id, "processed event_id"): _digest(
                    content_hash,
                    "processed content_hash",
                )
                for event_id, content_hash in value.items()
            }
        except ValueError as exc:
            raise _recovery_error("processed factual identity is invalid") from exc

    def _pending_terminal_event(self) -> FactualEventEnvelope | None:
        if self._pending_terminal is None:
            return None
        event_raw = self._pending_terminal.get("event")
        if not isinstance(event_raw, Mapping):
            raise _recovery_error("pending terminal event is invalid")
        try:
            return FactualEventEnvelope.from_mapping(event_raw)
        except (TypeError, ValueError) as exc:
            raise _recovery_error("pending terminal event is invalid") from exc

    def _journal_identities(self) -> tuple[dict[str, str], set[str]]:
        processed: dict[str, str] = {}
        started: dict[str, str] = {}
        try:
            for record in self.journal.read():
                if record.event_type not in {
                    "factual_event.processing_started",
                    "factual_event.processed",
                }:
                    continue
                event_id = _required_text(
                    record.payload.get("event_id"),
                    "journal event_id",
                )
                content_hash = _digest(
                    record.payload.get("content_hash"),
                    "journal content_hash",
                )
                if record.event_type == "factual_event.processing_started":
                    if event_id in processed:
                        raise _recovery_error(
                            "runtime journal contains a factual start after terminal"
                        )
                    existing_hash = started.get(event_id)
                    if existing_hash is not None and existing_hash != content_hash:
                        raise _recovery_error(
                            "runtime journal contains conflicting factual processing starts"
                        )
                    started[event_id] = content_hash
                    continue
                started_hash = started.pop(event_id, None)
                if started_hash != content_hash:
                    raise _recovery_error("runtime journal factual terminal has no matching start")
                if event_id in processed:
                    raise _recovery_error("runtime journal contains duplicate factual terminal")
                processed[event_id] = content_hash
        except ValueError as exc:
            raise _recovery_error("runtime journal factual identity is invalid") from exc
        if len(processed) > _MAX_PROCESSED_EVENTS:
            raise _recovery_error("processed factual identity limit exceeded")
        return processed, set(started)
