"""Opt-in durable Runner bar caller for the Core SQLite transaction boundary.

The default PaperBarExecutor is unchanged. This caller requires an external
trusted mapping from the exact saved market event to an already persisted Core
effect ID. It neither creates that source nor qualifies a PAPER composition.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from modular_core import StateRepository
from modular_core.lifecycle_venue import CorePaperContextUnavailable, SQLiteCorePaperVenue

from .core_bridge import ConservativePaperBarReconciliation
from .cycle import PaperBarExecutor, PaperBarResult


class DurableCoreBarEffectLookup(Protocol):
    """Trusted persisted event-to-effect mapping; lookup must never create/rebase."""

    def lookup_bar_effect_id(self, *, event_id: str, payload_hash: str, index: int) -> str: ...


class CoreLedgerPaperBarExecutor(PaperBarExecutor):
    """Bind the saved Runner event to one Core effect and resolve by its receipt.

    Pending state freezes both effect ID and full request digest before Core is
    called. Recovery replays that exact context, including its original expected
    generation and authorities, rather than inferring success from changed venue
    evidence. Every failure retains the pending reference for reconciliation.
    """

    def __init__(
        self,
        *,
        run_id: str,
        config_hash: str,
        state_repository: StateRepository,
        venue: SQLiteCorePaperVenue,
        effect_lookup: DurableCoreBarEffectLookup,
    ) -> None:
        if type(venue) is not SQLiteCorePaperVenue:
            raise TypeError("venue must be the canonical SQLite Core venue bridge")
        if not callable(getattr(effect_lookup, "lookup_bar_effect_id", None)):
            raise TypeError("effect_lookup must provide a trusted durable event-to-effect lookup")
        self._ledger_venue = venue
        self._effect_lookup = effect_lookup
        super().__init__(
            run_id=run_id,
            config_hash=config_hash,
            state_repository=state_repository,
            venue=venue,
            evidence=venue,
            reconciliation=ConservativePaperBarReconciliation(),
        )
        if state_repository.load(f"trading-runner-paper-bars:{self.run_id}") is not None:
            raise CorePaperContextUnavailable(
                "legacy Paper bar state requires an explicit owner migration before ledger binding"
            )
        if self._pending is not None:
            self._pending_capability()

    @property
    def _state_key(self) -> str:
        # Never reinterpret a legacy pending bar that lacks a saved Core context.
        return f"trading-runner-core-ledger-paper-bars:{self.run_id}"

    def _replace_state(
        self,
        *,
        processed: Mapping[str, str],
        pending: Mapping[str, Any] | None,
    ) -> None:
        if pending is not None:
            if "core_effect_id" in pending or "core_request_digest" in pending:
                raise CorePaperContextUnavailable("new bar state cannot replace saved Core context")
            event = self._decode_event(pending["event"])
            index = pending["index"]
            effect_id = self._effect_lookup.lookup_bar_effect_id(
                event_id=event.event_id,
                payload_hash=event.payload_hash,
                index=index,
            )
            capability = self._ledger_venue.for_effect(effect_id)
            digest = capability.validate_invocation("process_bar", bar=event.candle, index=index)
            pending = {**pending, "core_effect_id": effect_id, "core_request_digest": digest}
        super()._replace_state(processed=processed, pending=pending)

    def _pending_capability(self) -> SQLiteCorePaperVenue:
        if self._pending is None:
            raise CorePaperContextUnavailable("pending bar Core context is absent")
        effect_id = self._pending.get("core_effect_id")
        digest = self._pending.get("core_request_digest")
        if not isinstance(effect_id, str) or not isinstance(digest, str):
            raise CorePaperContextUnavailable(
                "pending bar lacks an immutable Core effect reference"
            )
        capability = self._ledger_venue.for_effect(effect_id)
        actual = capability.validate_invocation(
            "process_bar",
            bar=self._pending_event().candle,
            index=self._pending_index(),
        )
        if actual != digest:
            raise CorePaperContextUnavailable("saved bar Core effect context changed")
        return capability

    def _recover_pending(self) -> PaperBarResult:
        # Only Core's canonical idempotency/receipt resolves this exact request.
        return self._apply_pending(recovered=True)

    def _apply_pending(self, *, recovered: bool) -> PaperBarResult:
        event = self._pending_event()
        before = self._pending_before()
        capability = self._pending_capability()
        capability.process_bar(event.candle, self._pending_index())
        if capability.committed_receipt is None:
            raise CorePaperContextUnavailable("Core bar effect has no committed receipt")
        after = self._evidence(event)
        self._commit_processed(event)
        return PaperBarResult(
            event_id=event.event_id,
            content_hash=event.payload_hash,
            duplicate=False,
            recovered=recovered,
            before_evidence=before,
            after_evidence=after,
        )
