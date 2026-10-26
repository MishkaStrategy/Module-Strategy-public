from __future__ import annotations

import inspect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from modular_core import Candle, StateRepository

from .canonical import canonical_sha256, require_json_mapping
from .errors import (
    MarketCycleRecoveryRequired,
    PaperBarIdentityConflict,
    PaperBarRecoveryRequired,
)
from .journal import RuntimeEventJournal
from .market_data import MarketEventEnvelope, MarketEventStatus, MarketEventValidator
from .pipeline import CoreMarketPipeline
from .ports import RuntimeClock
from .routing import FactualEventEnvelope, FactualEventRouter

_STATE_SCHEMA = "1"
_MAX_PROCESSED_BARS = 1_000_000


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 256:
        raise ValueError(f"{name} exceeds maximum length")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class PaperBarRecoveryDecision(StrEnum):
    APPLIED = "applied"
    RETRY = "retry"
    BLOCKED = "blocked"


class PaperVenueBoundary(Protocol):
    def process_bar(self, bar: object, index: int) -> None: ...


class PaperVenueEvidenceBoundary(Protocol):
    def snapshot_evidence(self, event: MarketEventEnvelope) -> Mapping[str, Any]: ...


class PaperBarReconciliationBoundary(Protocol):
    def resolve(
        self,
        *,
        event: MarketEventEnvelope,
        before: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> PaperBarRecoveryDecision: ...


@dataclass(frozen=True, slots=True)
class PaperBarResult:
    event_id: str
    content_hash: str
    duplicate: bool
    recovered: bool
    before_evidence: Mapping[str, Any]
    after_evidence: Mapping[str, Any]


class PaperBarExecutor:
    """Durable wrapper around the non-idempotent Paper venue bar boundary."""

    def __init__(
        self,
        *,
        run_id: str,
        config_hash: str,
        state_repository: StateRepository,
        venue: PaperVenueBoundary,
        evidence: PaperVenueEvidenceBoundary,
        reconciliation: PaperBarReconciliationBoundary,
    ) -> None:
        self.run_id = _text(run_id, "run_id")
        self.config_hash = _digest(config_hash, "config_hash")
        if not callable(getattr(venue, "process_bar", None)):
            raise TypeError("venue must implement process_bar()")
        if not callable(getattr(evidence, "snapshot_evidence", None)):
            raise TypeError("evidence must implement snapshot_evidence()")
        if not callable(getattr(reconciliation, "resolve", None)):
            raise TypeError("reconciliation must implement resolve()")
        self.state_repository = state_repository
        self.venue = venue
        self.evidence = evidence
        self.reconciliation = reconciliation
        self._processed: dict[str, str] = {}
        self._pending: Mapping[str, Any] | None = None
        self._load_state()

    @property
    def recovery_required(self) -> bool:
        return self._pending is not None

    @property
    def _state_key(self) -> str:
        return f"trading-runner-paper-bars:{self.run_id}"

    def execute(self, event: MarketEventEnvelope, *, index: int) -> PaperBarResult:
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        index = _required_int(index, "index")
        existing = self._processed.get(event.event_id)
        if existing is not None:
            if existing != event.payload_hash:
                raise PaperBarIdentityConflict(
                    "market event_id was already applied with different content",
                    reason_code="paper_bar_identity_conflict",
                )
            evidence = self._evidence(event)
            return PaperBarResult(
                event_id=event.event_id,
                content_hash=event.payload_hash,
                duplicate=True,
                recovered=False,
                before_evidence=evidence,
                after_evidence=evidence,
            )
        if self._pending is not None:
            pending_event = self._pending_event()
            if pending_event.event_id != event.event_id:
                raise PaperBarRecoveryRequired(
                    "unfinished Paper bar must be recovered before a new event",
                    reason_code="paper_bar_recovery_required",
                )
            if pending_event.payload_hash != event.payload_hash:
                raise PaperBarIdentityConflict(
                    "pending Paper bar event_id has different content",
                    reason_code="paper_bar_identity_conflict",
                )
            return self._recover_pending()
        if len(self._processed) >= _MAX_PROCESSED_BARS:
            raise PaperBarRecoveryRequired(
                "processed Paper bar identity limit exceeded",
                reason_code="paper_bar_identity_limit",
            )
        before = self._evidence(event)
        pending = {
            "event": self._encode_event(event),
            "index": index,
            "before_evidence": before,
        }
        self._replace_state(processed=self._processed, pending=pending)
        return self._apply_pending(recovered=False)

    def recover(self) -> PaperBarResult | None:
        if self._pending is None:
            return None
        return self._recover_pending()

    def _recover_pending(self) -> PaperBarResult:
        event = self._pending_event()
        before = self._pending_before()
        current = self._evidence(event)
        if current == before:
            return self._apply_pending(recovered=True)
        decision = self.reconciliation.resolve(
            event=event,
            before=before,
            current=current,
        )
        if not isinstance(decision, PaperBarRecoveryDecision):
            raise PaperBarRecoveryRequired(
                "Paper bar reconciliation returned an invalid decision",
                reason_code="paper_bar_reconciliation_invalid",
            )
        if decision is PaperBarRecoveryDecision.BLOCKED:
            raise PaperBarRecoveryRequired(
                "Paper bar reconciliation is blocking",
                reason_code="paper_bar_reconciliation_blocked",
            )
        if decision is PaperBarRecoveryDecision.APPLIED:
            self._commit_processed(event)
            return PaperBarResult(
                event_id=event.event_id,
                content_hash=event.payload_hash,
                duplicate=False,
                recovered=True,
                before_evidence=before,
                after_evidence=current,
            )
        retry_pending = {
            "event": self._encode_event(event),
            "index": self._pending_index(),
            "before_evidence": current,
        }
        self._replace_state(processed=self._processed, pending=retry_pending)
        return self._apply_pending(recovered=True)

    def _apply_pending(self, *, recovered: bool) -> PaperBarResult:
        event = self._pending_event()
        index = self._pending_index()
        before = self._pending_before()
        try:
            self.venue.process_bar(event.candle, index)
        except Exception:
            current = self._evidence(event)
            if current == before:
                self._replace_state(processed=self._processed, pending=None)
            raise
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

    def _commit_processed(self, event: MarketEventEnvelope) -> None:
        processed = dict(self._processed)
        processed[event.event_id] = event.payload_hash
        self._replace_state(processed=processed, pending=None)

    def _replace_state(
        self,
        *,
        processed: Mapping[str, str],
        pending: Mapping[str, Any] | None,
    ) -> None:
        original_processed = self._processed
        original_pending = self._pending
        self._processed = dict(processed)
        self._pending = pending
        try:
            self._save_state()
        except Exception:
            self._processed = original_processed
            self._pending = original_pending
            raise

    def _pending_event(self) -> MarketEventEnvelope:
        if self._pending is None:
            raise PaperBarRecoveryRequired(
                "Paper bar pending state is missing",
                reason_code="paper_bar_state_invalid",
            )
        event_raw = self._pending.get("event")
        if not isinstance(event_raw, Mapping):
            raise PaperBarRecoveryRequired(
                "Paper bar pending event is invalid",
                reason_code="paper_bar_state_invalid",
            )
        try:
            return self._decode_event(event_raw)
        except Exception as exc:
            raise PaperBarRecoveryRequired(
                "Paper bar pending event cannot be decoded",
                reason_code="paper_bar_state_invalid",
            ) from exc

    def _pending_index(self) -> int:
        if self._pending is None:
            raise PaperBarRecoveryRequired(
                "Paper bar pending state is missing",
                reason_code="paper_bar_state_invalid",
            )
        return _required_int(self._pending.get("index"), "pending index")

    def _pending_before(self) -> Mapping[str, Any]:
        if self._pending is None:
            raise PaperBarRecoveryRequired(
                "Paper bar pending state is missing",
                reason_code="paper_bar_state_invalid",
            )
        return require_json_mapping(
            self._pending.get("before_evidence"),
            "Paper before evidence",
        )

    def _evidence(self, event: MarketEventEnvelope) -> Mapping[str, Any]:
        return require_json_mapping(
            self.evidence.snapshot_evidence(event),
            "Paper venue evidence",
        )

    def _state_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": _STATE_SCHEMA,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "processed": dict(sorted(self._processed.items())),
            "pending": self._pending,
        }
        payload["integrity_hash"] = canonical_sha256(payload)
        return payload

    def _save_state(self) -> None:
        self.state_repository.save(self._state_key, self._state_payload())

    def _load_state(self) -> None:
        state = self.state_repository.load(self._state_key)
        if state is None:
            return
        payload = dict(state)
        stored_hash = _digest(payload.pop("integrity_hash", None), "integrity_hash")
        if canonical_sha256(payload) != stored_hash:
            raise PaperBarRecoveryRequired(
                "Paper bar state integrity check failed",
                reason_code="paper_bar_state_corrupt",
            )
        if (
            payload.get("schema_version") != _STATE_SCHEMA
            or payload.get("run_id") != self.run_id
            or payload.get("config_hash") != self.config_hash
        ):
            raise PaperBarRecoveryRequired(
                "Paper bar state identity is incompatible",
                reason_code="paper_bar_state_incompatible",
            )
        processed = payload.get("processed", {})
        if not isinstance(processed, Mapping) or len(processed) > _MAX_PROCESSED_BARS:
            raise PaperBarRecoveryRequired(
                "Paper bar processed identities are invalid",
                reason_code="paper_bar_state_invalid",
            )
        self._processed = {
            _text(event_id, "processed event_id"): _digest(
                content_hash,
                "processed content_hash",
            )
            for event_id, content_hash in processed.items()
        }
        pending = payload.get("pending")
        if pending is not None and not isinstance(pending, Mapping):
            raise PaperBarRecoveryRequired(
                "Paper bar pending state must be a mapping or null",
                reason_code="paper_bar_state_invalid",
            )
        self._pending = pending

    @staticmethod
    def _encode_event(event: MarketEventEnvelope) -> Mapping[str, Any]:
        return {
            "event_id": event.event_id,
            "source_id": event.source_id,
            "market": event.market,
            "sequence": event.sequence,
            "available_at": event.available_at.isoformat(),
            "received_at": event.received_at.isoformat(),
            "is_closed": event.is_closed,
            "metadata_version": event.metadata_version,
            "payload_hash": event.payload_hash,
            "candle": {
                "timestamp": event.candle.timestamp.isoformat(),
                "open": event.candle.open,
                "high": event.candle.high,
                "low": event.candle.low,
                "close": event.candle.close,
                "volume": event.candle.volume,
                "symbol": event.candle.symbol,
                "timeframe": event.candle.timeframe,
            },
        }

    @staticmethod
    def _decode_event(value: Mapping[str, Any]) -> MarketEventEnvelope:
        candle_raw = value.get("candle")
        if not isinstance(candle_raw, Mapping):
            raise ValueError("Paper bar candle mapping is required")
        event = MarketEventEnvelope(
            event_id=_text(value.get("event_id"), "event_id"),
            source_id=_text(value.get("source_id"), "source_id"),
            market=_text(value.get("market"), "market"),
            sequence=_required_int(value.get("sequence"), "sequence"),
            available_at=datetime.fromisoformat(_text(value.get("available_at"), "available_at")),
            received_at=datetime.fromisoformat(_text(value.get("received_at"), "received_at")),
            candle=Candle(
                timestamp=datetime.fromisoformat(
                    _text(candle_raw.get("timestamp"), "candle timestamp")
                ),
                open=_finite_float(candle_raw.get("open"), "candle.open"),
                high=_finite_float(candle_raw.get("high"), "candle.high"),
                low=_finite_float(candle_raw.get("low"), "candle.low"),
                close=_finite_float(candle_raw.get("close"), "candle.close"),
                volume=_finite_float(candle_raw.get("volume"), "candle.volume"),
                symbol=_text(candle_raw.get("symbol"), "candle symbol"),
                timeframe=_text(candle_raw.get("timeframe"), "candle timeframe"),
            ),
            is_closed=_required_bool(value.get("is_closed"), "is_closed"),
            metadata_version=_text(value.get("metadata_version"), "metadata_version"),
        )
        if _digest(value.get("payload_hash"), "payload_hash") != event.payload_hash:
            raise ValueError("Paper bar event payload hash mismatch")
        return event


class MarketCycleStage(StrEnum):
    RECEIVED = "received"
    PAPER_APPLIED = "paper_applied"
    FACTUAL_APPLIED = "factual_applied"
    PIPELINE_APPLIED = "pipeline_applied"
    PLAN_PREPARED = "plan_prepared"
    PLAN_DISPATCHED = "plan_dispatched"
    PLAN_FACTUAL_APPLIED = "plan_factual_applied"
    COMMITTED = "committed"


_NEXT_MARKET_CYCLE_STAGE = {
    MarketCycleStage.RECEIVED: MarketCycleStage.PAPER_APPLIED,
    MarketCycleStage.PAPER_APPLIED: MarketCycleStage.FACTUAL_APPLIED,
    MarketCycleStage.FACTUAL_APPLIED: MarketCycleStage.PIPELINE_APPLIED,
    MarketCycleStage.PIPELINE_APPLIED: MarketCycleStage.PLAN_PREPARED,
    MarketCycleStage.PLAN_PREPARED: MarketCycleStage.PLAN_DISPATCHED,
    MarketCycleStage.PLAN_DISPATCHED: MarketCycleStage.PLAN_FACTUAL_APPLIED,
    MarketCycleStage.PLAN_FACTUAL_APPLIED: MarketCycleStage.COMMITTED,
}
_PLAN_EVIDENCE_STAGES = frozenset(
    {
        MarketCycleStage.PLAN_PREPARED,
        MarketCycleStage.PLAN_DISPATCHED,
        MarketCycleStage.PLAN_FACTUAL_APPLIED,
        MarketCycleStage.COMMITTED,
    }
)


def _plan_identity(plan: object | None) -> tuple[str | None, str | None, bool]:
    if plan is None:
        return None, None, True
    return (
        _text(getattr(plan, "plan_id", None), "plan_id"),
        _digest(getattr(plan, "content_hash", None), "plan content_hash"),
        False,
    )


class MarketCycleMetadata:
    """Checkpoint-owned stage metadata; no venue, order or portfolio state is copied."""

    def __init__(self) -> None:
        self.event: Mapping[str, Any] | None = None
        self.stage: MarketCycleStage | None = None
        self.data_gap: bool | None = None
        self.plan_id: str | None = None
        self.plan_hash: str | None = None
        self.no_plan = False

    def begin(self, event: MarketEventEnvelope, *, data_gap: bool = False) -> None:
        if self.event is not None:
            raise MarketCycleRecoveryRequired(
                "a market cycle is already active",
                reason_code="market_cycle_already_active",
            )
        if not isinstance(data_gap, bool):
            raise TypeError("data_gap must be bool")
        self.event = PaperBarExecutor._encode_event(event)
        self.stage = MarketCycleStage.RECEIVED
        self.data_gap = data_gap
        self.plan_id = None
        self.plan_hash = None
        self.no_plan = False

    def transition(
        self,
        expected: MarketCycleStage,
        target: MarketCycleStage,
        *,
        plan: object | None = None,
    ) -> None:
        if self.stage is not expected or _NEXT_MARKET_CYCLE_STAGE.get(expected) is not target:
            raise MarketCycleRecoveryRequired(
                f"invalid market-cycle transition: {self.stage!r} → {target.value}",
                reason_code="market_cycle_transition_invalid",
            )
        plan_id = self.plan_id
        plan_hash = self.plan_hash
        no_plan = self.no_plan
        if target is MarketCycleStage.PLAN_PREPARED:
            plan_id, plan_hash, no_plan = _plan_identity(plan)
        elif plan is not None:
            raise MarketCycleRecoveryRequired(
                "plan evidence is accepted only while preparing dispatch",
                reason_code="market_cycle_plan_evidence_invalid",
            )
        self.stage = target
        self.plan_id = plan_id
        self.plan_hash = plan_hash
        self.no_plan = no_plan

    def validate_prepared_plan(self, plan: object | None) -> None:
        if self.stage is not MarketCycleStage.PLAN_PREPARED:
            raise MarketCycleRecoveryRequired(
                "plan validation requires PLAN_PREPARED stage",
                reason_code="market_cycle_transition_invalid",
            )
        identity = _plan_identity(plan)
        expected = (self.plan_id, self.plan_hash, self.no_plan)
        if identity != expected:
            raise MarketCycleRecoveryRequired(
                "recomposed plan identity differs from prepared evidence",
                reason_code="market_cycle_plan_identity_changed",
            )

    def complete(self) -> None:
        if self.stage is not MarketCycleStage.COMMITTED:
            raise MarketCycleRecoveryRequired(
                "market cycle can complete only after commit",
                reason_code="market_cycle_transition_invalid",
            )
        self.event = None
        self.stage = None
        self.data_gap = None
        self.plan_id = None
        self.plan_hash = None
        self.no_plan = False

    def current_event(self) -> MarketEventEnvelope | None:
        if self.event is None:
            return None
        return PaperBarExecutor._decode_event(self.event)

    def snapshot_runtime_metadata(self) -> Mapping[str, Any]:
        return {
            "market_cycle": {
                "event": self.event,
                "stage": self.stage.value if self.stage is not None else None,
                "data_gap": self.data_gap,
                "plan_id": self.plan_id,
                "plan_hash": self.plan_hash,
                "no_plan": self.no_plan,
            }
        }

    def restore_runtime_metadata(self, state: Mapping[str, Any]) -> None:
        cycle = state.get("market_cycle", {})
        if not isinstance(cycle, Mapping):
            raise ValueError("market_cycle metadata must be a mapping")
        event = cycle.get("event")
        stage_raw = cycle.get("stage")
        data_gap = cycle.get("data_gap")
        plan_id = cycle.get("plan_id")
        plan_hash = cycle.get("plan_hash")
        no_plan = cycle.get("no_plan", False)
        if plan_id is not None:
            plan_id = _text(plan_id, "plan_id")
        if plan_hash is not None:
            plan_hash = _digest(plan_hash, "plan_hash")
        if not isinstance(no_plan, bool):
            raise ValueError("no_plan must be bool")
        if event is None:
            if (
                stage_raw is not None
                or data_gap is not None
                or plan_id is not None
                or plan_hash is not None
                or no_plan
            ):
                raise ValueError("empty market cycle contains active evidence")
            self.event = None
            self.stage = None
            self.data_gap = None
            self.plan_id = None
            self.plan_hash = None
            self.no_plan = False
            return
        if not isinstance(event, Mapping) or not isinstance(stage_raw, str):
            raise ValueError("active market cycle requires event and stage")
        if not isinstance(data_gap, bool):
            raise ValueError("active market cycle requires boolean data_gap")
        decoded = PaperBarExecutor._decode_event(event)
        stage = MarketCycleStage(stage_raw)
        if stage in _PLAN_EVIDENCE_STAGES:
            if no_plan:
                if plan_id is not None or plan_hash is not None:
                    raise ValueError("no-plan cycle cannot contain plan identity")
            elif plan_id is None or plan_hash is None:
                raise ValueError("prepared plan stage requires complete plan identity")
        elif plan_id is not None or plan_hash is not None or no_plan:
            raise ValueError("plan evidence is invalid before plan preparation")
        self.event = PaperBarExecutor._encode_event(decoded)
        self.stage = stage
        self.data_gap = data_gap
        self.plan_id = plan_id
        self.plan_hash = plan_hash
        self.no_plan = no_plan


class FactualEventSource(Protocol):
    def events_for(self, event: MarketEventEnvelope) -> Sequence[FactualEventEnvelope]: ...


class PlanComposerBoundary(Protocol):
    def compose(self, event: MarketEventEnvelope) -> object | None: ...


class PlanDispatcherBoundary(Protocol):
    def dispatch(self, plan: object, event: MarketEventEnvelope) -> object: ...


class MarketIndexBoundary(Protocol):
    def index_for(self, event: MarketEventEnvelope) -> int: ...


class CheckpointBoundary(Protocol):
    def capture(self) -> object: ...


@dataclass(frozen=True, slots=True)
class MarketCycleResult:
    event_id: str
    duplicate: bool
    plan_id: str | None
    plan_hash: str | None
    no_plan: bool


class MarketCycleCoordinator:
    """Final paper-only market cycle with checkpointed stage transitions."""

    def __init__(
        self,
        *,
        metadata: MarketCycleMetadata,
        validator: MarketEventValidator,
        paper: PaperBarExecutor,
        factual_source: FactualEventSource,
        factual_router: FactualEventRouter,
        pipeline: CoreMarketPipeline,
        composer: PlanComposerBoundary,
        dispatcher: PlanDispatcherBoundary,
        index: MarketIndexBoundary,
        checkpoint: CheckpointBoundary,
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
    ) -> None:
        self.metadata = metadata
        self.validator = validator
        self.paper = paper
        self.factual_source = factual_source
        self.factual_router = factual_router
        self.pipeline = pipeline
        self.composer = composer
        self.dispatcher = dispatcher
        self.index = index
        self.checkpoint = checkpoint
        self.journal = journal
        self.clock = clock
        self._checkpoint_pending = False

    async def process(self, event: MarketEventEnvelope) -> MarketCycleResult:
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        if self._checkpoint_pending:
            self._capture()
        current = self.metadata.current_event()
        if current is None:
            validation = self.validator.inspect(event, now=self.clock.now())
            if validation.status is MarketEventStatus.DUPLICATE:
                return self._completed_result(event)
            self.metadata.begin(event, data_gap=validation.data_gap)
            self._capture()
        elif current.event_id != event.event_id or current.payload_hash != event.payload_hash:
            raise MarketCycleRecoveryRequired(
                "unfinished market cycle must be recovered before a new event",
                reason_code="market_cycle_recovery_required",
            )
        return await self._resume(event)

    async def recover(self) -> MarketCycleResult | None:
        event = self.metadata.current_event()
        if event is None:
            return None
        if self._checkpoint_pending:
            self._capture()
        return await self._resume(event)

    async def _resume(self, event: MarketEventEnvelope) -> MarketCycleResult:
        stage = self.metadata.stage
        data_gap = self.metadata.data_gap
        if stage is None or not isinstance(data_gap, bool):
            raise MarketCycleRecoveryRequired(
                "active market cycle has incomplete metadata",
                reason_code="market_cycle_state_invalid",
            )
        if stage is MarketCycleStage.RECEIVED:
            self.paper.execute(event, index=self.index.index_for(event))
            self.metadata.transition(
                MarketCycleStage.RECEIVED,
                MarketCycleStage.PAPER_APPLIED,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.PAPER_APPLIED:
            self._route_factual_events(event)
            self.metadata.transition(
                MarketCycleStage.PAPER_APPLIED,
                MarketCycleStage.FACTUAL_APPLIED,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.FACTUAL_APPLIED:
            self.pipeline.process(event, data_gap=data_gap)
            self.metadata.transition(
                MarketCycleStage.FACTUAL_APPLIED,
                MarketCycleStage.PIPELINE_APPLIED,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.PIPELINE_APPLIED:
            plan = self.composer.compose(event)
            self.metadata.transition(
                MarketCycleStage.PIPELINE_APPLIED,
                MarketCycleStage.PLAN_PREPARED,
                plan=plan,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.PLAN_PREPARED:
            plan = self.composer.compose(event)
            self.metadata.validate_prepared_plan(plan)
            if plan is not None:
                await _await_if_needed(self.dispatcher.dispatch(plan, event))
            self.metadata.transition(
                MarketCycleStage.PLAN_PREPARED,
                MarketCycleStage.PLAN_DISPATCHED,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.PLAN_DISPATCHED:
            self._route_factual_events(event)
            self.metadata.transition(
                MarketCycleStage.PLAN_DISPATCHED,
                MarketCycleStage.PLAN_FACTUAL_APPLIED,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.PLAN_FACTUAL_APPLIED:
            self.validator.commit(event)
            self.metadata.transition(
                MarketCycleStage.PLAN_FACTUAL_APPLIED,
                MarketCycleStage.COMMITTED,
            )
            self._capture()
            stage = self.metadata.stage
        if stage is MarketCycleStage.COMMITTED:
            plan_id = self.metadata.plan_id
            plan_hash = self.metadata.plan_hash
            no_plan = self.metadata.no_plan
            if not self._completion_exists(event):
                self.journal.append(
                    timestamp=self.clock.now(),
                    event_type="market_cycle.completed",
                    aggregate_id=event.event_id,
                    correlation_id=plan_id,
                    symbol=event.candle.symbol,
                    payload={
                        "event_id": event.event_id,
                        "content_hash": event.payload_hash,
                        "plan_id": plan_id,
                        "plan_hash": plan_hash,
                        "no_plan": no_plan,
                    },
                )
            self.metadata.complete()
            self._capture()
            return MarketCycleResult(
                event_id=event.event_id,
                duplicate=False,
                plan_id=plan_id,
                plan_hash=plan_hash,
                no_plan=no_plan,
            )
        raise MarketCycleRecoveryRequired(
            "market cycle stopped at an unsupported stage",
            reason_code="market_cycle_state_invalid",
        )

    def _route_factual_events(self, event: MarketEventEnvelope) -> None:
        raw_events = self.factual_source.events_for(event)
        if isinstance(raw_events, (str, bytes)) or not isinstance(raw_events, Sequence):
            raise TypeError("factual source must return a sequence")
        events = tuple(raw_events)
        if any(not isinstance(item, FactualEventEnvelope) for item in events):
            raise TypeError("factual source returned a non-factual event")
        for factual in events:
            self.factual_router.route(factual)
        commit = getattr(self.factual_source, "commit", None)
        if callable(commit):
            commit(events)

    def _capture(self) -> None:
        try:
            self.checkpoint.capture()
        except Exception as exc:
            self._checkpoint_pending = True
            raise MarketCycleRecoveryRequired(
                "market-cycle checkpoint capture failed",
                reason_code="market_cycle_checkpoint_failed",
            ) from exc
        self._checkpoint_pending = False

    def _completion_exists(self, event: MarketEventEnvelope) -> bool:
        return any(
            record.payload.get("event_id") == event.event_id
            and record.payload.get("content_hash") == event.payload_hash
            for record in self.journal.read("market_cycle.completed")
        )

    def _completed_result(self, event: MarketEventEnvelope) -> MarketCycleResult:
        matches = tuple(
            record
            for record in self.journal.read("market_cycle.completed")
            if record.payload.get("event_id") == event.event_id
            and record.payload.get("content_hash") == event.payload_hash
        )
        if not matches:
            raise MarketCycleRecoveryRequired(
                "validator duplicate has no completion evidence",
                reason_code="market_cycle_completion_missing",
            )
        identities = tuple(self._completion_identity(record.payload) for record in matches)
        if any(identity != identities[0] for identity in identities[1:]):
            raise MarketCycleRecoveryRequired(
                "completion evidence contains conflicting plan identities",
                reason_code="market_cycle_completion_conflict",
            )
        plan_id, plan_hash, no_plan = identities[0]
        return MarketCycleResult(
            event_id=event.event_id,
            duplicate=True,
            plan_id=plan_id,
            plan_hash=plan_hash,
            no_plan=no_plan,
        )

    @staticmethod
    def _completion_identity(
        payload: Mapping[str, Any],
    ) -> tuple[str | None, str | None, bool]:
        no_plan = _required_bool(payload.get("no_plan"), "completion no_plan")
        plan_id_raw = payload.get("plan_id")
        plan_hash_raw = payload.get("plan_hash")
        if no_plan:
            if plan_id_raw is not None or plan_hash_raw is not None:
                raise MarketCycleRecoveryRequired(
                    "no-plan completion contains plan identity",
                    reason_code="market_cycle_completion_invalid",
                )
            return None, None, True
        try:
            return (
                _text(plan_id_raw, "completion plan_id"),
                _digest(plan_hash_raw, "completion plan_hash"),
                False,
            )
        except ValueError as exc:
            raise MarketCycleRecoveryRequired(
                "completion evidence has invalid plan identity",
                reason_code="market_cycle_completion_invalid",
            ) from exc


async def _await_if_needed(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


class RunnerDispatchBoundary(Protocol):
    def dispatch_plan(
        self,
        plan: object,
        *,
        current_index: int,
        mark_price: Decimal,
        paper_versions: object,
    ) -> object: ...


class PaperVersionsBoundary(Protocol):
    def versions_for(self, event: MarketEventEnvelope) -> object: ...


class RunnerPlanDispatcher:
    """Thin adapter from final market cycle to the existing TradingRunner dispatch API."""

    def __init__(
        self,
        *,
        runner: RunnerDispatchBoundary,
        versions: PaperVersionsBoundary,
        index: MarketIndexBoundary,
    ) -> None:
        if not callable(getattr(runner, "dispatch_plan", None)):
            raise TypeError("runner must implement dispatch_plan()")
        if not callable(getattr(versions, "versions_for", None)):
            raise TypeError("versions must implement versions_for()")
        self.runner = runner
        self.versions = versions
        self.index = index

    def dispatch(self, plan: object, event: MarketEventEnvelope) -> object:
        return self.runner.dispatch_plan(
            plan,
            current_index=self.index.index_for(event),
            mark_price=Decimal(str(event.candle.close)),
            paper_versions=self.versions.versions_for(event),
        )
