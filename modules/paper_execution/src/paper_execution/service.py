from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from modular_core import OrderSnapshot, OrderStatus, PositionUpdateRequest
from modular_core.events import EventStore

from .adapter import CorePaperVenue
from .config import PaperExecutionConfig
from .contracts import (
    ExecutableTradePlan,
    ExecutionResult,
    StateVersions,
    VenueMetadataSnapshot,
)
from .coordinator import InstrumentMetadataProvider, _PreparedIntent
from .enums import IntentRole, PaperOrderState, PaperReasonCode, ReconciliationMode
from .exceptions import PlanValidationError, ReconciliationBlocked
from .market import VenueMarkProvider, VenueMarkSnapshot, markets_compatible
from .reconciliation import ReconciliationReport
from .reconciliation_policy import LifecycleState
from .recoverable import PaperExecutionCoordinator as RecoverableCoordinator
from .snapshot_identity import parse_snapshot_tag
from .state_machine import OrderAggregate

_SOURCE_MODULE = "paper_execution_venue_reconciliation"
_TERMINAL_EXECUTION_EVENTS = frozenset({"plan.accepted", "execution.failed"})


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value


def _execution_intent_scopes(
    store: EventStore,
) -> tuple[tuple[str, str], ...]:
    pending: dict[str, tuple[str, str, str]] = {}
    observed_scopes: set[tuple[str, str]] = set()
    for event in store.read():
        payload = dict(event.payload)
        if event.event_type == "execution.intent_recorded":
            plan_id = _identifier(payload.get("plan_id"), "intent plan_id")
            content_hash = _digest(
                payload.get("content_hash"),
                "intent content_hash",
            )
            portfolio_id = _identifier(
                payload.get("portfolio_id"),
                "intent portfolio_id",
            )
            instrument = _identifier(
                payload.get("instrument"),
                "intent instrument",
            )
            if (
                payload.get("aggregate_id") != plan_id
                or event.correlation_id != plan_id
                or event.symbol != instrument
            ):
                raise ValueError("execution intent journal identity mismatch")
            record = (content_hash, portfolio_id, instrument)
            existing = pending.get(plan_id)
            if existing is not None and existing != record:
                raise ValueError("conflicting execution intent journal records")
            if existing is not None:
                raise ValueError("duplicate unresolved execution intent")
            pending[plan_id] = record
            observed_scopes.add((portfolio_id, instrument))
            continue

        if event.event_type not in _TERMINAL_EXECUTION_EVENTS:
            continue
        plan_id_raw = payload.get("plan_id")
        if not isinstance(plan_id_raw, str) or plan_id_raw not in pending:
            continue
        content_hash, portfolio_id, instrument = pending[plan_id_raw]
        if (
            _digest(payload.get("content_hash"), "terminal content_hash") != content_hash
            or _identifier(
                payload.get("portfolio_id"),
                "terminal portfolio_id",
            )
            != portfolio_id
            or _identifier(
                payload.get("instrument"),
                "terminal instrument",
            )
            != instrument
            or payload.get("aggregate_id") != plan_id_raw
            or event.correlation_id != plan_id_raw
            or event.symbol != instrument
        ):
            raise ValueError("execution terminal journal identity mismatch")
        pending.pop(plan_id_raw)

    return tuple(sorted(observed_scopes))


class PaperExecutionCoordinator(RecoverableCoordinator):
    """Public coordinator with fail-closed market and repair boundaries."""

    def __init__(
        self,
        *,
        config: PaperExecutionConfig,
        venue: CorePaperVenue,
        metadata_provider: InstrumentMetadataProvider,
        run_id: str,
        event_store: EventStore | None = None,
        mark_provider: VenueMarkProvider | None = None,
    ) -> None:
        super().__init__(
            config=config,
            venue=venue,
            metadata_provider=metadata_provider,
            run_id=run_id,
            event_store=event_store,
        )
        if mark_provider is not None:
            self._mark_provider: VenueMarkProvider | None = mark_provider
        elif isinstance(venue, VenueMarkProvider):
            self._mark_provider = venue
        else:
            self._mark_provider = None

        intent_scopes = _execution_intent_scopes(self.journal.store)
        for scope in intent_scopes:
            self._require_full_reconciliation(scope)
        if intent_scopes:
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
        existing_hash = self._plan_hashes.get(plan.plan_id)
        if existing_hash is not None:
            if existing_hash != plan.content_hash:
                raise PlanValidationError(
                    PaperReasonCode.PLAN_ID_CONTENT_MISMATCH,
                    "plan_id was already used with different content",
                )
            return self._plan_results[plan.plan_id]
        return super().accept_plan(
            plan,
            now=now,
            current_index=current_index,
            mark_price=mark_price,
            versions=versions,
        )

    def _prepare_plan(
        self,
        *,
        plan: ExecutableTradePlan,
        metadata: VenueMetadataSnapshot,
        now: datetime,
        mark_price: Decimal,
        scope: tuple[str, str],
    ) -> tuple[_PreparedIntent, ...]:
        if not markets_compatible(plan.market, metadata.market_type):
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "plan market does not match venue market type",
            )
        return super()._prepare_plan(
            plan=plan,
            metadata=metadata,
            now=now,
            mark_price=mark_price,
            scope=scope,
        )

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
        if self._mark_provider is None:
            raise ReconciliationBlocked(
                PaperReasonCode.POSITION_UNPROTECTED,
                "deferred protection requires a fresh venue mark provider",
            )
        mark = self._mark_provider.mark_snapshot(
            aggregate.instrument,
            timestamp,
        )
        if not isinstance(mark, VenueMarkSnapshot):
            raise TypeError("mark provider returned an invalid snapshot type")
        mark.validate_for(
            instrument=aggregate.instrument,
            timestamp=timestamp,
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
                "mark_price": mark.price,
                "mark_observed_at": mark.observed_at,
                "mark_source": mark.source,
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
            mark_price=float(mark.price),
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
                "mark_price": mark.price,
                "mark_observed_at": mark.observed_at,
                "mark_source": mark.source,
            },
            correlation_id=client_id,
            symbol=aggregate.instrument,
        )
        return True

    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime | None = None,
    ) -> ReconciliationReport:
        report = super().reconcile(
            portfolio_id=portfolio_id,
            instrument=instrument,
        )
        if (
            self.config.reconciliation_mode is not ReconciliationMode.SAFE_AUTO_REPAIR
            or timestamp is None
            or not report.blocked
        ):
            return report
        if not self._repair_unique_owned_orphan(
            report=report,
            portfolio_id=portfolio_id,
            instrument=instrument,
            timestamp=timestamp,
        ):
            return report
        return super().reconcile(
            portfolio_id=portfolio_id,
            instrument=instrument,
        )

    def _repair_unique_owned_orphan(
        self,
        *,
        report: ReconciliationReport,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> bool:
        if len(report.findings) != 1:
            return False
        finding = report.findings[0]
        if (
            finding.category != "venue_order_missing_internally"
            or not finding.automatic_resolution_eligible
            or not finding.blocking
        ):
            return False
        client_order_id = finding.observed.get("client_order_id")
        if not isinstance(client_order_id, str):
            return False
        prefixes = {
            f"{self.config.module_namespace}-",
            f"{self.config.module_namespace[:4]}-",
        }
        if not any(client_order_id.startswith(prefix) for prefix in prefixes):
            return False

        matches = tuple(
            snapshot
            for snapshot in self.venue.pending_snapshots()
            if snapshot.client_order_id == client_order_id
        )
        if len(matches) != 1:
            return False
        snapshot = matches[0]
        aggregate = self._aggregate_from_owned_snapshot(
            snapshot=snapshot,
            portfolio_id=portfolio_id,
            instrument=instrument,
        )
        if aggregate is None or client_order_id in self.orders:
            return False

        self.orders[client_order_id] = aggregate
        try:
            self.journal.append(
                timestamp=timestamp,
                event_type="reconciliation.order_projection_repaired",
                aggregate_id=client_order_id,
                payload={
                    "core_order_id": snapshot.id,
                    "portfolio_id": portfolio_id,
                    "instrument": instrument,
                    "intent_role": aggregate.intent_role,
                    "state": aggregate.state.value,
                    "quantity": aggregate.requested_quantity,
                    "filled_quantity": aggregate.filled_quantity,
                },
                correlation_id=client_order_id,
                symbol=instrument,
            )
        except Exception:
            self.orders.pop(client_order_id, None)
            raise
        return True

    @staticmethod
    def _aggregate_from_owned_snapshot(
        *,
        snapshot: OrderSnapshot,
        portfolio_id: str,
        instrument: str,
    ) -> OrderAggregate | None:
        if snapshot.source_module != _SOURCE_MODULE or snapshot.status.terminal:
            return None
        role = parse_snapshot_tag(
            snapshot.tag,
            portfolio_id=portfolio_id,
            instrument=instrument,
        )
        if role is None:
            return None
        state_by_status = {
            OrderStatus.NEW: PaperOrderState.OPEN,
            OrderStatus.PENDING: PaperOrderState.OPEN,
            OrderStatus.PARTIALLY_FILLED: (PaperOrderState.PARTIALLY_FILLED),
            OrderStatus.CANCEL_PENDING: PaperOrderState.CANCEL_PENDING,
        }
        state = state_by_status.get(snapshot.status)
        if state is None:
            return None
        quantity = Decimal(str(snapshot.quantity))
        filled = Decimal(str(snapshot.filled_quantity))
        fees = Decimal(str(snapshot.fees_paid))
        average = (
            Decimal(str(snapshot.average_fill_price))
            if snapshot.average_fill_price is not None
            else None
        )
        if quantity <= 0 or filled < 0 or filled > quantity or fees < 0:
            return None
        if state is PaperOrderState.PARTIALLY_FILLED:
            if not (Decimal("0") < filled < quantity) or average is None:
                return None
        elif filled != 0 or average is not None:
            return None
        client_order_id = snapshot.client_order_id
        if client_order_id is None:
            return None
        return OrderAggregate(
            client_order_id=client_order_id,
            core_order_id=snapshot.id,
            requested_quantity=quantity,
            state=state,
            filled_quantity=filled,
            average_fill_price=average,
            fees_paid=fees,
            portfolio_id=portfolio_id,
            instrument=instrument,
            intent_role=role.value,
        )
