from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping

from modular_core import (
    CancelRequest,
    OrderEvent,
    PositionSnapshot,
    PositionUpdateRequest,
)
from modular_core.events import EventStore

from .adapter import CorePaperVenue
from .config import PaperExecutionConfig
from .contracts import (
    ExecutableTradePlan,
    ExecutionResult,
    ReconciliationFinding,
    StateVersions,
    VenueMetadataSnapshot,
)
from .coordinator import (
    InstrumentMetadataProvider,
    PaperExecutionCoordinator as BaseCoordinator,
    _PreparedIntent,
)
from .enums import IntentRole, PaperOrderState, PaperReasonCode, PlanAction
from .exceptions import (
    OrderStateError,
    PlanValidationError,
    ReconciliationBlocked,
)
from .journal import ExecutionEventJournal
from .manifest import build_manifest
from .orchestration_state import ExecutionOrchestrationState
from .oco import OcoAdjustmentKind, OcoGroup
from .oco_runtime import build_submitted_oco_groups, find_oco_group
from .position_reconciliation import ExpectedPositionState, reconcile_position
from .protection import (
    DeferredProtectionLeg,
    ProtectionActivationPolicy,
    ProtectionCoordinator,
)
from .reconciliation import ReconciliationReport
from .reconciliation_policy import LifecycleState, PolicyDecision, decide_reconciliation
from .recovery import restore_execution_state, snapshot_execution_state

RecoveryComponent = Literal["orders", "position"]
_RECOVERY_COMPONENTS: frozenset[RecoveryComponent] = frozenset(("orders", "position"))
_LATE_CANCEL_EVENTS = frozenset(("cancel_pending", "cancelled"))


class PaperExecutionCoordinator(BaseCoordinator):
    """Core coordinator with transactional recovery and fill-driven protection."""

    name = BaseCoordinator.MODULE_ID
    manifest = build_manifest()

    def __init__(
        self,
        *,
        config: PaperExecutionConfig,
        venue: CorePaperVenue,
        metadata_provider: InstrumentMetadataProvider,
        run_id: str,
        event_store: EventStore | None = None,
    ) -> None:
        super().__init__(
            config=config,
            venue=venue,
            metadata_provider=metadata_provider,
            run_id=run_id,
        )
        if event_store is not None:
            self.journal = ExecutionEventJournal(run_id, event_store)
        self._plan_scopes: dict[str, tuple[str, str]] = {}
        self._recovery_requirements: dict[tuple[str, str], set[RecoveryComponent]] = {}
        self._reconciliation_blocks: dict[tuple[str, str], set[RecoveryComponent]] = {}
        self._lifecycle = LifecycleState.READY
        self.orchestration_state = ExecutionOrchestrationState()
        self._pending_protection_by_plan: dict[str, ProtectionCoordinator] = {}

    @property
    def lifecycle(self) -> LifecycleState:
        return self._lifecycle

    def _block_unresolved_core_scope(self, scope: tuple[str, str]) -> None:
        super()._block_unresolved_core_scope(scope)
        self._require_full_reconciliation(scope)
        self._lifecycle = LifecycleState.BLOCKED

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
        if scope in self._recovery_requirements:
            raise ReconciliationBlocked(
                PaperReasonCode.RECONCILIATION_BLOCKED,
                "execution scope requires recovery reconciliation",
            )
        was_blocked = scope in self.blocked_scopes
        try:
            result = super().accept_plan(
                plan,
                now=now,
                current_index=current_index,
                mark_price=mark_price,
                versions=versions,
            )
        except Exception:
            self._pending_protection_by_plan.pop(plan.plan_id, None)
            raise
        if not was_blocked and scope in self.blocked_scopes:
            self._require_full_reconciliation(scope)

        self._plan_scopes.setdefault(plan.plan_id, scope)
        deferred = self._pending_protection_by_plan.pop(plan.plan_id, None)
        if not result.accepted or deferred is None:
            return result

        try:
            self.orchestration_state.register_protection(deferred)
            self.journal.append(
                timestamp=now,
                event_type="protection.deferred",
                aggregate_id=deferred.entry_client_order_id,
                payload={
                    "plan_id": plan.plan_id,
                    "entry_client_order_id": deferred.entry_client_order_id,
                    "protection_legs": sorted(deferred.legs),
                },
                correlation_id=plan.plan_id,
                symbol=plan.instrument,
            )
        except Exception as exc:
            self._require_full_reconciliation(scope)
            self._lifecycle = LifecycleState.BLOCKED
            self._plan_results.pop(plan.plan_id, None)
            self._plan_hashes.pop(plan.plan_id, None)
            raise ReconciliationBlocked(
                PaperReasonCode.RECONCILIATION_BLOCKED,
                "entry was accepted but deferred protection registration failed",
            ) from exc
        return result

    def _before_plan_acceptance(
        self,
        *,
        plan: ExecutableTradePlan,
        prepared: tuple[_PreparedIntent, ...],
        submitted_client_ids: tuple[str, ...],
        timestamp: datetime,
        scope: tuple[str, str],
    ) -> None:
        super()._before_plan_acceptance(
            plan=plan,
            prepared=prepared,
            submitted_client_ids=submitted_client_ids,
            timestamp=timestamp,
            scope=scope,
        )
        groups = build_submitted_oco_groups(
            plan=plan,
            submitted_client_ids=submitted_client_ids,
            orders=self.orders,
            id_factory=self.id_factory,
        )
        registered: list[str] = []
        try:
            for group in groups:
                if group.group_id in self.orchestration_state.oco_groups:
                    raise PlanValidationError(
                        PaperReasonCode.INVALID_PLAN,
                        "synthetic OCO group id is already registered",
                    )
                self.orchestration_state.register_oco(group)
                registered.append(group.group_id)
                self.journal.append(
                    timestamp=timestamp,
                    event_type="oco.registered",
                    aggregate_id=group.group_id,
                    payload={
                        "plan_id": plan.plan_id,
                        "position_quantity": group.position_quantity,
                        "members": sorted(group.members),
                    },
                    correlation_id=plan.plan_id,
                    symbol=plan.instrument,
                )
        except Exception:
            for group_id in registered:
                self.orchestration_state.oco_groups.pop(group_id, None)
            raise

    def _prepare_plan(
        self,
        *,
        plan: ExecutableTradePlan,
        metadata: VenueMetadataSnapshot,
        now: datetime,
        mark_price: Decimal,
        scope: tuple[str, str],
    ) -> tuple[_PreparedIntent, ...]:
        self._pending_protection_by_plan.pop(plan.plan_id, None)
        prepared = super()._prepare_plan(
            plan=plan,
            metadata=metadata,
            now=now,
            mark_price=mark_price,
            scope=scope,
        )
        protection_items = tuple(
            item for item in prepared if item.intent.role in {IntentRole.STOP, IntentRole.TARGET}
        )
        if not protection_items:
            return prepared
        if plan.action is not PlanAction.OPEN_POSITION:
            return prepared

        entry_items = tuple(item for item in prepared if item.intent.role is IntentRole.ENTRY)
        unsupported = tuple(
            item
            for item in prepared
            if item.intent.role not in {IntentRole.ENTRY, IntentRole.STOP, IntentRole.TARGET}
        )
        stop_items = tuple(item for item in protection_items if item.intent.role is IntentRole.STOP)
        target_items = tuple(
            item for item in protection_items if item.intent.role is IntentRole.TARGET
        )
        if len(entry_items) != 1 or unsupported:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "deferred bracket v1 requires exactly one entry intent",
            )
        if len(stop_items) != 1 or len(target_items) > 1:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "deferred bracket v1 requires one Stop and at most one Target",
            )
        group_ids = {
            item.intent.oco_group_id
            for item in protection_items
            if item.intent.oco_group_id is not None
        }
        if len(group_ids) > 1:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "deferred Stop and Target must belong to the same OCO group",
            )

        entry = entry_items[0]
        if entry.client_order_id is None or entry.normalized is None:
            raise PlanValidationError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "prepared entry intent is incomplete",
            )
        entry_quantity = entry.normalized.audit.normalized_quantity
        if entry_quantity is None:
            raise PlanValidationError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "deferred bracket entry requires an explicit normalized quantity",
            )
        coordinator = ProtectionCoordinator(entry.client_order_id, entry_quantity)
        for item in protection_items:
            if item.client_order_id is None or item.normalized is None:
                raise PlanValidationError(
                    PaperReasonCode.INVARIANT_VIOLATION,
                    "prepared protection intent is incomplete",
                )
            quantity = item.normalized.audit.normalized_quantity
            trigger_price = item.normalized.audit.normalized_price
            if quantity is None:
                raise PlanValidationError(
                    PaperReasonCode.INVARIANT_VIOLATION,
                    "deferred protection requires an explicit normalized quantity",
                )
            if quantity != entry_quantity:
                raise PlanValidationError(
                    PaperReasonCode.INVALID_PLAN,
                    "deferred bracket v1 does not support partial protection quantities",
                )
            if trigger_price is None:
                raise PlanValidationError(
                    PaperReasonCode.INVALID_PLAN,
                    "deferred protection requires a normalized trigger price",
                )
            coordinator.register_leg(
                DeferredProtectionLeg(
                    client_order_id=item.client_order_id,
                    role=item.intent.role,
                    planned_quantity=quantity,
                    trigger_price=trigger_price,
                    activation_policy=(
                        ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL
                        if item.intent.role is IntentRole.STOP
                        else ProtectionActivationPolicy.AFTER_ENTRY_TERMINAL
                    ),
                )
            )
        self._pending_protection_by_plan[plan.plan_id] = coordinator
        return entry_items

    def ingest_order_event(self, event: OrderEvent) -> bool:
        if self._is_late_cancel_confirmation(event):
            return False
        client_id = event.order.client_order_id
        group = (
            find_oco_group(
                self.orchestration_state.oco_groups,
                client_id,
            )
            if client_id is not None
            else None
        )
        try:
            changed = super().ingest_order_event(event)
        except OrderStateError as exc:
            if client_id is None or group is None:
                raise
            aggregate = self.orders.get(client_id)
            if aggregate is None:
                raise
            scope = (aggregate.portfolio_id, aggregate.instrument)
            self._block_oco_scope(
                group=group,
                client_id=client_id,
                scope=scope,
                instrument=aggregate.instrument,
                timestamp=event.timestamp,
                event_type="oco.race_detected",
                message=f"late OCO event violated terminal state: {type(exc).__name__}",
            )
            raise ReconciliationBlocked(
                PaperReasonCode.RECONCILIATION_BLOCKED,
                "late OCO fill/cancel race requires reconciliation",
            ) from exc
        if not changed:
            return False
        if client_id is None:
            return True

        aggregate = self.orders[client_id]
        scope = (aggregate.portfolio_id, aggregate.instrument)
        if group is not None:
            self._process_oco_event(
                group=group,
                client_id=client_id,
                timestamp=event.timestamp,
                scope=scope,
                instrument=aggregate.instrument,
            )

        protection = self.orchestration_state.protection_by_entry.get(client_id)
        if protection is None:
            return True
        try:
            protection.observe_entry_state(
                aggregate.filled_quantity,
                entry_terminal=aggregate.state.terminal,
            )
            self._apply_pending_protection(
                client_id=client_id,
                timestamp=event.timestamp,
            )
            if aggregate.state is PaperOrderState.PARTIALLY_FILLED:
                self._cancel_partial_entry_remainder(
                    client_id=client_id,
                    timestamp=event.timestamp,
                )
        except Exception as exc:
            self._block_protection_failure(
                scope=scope,
                client_id=client_id,
                timestamp=event.timestamp,
                instrument=aggregate.instrument,
                exc=exc,
            )
        return True

    def _process_oco_event(
        self,
        *,
        group: OcoGroup,
        client_id: str,
        timestamp: datetime,
        scope: tuple[str, str],
        instrument: str,
    ) -> None:
        aggregate = self.orders[client_id]
        try:
            group.record_fill(client_id, aggregate.filled_quantity)
            adjustments = group.required_adjustments()
        except OrderStateError as exc:
            self._block_oco_scope(
                group=group,
                client_id=client_id,
                scope=scope,
                instrument=instrument,
                timestamp=timestamp,
                event_type="oco.race_detected",
                message=str(exc),
            )
            raise ReconciliationBlocked(
                PaperReasonCode.RECONCILIATION_BLOCKED,
                "synthetic OCO state requires reconciliation",
            ) from exc

        for adjustment in adjustments:
            sibling_id = adjustment.client_order_id
            sibling = self.orders.get(sibling_id)
            if sibling is None:
                self._block_oco_scope(
                    group=group,
                    client_id=sibling_id,
                    scope=scope,
                    instrument=instrument,
                    timestamp=timestamp,
                    event_type="oco.adjustment_failed",
                    message="OCO sibling projection is missing",
                )
                return
            try:
                matched = self.venue.cancel(
                    CancelRequest(
                        client_order_id=sibling_id,
                        reason="paper_execution_oco_adjustment",
                    ),
                    timestamp=timestamp,
                    source_module=self.MODULE_ID,
                )
            except Exception as exc:
                self._block_oco_scope(
                    group=group,
                    client_id=sibling_id,
                    scope=scope,
                    instrument=instrument,
                    timestamp=timestamp,
                    event_type="oco.adjustment_failed",
                    message=f"venue cancel failed: {type(exc).__name__}",
                )
                return
            if matched <= 0:
                self._block_oco_scope(
                    group=group,
                    client_id=sibling_id,
                    scope=scope,
                    instrument=instrument,
                    timestamp=timestamp,
                    event_type="oco.adjustment_failed",
                    message="venue did not confirm OCO sibling cancellation",
                )
                return
            if not sibling.state.terminal:
                if sibling.state is not PaperOrderState.CANCEL_PENDING:
                    sibling.transition(PaperOrderState.CANCEL_PENDING)
                sibling.transition(PaperOrderState.CANCELED)
            group.record_canceled(sibling_id)
            self.journal.append(
                timestamp=timestamp,
                event_type="oco.sibling_canceled",
                aggregate_id=group.group_id,
                payload={
                    "filled_client_order_id": client_id,
                    "canceled_client_order_id": sibling_id,
                    "adjustment_kind": adjustment.kind.value,
                    "remaining_quantity": adjustment.remaining_quantity,
                },
                correlation_id=group.group_id,
                symbol=instrument,
            )
            if adjustment.kind is OcoAdjustmentKind.REDUCE_REMAINING_TO:
                self._block_oco_scope(
                    group=group,
                    client_id=sibling_id,
                    scope=scope,
                    instrument=instrument,
                    timestamp=timestamp,
                    event_type="oco.residual_protection_blocked",
                    message=(
                        "Core paper venue has no amend path; residual protection "
                        "must be reconciled before another plan"
                    ),
                )
                return

    def _block_oco_scope(
        self,
        *,
        group: OcoGroup,
        client_id: str,
        scope: tuple[str, str],
        instrument: str,
        timestamp: datetime,
        event_type: str,
        message: str,
    ) -> None:
        self.blocked_scopes.add(scope)
        self._require_full_reconciliation(scope)
        self._lifecycle = LifecycleState.BLOCKED
        self.journal.append(
            timestamp=timestamp,
            event_type=event_type,
            aggregate_id=group.group_id,
            payload={
                "client_order_id": client_id,
                "message": message,
            },
            correlation_id=group.group_id,
            symbol=instrument,
        )

    def retry_pending_protection(
        self,
        *,
        client_order_id: str,
        timestamp: datetime,
    ) -> bool:
        aggregate = self.orders.get(client_order_id)
        protection = self.orchestration_state.protection_by_entry.get(client_order_id)
        if aggregate is None or protection is None:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "unknown deferred protection entry",
            )
        scope = (aggregate.portfolio_id, aggregate.instrument)
        try:
            protection.observe_entry_state(
                aggregate.filled_quantity,
                entry_terminal=aggregate.state.terminal,
            )
            changed = self._apply_pending_protection(
                client_id=client_order_id,
                timestamp=timestamp,
            )
            if aggregate.state in {
                PaperOrderState.PARTIALLY_FILLED,
                PaperOrderState.CANCEL_PENDING,
            }:
                self._cancel_partial_entry_remainder(
                    client_id=client_order_id,
                    timestamp=timestamp,
                )
                changed = True
            return changed
        except Exception as exc:
            self._block_protection_failure(
                scope=scope,
                client_id=client_order_id,
                timestamp=timestamp,
                instrument=aggregate.instrument,
                exc=exc,
            )
        raise AssertionError("unreachable protection retry state")

    def _apply_pending_protection(
        self,
        *,
        client_id: str,
        timestamp: datetime,
    ) -> bool:
        aggregate = self.orders[client_id]
        protection = self.orchestration_state.protection_by_entry[client_id]
        activations = protection.pending_activations()
        if not activations:
            return False
        stop_price = protection.desired_trigger_price(IntentRole.STOP)
        target_price = protection.desired_trigger_price(IntentRole.TARGET)
        if stop_price is None:
            raise ReconciliationBlocked(
                PaperReasonCode.POSITION_UNPROTECTED,
                "factual entry fill has no desired Stop",
            )
        reference_price = aggregate.average_fill_price
        if reference_price is None:
            raise ReconciliationBlocked(
                PaperReasonCode.POSITION_UNPROTECTED,
                "factual entry fill has no average fill price",
            )
        self.journal.append(
            timestamp=timestamp,
            event_type="protection.activation_requested",
            aggregate_id=client_id,
            payload={
                "stop_price": stop_price,
                "target_price": target_price,
                "filled_quantity": aggregate.filled_quantity,
                "entry_terminal": protection.entry_terminal,
                "activation_ids": [item.client_order_id for item in activations],
            },
            correlation_id=client_id,
            symbol=aggregate.instrument,
        )
        updated = self.venue.update_position(
            PositionUpdateRequest(
                stop_loss=float(stop_price),
                take_profit=(float(target_price) if target_price is not None else None),
                reason="paper_execution_deferred_protection",
            ),
            timestamp=timestamp,
            source_module=self.MODULE_ID,
            mark_price=float(reference_price),
        )
        if not updated:
            raise ReconciliationBlocked(
                PaperReasonCode.POSITION_UNPROTECTED,
                "Core paper venue rejected deferred protection activation",
            )
        protection.commit_activations(activations)
        self.journal.append(
            timestamp=timestamp,
            event_type="protection.activated",
            aggregate_id=client_id,
            payload={
                "stop_price": protection.active_trigger_price(IntentRole.STOP),
                "target_price": protection.active_trigger_price(IntentRole.TARGET),
                "protected_quantity": protection.protected_quantity,
                "entry_terminal": protection.entry_terminal,
            },
            correlation_id=client_id,
            symbol=aggregate.instrument,
        )
        return True

    def _cancel_partial_entry_remainder(
        self,
        *,
        client_id: str,
        timestamp: datetime,
    ) -> None:
        aggregate = self.orders[client_id]
        protection = self.orchestration_state.protection_by_entry[client_id]
        if aggregate.remaining_quantity <= 0:
            return
        if aggregate.state is PaperOrderState.PARTIALLY_FILLED:
            aggregate.transition(PaperOrderState.CANCEL_PENDING)
            self.journal.append(
                timestamp=timestamp,
                event_type="entry.remainder_cancel_requested",
                aggregate_id=client_id,
                payload={
                    "filled_quantity": aggregate.filled_quantity,
                    "remaining_quantity": aggregate.remaining_quantity,
                },
                correlation_id=client_id,
                symbol=aggregate.instrument,
            )
        if aggregate.state is not PaperOrderState.CANCEL_PENDING:
            return
        matched = self.venue.cancel(
            CancelRequest(
                client_order_id=client_id,
                reason="partial_entry_remainder_cancel",
            ),
            timestamp=timestamp,
            source_module=self.MODULE_ID,
        )
        if matched == 0:
            raise ReconciliationBlocked(
                PaperReasonCode.RECONCILIATION_BLOCKED,
                "Core paper venue could not cancel the partial entry remainder",
            )
        aggregate.transition(PaperOrderState.CANCELED)
        protection.observe_entry_state(
            aggregate.filled_quantity,
            entry_terminal=True,
        )
        self.journal.append(
            timestamp=timestamp,
            event_type="entry.remainder_canceled",
            aggregate_id=client_id,
            payload={
                "filled_quantity": aggregate.filled_quantity,
                "remaining_quantity": aggregate.remaining_quantity,
            },
            correlation_id=client_id,
            symbol=aggregate.instrument,
        )
        self._apply_pending_protection(
            client_id=client_id,
            timestamp=timestamp,
        )

    def _is_late_cancel_confirmation(self, event: OrderEvent) -> bool:
        if event.event not in _LATE_CANCEL_EVENTS:
            return False
        client_id = event.order.client_order_id
        if client_id is None:
            return False
        aggregate = self.orders.get(client_id)
        if aggregate is None or aggregate.state is not PaperOrderState.CANCELED:
            return False
        return (
            event.order.id == aggregate.core_order_id
            and Decimal(str(event.order.filled_quantity)) == aggregate.filled_quantity
        )

    def _block_protection_failure(
        self,
        *,
        scope: tuple[str, str],
        client_id: str,
        timestamp: datetime,
        instrument: str,
        exc: Exception,
    ) -> None:
        self._require_full_reconciliation(scope)
        self._lifecycle = LifecycleState.BLOCKED
        try:
            self.journal.append(
                timestamp=timestamp,
                event_type="protection.activation_failed",
                aggregate_id=client_id,
                payload={"error_type": type(exc).__name__},
                correlation_id=client_id,
                symbol=instrument,
            )
        except Exception:
            self._lifecycle = LifecycleState.FAILED
        if isinstance(exc, ReconciliationBlocked):
            raise exc
        raise ReconciliationBlocked(
            PaperReasonCode.POSITION_UNPROTECTED,
            f"deferred protection activation failed: {type(exc).__name__}",
        ) from exc

    def _record_failure(
        self,
        plan: ExecutableTradePlan,
        timestamp: datetime,
        client_order_ids: tuple[str, ...],
        reason_code: PaperReasonCode,
        message: str,
    ) -> ExecutionResult:
        result = super()._record_failure(
            plan,
            timestamp,
            client_order_ids,
            reason_code,
            message,
        )
        self._plan_scopes[plan.plan_id] = (
            plan.portfolio_id,
            plan.instrument,
        )
        return result

    @property
    def recovery_pending(self) -> bool:
        return bool(self._recovery_requirements)

    def snapshot_state(self) -> Mapping[str, object]:
        return snapshot_execution_state(
            configuration_hash=self.config.canonical_hash,
            plan_results=self._plan_results,
            plan_scopes=self._plan_scopes,
            orders=self.orders,
            blocked_scopes=self.blocked_scopes,
            event_sequence=self._event_sequence,
            journal_attestation=self.journal.attestation(),
            orchestration_state=self.orchestration_state,
        )

    def restore_state(self, state: Mapping[str, object]) -> None:
        self._lifecycle = LifecycleState.RECOVERING
        try:
            restored = restore_execution_state(
                state,
                expected_configuration_hash=self.config.canonical_hash,
            )
            self.journal.verify_attestation(restored.journal_attestation)
            recovery_scopes = set(restored.plan_scopes.values())
            recovery_scopes.update(
                (aggregate.portfolio_id, aggregate.instrument)
                for aggregate in restored.orders.values()
            )
            recovery_scopes.update(restored.blocked_scopes)
            self._plan_results = restored.plan_results
            self._plan_hashes = restored.plan_hashes
            self._plan_scopes = restored.plan_scopes
            self.orders = restored.orders
            self.blocked_scopes = restored.blocked_scopes
            self._event_sequence = restored.event_sequence
            self.orchestration_state = restored.orchestration_state
            self._recovery_requirements = {
                scope: set(_RECOVERY_COMPONENTS) for scope in recovery_scopes
            }
            self._reconciliation_blocks = {
                scope: set(_RECOVERY_COMPONENTS) for scope in restored.blocked_scopes
            }
            self._lifecycle = (
                LifecycleState.RECONCILING if self._recovery_requirements else LifecycleState.READY
            )
        except Exception:
            self._lifecycle = LifecycleState.FAILED
            raise

    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
    ) -> ReconciliationReport:
        scope = (portfolio_id, instrument)
        self._lifecycle = LifecycleState.RECONCILING
        report = super().reconcile(
            portfolio_id=portfolio_id,
            instrument=instrument,
        )
        decision = decide_reconciliation(
            mode=self.config.reconciliation_mode,
            findings=report.findings,
            recovery_pending=scope in self._recovery_requirements,
        )
        self._apply_policy_decision(
            scope,
            decision,
            component="orders",
        )
        return report

    def reconcile_position_state(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        expected: ExpectedPositionState | None,
        observed: PositionSnapshot | None,
    ) -> tuple[ReconciliationFinding, ...]:
        scope = (portfolio_id, instrument)
        scoped_orders = {
            client_id: aggregate
            for client_id, aggregate in self.orders.items()
            if (aggregate.portfolio_id, aggregate.instrument) == scope
        }
        self._lifecycle = LifecycleState.RECONCILING
        findings = reconcile_position(
            expected=expected,
            observed=observed,
            orders=scoped_orders,
        )
        decision = decide_reconciliation(
            mode=self.config.reconciliation_mode,
            findings=findings,
            recovery_pending=scope in self._recovery_requirements,
        )
        self._apply_policy_decision(
            scope,
            decision,
            component="position",
        )
        return findings

    def _require_full_reconciliation(
        self,
        scope: tuple[str, str],
    ) -> None:
        self._recovery_requirements.setdefault(scope, set()).update(_RECOVERY_COMPONENTS)
        self._reconciliation_blocks.setdefault(scope, set()).update(_RECOVERY_COMPONENTS)
        self.blocked_scopes.add(scope)

    def _apply_policy_decision(
        self,
        scope: tuple[str, str],
        decision: PolicyDecision,
        *,
        component: RecoveryComponent,
    ) -> None:
        blockers = self._reconciliation_blocks.setdefault(scope, set())
        if decision.block_scope:
            blockers.add(component)
        else:
            blockers.discard(component)
        if not blockers:
            self._reconciliation_blocks.pop(scope, None)
            self.blocked_scopes.discard(scope)
        else:
            self.blocked_scopes.add(scope)

        if decision.recovery_complete:
            remaining = self._recovery_requirements.get(scope)
            if remaining is not None:
                remaining.discard(component)
                if not remaining:
                    self._recovery_requirements.pop(scope, None)

        if self._reconciliation_blocks.get(scope):
            self._lifecycle = LifecycleState.BLOCKED
        elif scope in self._recovery_requirements:
            self._lifecycle = LifecycleState.RECONCILING
        else:
            self._lifecycle = decision.lifecycle
