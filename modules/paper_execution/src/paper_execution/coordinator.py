from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from modular_core import CancelRequest, OrderEvent
from modular_core.lifecycle import CoreCommitStatus
from modular_core.lifecycle_venue import (
    CorePaperCommittedProjectionUnavailable,
    CorePaperContextUnavailable,
    CorePaperEffectNotCommitted,
    CoreInvocationRef,
)

from .adapter import CorePaperVenue
from .config import PaperExecutionConfig
from .contracts import (
    ExecutableTradePlan,
    ExecutionIntent,
    ExecutionResult,
    NormalizedOrder,
    StateVersions,
    VenueMetadataSnapshot,
)
from .enums import IntentRole, PaperOrderState, PaperReasonCode
from .exceptions import (
    PaperExecutionError,
    PlanValidationError,
    ReconciliationBlocked,
    VenueRuleRejection,
)
from .ids import ClientOrderIdFactory
from .journal import ExecutionEventJournal
from .reconciliation import ReconciliationEngine, ReconciliationReport
from .snapshot_identity import build_snapshot_tag
from .state_machine import OrderAggregate
from .venue import VenueRuleEngine


class InstrumentMetadataProvider(Protocol):
    def snapshot(
        self,
        instrument: str,
        timestamp: datetime,
    ) -> VenueMetadataSnapshot: ...


@dataclass(frozen=True, slots=True)
class _PreparedIntent:
    intent: ExecutionIntent
    client_order_id: str | None = None
    normalized: NormalizedOrder | None = None


class PaperExecutionCoordinator:
    MODULE_ID = "paper_execution_venue_reconciliation"

    def __init__(
        self,
        *,
        config: PaperExecutionConfig,
        venue: CorePaperVenue,
        metadata_provider: InstrumentMetadataProvider,
        run_id: str,
    ) -> None:
        self.config = config
        self.venue = venue
        self.metadata_provider = metadata_provider
        self.journal = ExecutionEventJournal(run_id)
        self.rule_engine = VenueRuleEngine()
        self.id_factory = ClientOrderIdFactory(
            config.module_namespace,
            config.client_order_id_max_length,
        )
        self.reconciliation_engine = ReconciliationEngine()
        self._plan_results: dict[str, ExecutionResult] = {}
        self._plan_hashes: dict[str, str] = {}
        self._execution_intent_event_ids: dict[str, str] = {}
        self.orders: dict[str, OrderAggregate] = {}
        self.blocked_scopes: set[tuple[str, str]] = set()
        self._event_sequence = 0

    def accept_plan(
        self,
        plan: ExecutableTradePlan,
        *,
        now: datetime,
        current_index: int,
        mark_price: Decimal,
        versions: StateVersions,
    ) -> ExecutionResult:
        scope = (plan.portfolio_id, plan.instrument)
        if scope in self.blocked_scopes:
            raise ReconciliationBlocked(
                PaperReasonCode.RECONCILIATION_BLOCKED,
                "execution scope is blocked",
            )
        existing_hash = self._plan_hashes.get(plan.plan_id)
        if existing_hash is not None:
            if existing_hash != plan.content_hash:
                raise PlanValidationError(
                    PaperReasonCode.PLAN_ID_CONTENT_MISMATCH,
                    "plan_id was already used with different content",
                )
            return self._plan_results[plan.plan_id]
        plan.validate(now, versions)
        if plan.configuration_hash != self.config.canonical_hash:
            raise PlanValidationError(
                PaperReasonCode.INVALID_CONFIGURATION,
                "plan configuration hash does not match active paper execution configuration",
            )
        metadata = self.metadata_provider.snapshot(plan.instrument, now)
        if (
            metadata.instrument != plan.instrument
            or metadata.metadata_version != plan.metadata_version
        ):
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "plan metadata version does not match venue snapshot",
            )

        try:
            prepared = self._prepare_plan(
                plan=plan,
                metadata=metadata,
                now=now,
                mark_price=mark_price,
                scope=scope,
            )
        except PaperExecutionError as exc:
            return self._record_failure(
                plan,
                now,
                (),
                exc.reason_code,
                str(exc),
            )
        except Exception as exc:
            self.blocked_scopes.add(scope)
            return self._record_failure(
                plan,
                now,
                (),
                PaperReasonCode.INTERNAL_UNEXPECTED_ERROR,
                f"unexpected preflight error: {type(exc).__name__}",
            )

        intent_event = self.journal.append(
            timestamp=now,
            event_type="execution.intent_recorded",
            aggregate_id=plan.plan_id,
            payload={
                "plan_id": plan.plan_id,
                "content_hash": plan.content_hash,
                "portfolio_id": plan.portfolio_id,
                "instrument": plan.instrument,
                "intents": self._execution_intent_rows(plan, prepared),
            },
            correlation_id=plan.plan_id,
            symbol=plan.instrument,
        )
        self._execution_intent_event_ids[plan.plan_id] = intent_event.event_id

        submitted_client_ids: list[str] = []
        canceled_targets: list[str] = []
        core_mutation_committed = False
        current_core_invocation: CoreInvocationRef | None = None
        try:
            for item in prepared:
                intent = item.intent
                if intent.role is IntentRole.CANCEL:
                    cancel_client_order_id = intent.cancel_client_order_id
                    if cancel_client_order_id is None:
                        raise PlanValidationError(
                            PaperReasonCode.INVARIANT_VIOLATION,
                            "prepared cancel intent is missing its target",
                        )
                    cancel_request = CancelRequest(
                        client_order_id=cancel_client_order_id,
                        reason="plan_cancel_intent",
                    )
                    current_core_invocation = self._core_invocation_ref(
                        "cancel_order",
                        request=cancel_request,
                        timestamp=now,
                        source_module=self.MODULE_ID,
                    )
                    matched = self.venue.cancel(
                        cancel_request,
                        timestamp=now,
                        source_module=self.MODULE_ID,
                    )
                    core_mutation_committed = (
                        core_mutation_committed or current_core_invocation is not None
                    )
                    if current_core_invocation is not None and not self._core_receipt_matches(
                        current_core_invocation
                    ):
                        raise RuntimeError("Core cancel returned without its exact matching receipt")
                    if matched == 0:
                        raise VenueRuleRejection(
                            PaperReasonCode.CANCEL_TARGET_NOT_FOUND,
                            f"cancel target disappeared: {cancel_client_order_id}",
                        )
                    canceled_targets.append(cancel_client_order_id)
                    aggregate = self.orders.get(cancel_client_order_id)
                    if aggregate is not None and not aggregate.state.terminal:
                        aggregate.transition(PaperOrderState.CANCEL_PENDING)
                        aggregate.transition(PaperOrderState.CANCELED)
                    self.journal.append(
                        timestamp=now,
                        event_type="order.cancel_requested",
                        aggregate_id=cancel_client_order_id,
                        payload={
                            "plan_id": plan.plan_id,
                            "intent_id": intent.intent_id,
                        },
                        correlation_id=plan.plan_id,
                        causation_id=intent_event.event_id,
                        symbol=plan.instrument,
                    )
                    continue

                if item.client_order_id is None or item.normalized is None:
                    raise PlanValidationError(
                        PaperReasonCode.INVARIANT_VIOLATION,
                        "prepared order intent is incomplete",
                    )
                try:
                    current_core_invocation = self._core_invocation_ref(
                        "submit_order",
                        request=item.normalized.request,
                        source_module=self.MODULE_ID,
                        created_at=now,
                        current_index=current_index,
                        mark_price=float(mark_price),
                    )
                    order = self.venue.submit(
                        item.normalized.request,
                        source_module=self.MODULE_ID,
                        created_at=now,
                        current_index=current_index,
                        mark_price=float(mark_price),
                    )
                except CorePaperCommittedProjectionUnavailable:
                    core_mutation_committed = True
                    raise
                core_mutation_committed = (
                    core_mutation_committed or current_core_invocation is not None
                )
                if current_core_invocation is not None and not self._core_receipt_matches(
                    current_core_invocation
                ):
                    raise RuntimeError("Core submit returned without its exact matching receipt")
                if order is None:
                    raise VenueRuleRejection(
                        PaperReasonCode.ORDER_REJECTED,
                        f"Core paper venue rejected intent {intent.intent_id}",
                    )
                resolved_quantity = Decimal(str(order.quantity))
                if not resolved_quantity.is_finite() or resolved_quantity <= 0:
                    raise PlanValidationError(
                        PaperReasonCode.INVARIANT_VIOLATION,
                        "Core paper venue returned an invalid order quantity",
                    )
                normalized_quantity = item.normalized.audit.normalized_quantity
                if normalized_quantity is not None and resolved_quantity != normalized_quantity:
                    raise PlanValidationError(
                        PaperReasonCode.INVARIANT_VIOLATION,
                        "Core paper venue changed an explicit normalized quantity",
                    )
                aggregate = OrderAggregate(
                    client_order_id=item.client_order_id,
                    core_order_id=order.id,
                    requested_quantity=resolved_quantity,
                    state=PaperOrderState.SUBMITTED,
                    portfolio_id=plan.portfolio_id,
                    instrument=plan.instrument,
                    intent_role=intent.role.value,
                )
                self.orders[item.client_order_id] = aggregate
                submitted_client_ids.append(item.client_order_id)
                self.journal.append(
                    timestamp=now,
                    event_type="order.submitted",
                    aggregate_id=item.client_order_id,
                    payload={
                        "plan_id": plan.plan_id,
                        "intent_id": intent.intent_id,
                        "core_order_id": order.id,
                        "quantity": resolved_quantity,
                        "price": item.normalized.audit.normalized_price,
                        "notional": item.normalized.audit.notional,
                        "metadata_version": metadata.metadata_version,
                    },
                    correlation_id=plan.plan_id,
                    causation_id=intent_event.event_id,
                    symbol=plan.instrument,
                )
            self._before_plan_acceptance(
                plan=plan,
                prepared=prepared,
                submitted_client_ids=tuple(submitted_client_ids),
                timestamp=now,
                scope=scope,
            )
        except CorePaperCommittedProjectionUnavailable as exc:
            core_mutation_committed = True
            self._raise_committed_core_reconciliation(scope, exc)
        except CorePaperEffectNotCommitted as exc:
            if core_mutation_committed:
                self._raise_committed_core_reconciliation(scope, exc)
            if exc.status is CoreCommitStatus.UNRESOLVED:
                # Core may already have committed. Keep the durable intent open;
                # compensation and a terminal failure would invent an outcome.
                self._block_unresolved_core_scope(scope)
                raise ReconciliationBlocked(
                    PaperReasonCode.RECONCILIATION_BLOCKED,
                    "Core effect outcome is unresolved; durable intent requires reconciliation",
                ) from exc
            # Preserve the existing generic venue-failure behavior for known
            # non-committed Core outcomes. Only UNRESOLVED changes semantics.
            return self._handle_execution_failure(
                plan=plan,
                timestamp=now,
                scope=scope,
                submitted_client_ids=tuple(submitted_client_ids),
                canceled_targets=tuple(canceled_targets),
                reason_code=PaperReasonCode.INTERNAL_UNEXPECTED_ERROR,
                message=f"unexpected execution error: {type(exc).__name__}",
                force_block=True,
            )
        except PaperExecutionError as exc:
            if core_mutation_committed:
                self._raise_committed_core_reconciliation(scope, exc)
            return self._handle_execution_failure(
                plan=plan,
                timestamp=now,
                scope=scope,
                submitted_client_ids=tuple(submitted_client_ids),
                canceled_targets=tuple(canceled_targets),
                reason_code=exc.reason_code,
                message=str(exc),
            )
        except Exception as exc:
            if core_mutation_committed:
                self._raise_committed_core_reconciliation(scope, exc)
            return self._handle_execution_failure(
                plan=plan,
                timestamp=now,
                scope=scope,
                submitted_client_ids=tuple(submitted_client_ids),
                canceled_targets=tuple(canceled_targets),
                reason_code=PaperReasonCode.INTERNAL_UNEXPECTED_ERROR,
                message=f"unexpected execution error: {type(exc).__name__}",
                force_block=True,
            )

        result = ExecutionResult(
            plan_id=plan.plan_id,
            content_hash=plan.content_hash,
            accepted=True,
            terminal=False,
            client_order_ids=tuple(submitted_client_ids),
        )
        try:
            self.journal.append(
                timestamp=now,
                event_type="plan.accepted",
                aggregate_id=plan.plan_id,
                payload={
                    "plan_id": plan.plan_id,
                    "content_hash": plan.content_hash,
                    "portfolio_id": plan.portfolio_id,
                    "instrument": plan.instrument,
                    "client_order_ids": list(result.client_order_ids),
                },
                correlation_id=plan.plan_id,
                causation_id=intent_event.event_id,
                symbol=plan.instrument,
            )
        except Exception as exc:
            if core_mutation_committed:
                self._raise_committed_core_reconciliation(scope, exc)
            return self._handle_execution_failure(
                plan=plan,
                timestamp=now,
                scope=scope,
                submitted_client_ids=tuple(submitted_client_ids),
                canceled_targets=tuple(canceled_targets),
                reason_code=PaperReasonCode.RECONCILIATION_BLOCKED,
                message=(f"execution acceptance commit failed: {type(exc).__name__}"),
                force_block=True,
            )
        self._execution_intent_event_ids.pop(plan.plan_id, None)
        self._plan_hashes[plan.plan_id] = plan.content_hash
        self._plan_results[plan.plan_id] = result
        return result

    def _core_invocation_ref(self, operation: str, **fields: object) -> CoreInvocationRef | None:
        if getattr(self.venue, "core_receipt_aware", False) is not True:
            return None
        invocation_builder = getattr(self.venue, "invocation_ref", None)
        receipt_lookup = getattr(self.venue, "receipt_for_invocation", None)
        if not callable(invocation_builder) or not callable(receipt_lookup):
            raise CorePaperContextUnavailable(
                "Core-bound venue must expose invocation_ref and receipt_for_invocation"
            )
        invocation_ref = invocation_builder(operation, **fields)
        if type(invocation_ref) is not CoreInvocationRef:
            raise CorePaperContextUnavailable("Core-bound venue returned an invalid invocation ref")
        return invocation_ref

    def _core_receipt_matches(self, invocation_ref: CoreInvocationRef) -> bool:
        venue = self.venue
        if getattr(venue, "core_receipt_aware", False) is not True:
            return False
        receipt_lookup = getattr(venue, "receipt_for_invocation", None)
        if not callable(receipt_lookup):
            return False
        receipt = receipt_lookup(invocation_ref)
        if receipt is None or not invocation_ref.matches_receipt(receipt):
            return False
        receipt.__post_init__()
        return True

    def _raise_committed_core_reconciliation(
        self, scope: tuple[str, str], cause: BaseException
    ) -> None:
        self._block_unresolved_core_scope(scope)
        raise ReconciliationBlocked(
            PaperReasonCode.RECONCILIATION_BLOCKED,
            "Core mutation committed but Paper acknowledgement is unresolved",
        ) from cause

    def _block_unresolved_core_scope(self, scope: tuple[str, str]) -> None:
        self.blocked_scopes.add(scope)

    def _before_plan_acceptance(
        self,
        *,
        plan: ExecutableTradePlan,
        prepared: tuple[_PreparedIntent, ...],
        submitted_client_ids: tuple[str, ...],
        timestamp: datetime,
        scope: tuple[str, str],
    ) -> None:
        del plan, prepared, submitted_client_ids, timestamp, scope

    def _execution_intent_rows(
        self,
        plan: ExecutableTradePlan,
        prepared: tuple[_PreparedIntent, ...],
    ) -> list[dict[str, str | None]]:
        prepared_by_intent = {item.intent.intent_id: item for item in prepared}
        rows: list[dict[str, str | None]] = []
        for intent in plan.intents:
            item = prepared_by_intent.get(intent.intent_id)
            if intent.role is IntentRole.CANCEL:
                client_order_id = None
            elif item is not None and item.client_order_id is not None:
                client_order_id = item.client_order_id
            else:
                client_order_id = self.id_factory.create(
                    plan_id=plan.plan_id,
                    intent_id=intent.intent_id,
                    role=intent.role.value,
                )
            rows.append(
                {
                    "intent_id": intent.intent_id,
                    "role": intent.role.value,
                    "client_order_id": client_order_id,
                    "cancel_client_order_id": (intent.cancel_client_order_id),
                }
            )
        return rows

    def _prepare_plan(
        self,
        *,
        plan: ExecutableTradePlan,
        metadata: VenueMetadataSnapshot,
        now: datetime,
        mark_price: Decimal,
        scope: tuple[str, str],
    ) -> tuple[_PreparedIntent, ...]:
        pending_by_client = {
            snapshot.client_order_id: snapshot
            for snapshot in self.venue.pending_snapshots()
            if snapshot.client_order_id is not None
        }
        prepared: list[_PreparedIntent] = []
        reserved_ids: set[str] = set()
        new_order_count = 0
        new_conditional_count = 0
        for intent in plan.intents:
            if intent.role is IntentRole.CANCEL:
                cancel_client_order_id = intent.cancel_client_order_id
                if cancel_client_order_id is None:
                    raise PlanValidationError(
                        PaperReasonCode.INVARIANT_VIOLATION,
                        "cancel intent is missing its target",
                    )
                aggregate = self.orders.get(cancel_client_order_id)
                if (
                    cancel_client_order_id not in pending_by_client
                    or aggregate is None
                    or (aggregate.portfolio_id, aggregate.instrument) != scope
                ):
                    raise VenueRuleRejection(
                        PaperReasonCode.CANCEL_TARGET_NOT_FOUND,
                        f"cancel target not found in scope: {cancel_client_order_id}",
                    )
                prepared.append(_PreparedIntent(intent=intent))
                continue
            client_id = self.id_factory.create(
                plan_id=plan.plan_id,
                intent_id=intent.intent_id,
                role=intent.role.value,
            )
            if client_id in self.orders or client_id in reserved_ids:
                raise PlanValidationError(
                    PaperReasonCode.CLIENT_ORDER_ID_COLLISION,
                    client_id,
                )
            reserved_ids.add(client_id)
            normalized = self.rule_engine.normalize(
                intent=intent,
                metadata=metadata,
                client_order_id=client_id,
                reference_price=mark_price,
                event_time=now,
            )
            normalized = replace(
                normalized,
                request=replace(
                    normalized.request,
                    tag=build_snapshot_tag(
                        portfolio_id=plan.portfolio_id,
                        instrument=plan.instrument,
                        role=intent.role,
                    ),
                ),
            )
            prepared.append(
                _PreparedIntent(
                    intent=intent,
                    client_order_id=client_id,
                    normalized=normalized,
                )
            )
            new_order_count += 1
            if intent.role in {IntentRole.STOP, IntentRole.TARGET}:
                new_conditional_count += 1

        cancel_targets = {
            item.intent.cancel_client_order_id
            for item in prepared
            if item.intent.role is IntentRole.CANCEL
            and item.intent.cancel_client_order_id is not None
        }
        existing_open = sum(
            1
            for client_id, aggregate in self.orders.items()
            if (aggregate.portfolio_id, aggregate.instrument) == scope
            and not aggregate.state.terminal
            and client_id not in cancel_targets
        )
        existing_conditional = sum(
            1
            for client_id, aggregate in self.orders.items()
            if (aggregate.portfolio_id, aggregate.instrument) == scope
            and not aggregate.state.terminal
            and client_id not in cancel_targets
            and aggregate.intent_role in {IntentRole.STOP.value, IntentRole.TARGET.value}
        )
        if existing_open + new_order_count > self.config.maximum_open_orders:
            raise VenueRuleRejection(
                PaperReasonCode.ORDER_LIMIT_EXCEEDED,
                "maximum_open_orders would be exceeded",
            )
        if existing_conditional + new_conditional_count > self.config.maximum_conditional_orders:
            raise VenueRuleRejection(
                PaperReasonCode.ORDER_LIMIT_EXCEEDED,
                "maximum_conditional_orders would be exceeded",
            )
        return tuple(prepared)

    def _handle_execution_failure(
        self,
        *,
        plan: ExecutableTradePlan,
        timestamp: datetime,
        scope: tuple[str, str],
        submitted_client_ids: tuple[str, ...],
        canceled_targets: tuple[str, ...],
        reason_code: PaperReasonCode,
        message: str,
        force_block: bool = False,
    ) -> ExecutionResult:
        rollback_failed = self._compensate_submitted_orders(
            client_ids=submitted_client_ids,
            timestamp=timestamp,
            scope=scope,
            plan_id=plan.plan_id,
            instrument=plan.instrument,
        )
        if force_block:
            self.blocked_scopes.add(scope)
        if rollback_failed or canceled_targets:
            self.blocked_scopes.add(scope)
            reason_code = PaperReasonCode.RECONCILIATION_BLOCKED
        return self._record_failure(
            plan,
            timestamp,
            submitted_client_ids,
            reason_code,
            message,
        )

    def _record_failure(
        self,
        plan: ExecutableTradePlan,
        timestamp: datetime,
        client_order_ids: tuple[str, ...],
        reason_code: PaperReasonCode,
        message: str,
    ) -> ExecutionResult:
        result = ExecutionResult(
            plan_id=plan.plan_id,
            content_hash=plan.content_hash,
            accepted=False,
            terminal=True,
            client_order_ids=client_order_ids,
            reason_code=reason_code,
            message=message,
        )
        causation_id = self._execution_intent_event_ids.get(plan.plan_id)
        self.journal.append(
            timestamp=timestamp,
            event_type="execution.failed",
            aggregate_id=plan.plan_id,
            payload={
                "plan_id": plan.plan_id,
                "content_hash": plan.content_hash,
                "portfolio_id": plan.portfolio_id,
                "instrument": plan.instrument,
                "client_order_ids": list(client_order_ids),
                "reason_code": reason_code.value,
                "message": result.message,
            },
            correlation_id=plan.plan_id,
            causation_id=causation_id,
            symbol=plan.instrument,
        )
        self._execution_intent_event_ids.pop(plan.plan_id, None)
        self._plan_hashes[plan.plan_id] = plan.content_hash
        self._plan_results[plan.plan_id] = result
        return result

    def _compensate_submitted_orders(
        self,
        *,
        client_ids: tuple[str, ...],
        timestamp: datetime,
        scope: tuple[str, str],
        plan_id: str,
        instrument: str,
    ) -> bool:
        rollback_failed = False
        causation_id = self._execution_intent_event_ids.get(plan_id)
        for client_id in reversed(client_ids):
            try:
                matched = self.venue.cancel(
                    CancelRequest(
                        client_order_id=client_id,
                        reason="plan_submission_rollback",
                    ),
                    timestamp=timestamp,
                    source_module=self.MODULE_ID,
                )
            except Exception:
                matched = 0
            aggregate = self.orders.get(client_id)
            if matched > 0 and aggregate is not None:
                if not aggregate.state.terminal:
                    aggregate.transition(PaperOrderState.CANCEL_PENDING)
                    aggregate.transition(PaperOrderState.CANCELED)
                self.journal.append(
                    timestamp=timestamp,
                    event_type="order.rollback_canceled",
                    aggregate_id=client_id,
                    payload={
                        "plan_id": plan_id,
                        "client_order_id": client_id,
                    },
                    correlation_id=plan_id,
                    causation_id=causation_id,
                    symbol=instrument,
                )
            else:
                rollback_failed = True
                self.journal.append(
                    timestamp=timestamp,
                    event_type="order.rollback_failed",
                    aggregate_id=client_id,
                    payload={
                        "plan_id": plan_id,
                        "client_order_id": client_id,
                    },
                    correlation_id=plan_id,
                    causation_id=causation_id,
                    symbol=instrument,
                )
        if rollback_failed:
            self.blocked_scopes.add(scope)
        return rollback_failed

    def ingest_order_event(self, event: OrderEvent) -> bool:
        client_id = event.order.client_order_id
        if client_id is None or client_id not in self.orders:
            return False
        self._event_sequence += 1
        event_id = event.event_id or f"core-order-{self._event_sequence:012d}"
        changed = self.orders[client_id].apply_core_event(
            event,
            sequence=self._event_sequence,
            event_id=event_id,
        )
        if changed:
            aggregate = self.orders[client_id]
            self.journal.append(
                timestamp=event.timestamp,
                event_type=f"order.{event.event}",
                aggregate_id=client_id,
                payload={
                    "core_order_id": event.order.id,
                    "state": aggregate.state.value,
                    "filled_quantity": aggregate.filled_quantity,
                    "remaining_quantity": aggregate.remaining_quantity,
                },
                correlation_id=client_id,
                symbol=aggregate.instrument or None,
            )
        return changed

    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
    ) -> ReconciliationReport:
        scope = (portfolio_id, instrument)
        internal = {
            client_id: aggregate
            for client_id, aggregate in self.orders.items()
            if (aggregate.portfolio_id, aggregate.instrument) == scope
        }
        report = self.reconciliation_engine.reconcile_orders(
            internal=internal,
            venue_orders=self.venue.pending_snapshots(),
            mode=self.config.reconciliation_mode,
            known_client_order_ids=set(self.orders),
            owned_prefix=f"{self.config.module_namespace}-",
        )
        if report.blocked:
            self.blocked_scopes.add(scope)
        return report
