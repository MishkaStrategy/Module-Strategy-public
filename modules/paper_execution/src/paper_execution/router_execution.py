from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from modular_core import PositionSide

from .contracts import (
    ExecutableTradePlan,
    ExecutionResult,
    StateVersions,
    VenueMetadataSnapshot,
)
from .enums import IntentRole, PaperReasonCode, PlanAction
from .exceptions import PlanValidationError, ReconciliationBlocked
from .protection import (
    DeferredProtectionLeg,
    ProtectionActivationPolicy,
    ProtectionCoordinator,
)
from .reconciliation_policy import LifecycleState
from .router_adapter import (
    RouterAdaptation,
    RouterOCCProvider,
    RouterPlanAdapter,
    RouterPlanEvidence,
)
from .router_position import RouterPositionProvider

_ROUTER_EVENT_TYPE = "router.envelope_received"
_POSITION_ACTIONS = frozenset({"add_to_position", "reduce_position", "close_position"})


def _load_router_evidence(coordinator: Any) -> dict[str, RouterPlanEvidence]:
    evidence_by_plan: dict[str, RouterPlanEvidence] = {}
    for event in coordinator.journal.store.read():
        if event.event_type != _ROUTER_EVENT_TYPE:
            continue
        evidence = RouterPlanEvidence.from_journal_payload(dict(event.payload))
        if (
            event.payload.get("aggregate_id") != evidence.plan_id
            or event.correlation_id != evidence.plan_id
            or event.symbol != evidence.instrument
        ):
            raise ValueError("Router envelope journal identity mismatch")
        existing = evidence_by_plan.get(evidence.plan_id)
        if existing is not None and existing != evidence:
            raise ValueError("conflicting Router envelope journal evidence")
        evidence_by_plan[evidence.plan_id] = evidence
    return evidence_by_plan


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a mapping",
        )
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a non-blank string",
        )
    return value


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a decimal string",
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation as exc:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} is not a valid decimal",
        ) from exc
    if not result.is_finite() or result <= 0:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be positive and finite",
        )
    return result


def _position_contract(
    mapping: Mapping[str, Any],
) -> tuple[str, PositionSide, Decimal]:
    position = _mapping(mapping.get("position_intent"), "position_intent")
    position_id = _identifier(position.get("position_id"), "position_id")
    side_text = _identifier(position.get("current_side"), "current_side")
    side_by_text = {
        "long": PositionSide.LONG,
        "short": PositionSide.SHORT,
    }
    try:
        side = side_by_text[side_text]
    except KeyError as exc:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            "Router current position side is unsupported",
        ) from exc
    quantity = _decimal(position.get("current_quantity"), "current_quantity")
    return position_id, side, quantity


def _add_position_context(
    adaptation: RouterAdaptation,
    *,
    baseline_quantity: Decimal,
    position_id: str,
    side: PositionSide,
) -> RouterAdaptation:
    plan = adaptation.plan
    if plan.action is not PlanAction.ADD_TO_POSITION:
        return adaptation
    entry = next(
        (item for item in plan.intents if item.role is IntentRole.ENTRY),
        None,
    )
    if entry is None or entry.quantity is None:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            "Router add plan is missing an explicit Entry quantity",
        )
    resulting_quantity = baseline_quantity + entry.quantity
    position_metadata = {
        "router_position_id": position_id,
        "router_current_position_side": side.value,
        "router_current_position_quantity": str(baseline_quantity),
        "router_resulting_position_quantity": str(resulting_quantity),
    }
    intents = tuple(
        replace(
            intent,
            quantity=(
                resulting_quantity
                if intent.role in {IntentRole.STOP, IntentRole.TARGET}
                else intent.quantity
            ),
            metadata={**dict(intent.metadata), **position_metadata},
        )
        for intent in plan.intents
    )
    rebuilt = ExecutableTradePlan.create(
        plan_id=plan.plan_id,
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=intents,
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
        input_hashes=plan.input_hashes,
    )
    return RouterAdaptation(evidence=adaptation.evidence, plan=rebuilt)


def _entry_only_plan(plan: ExecutableTradePlan) -> ExecutableTradePlan:
    entry_intents = tuple(intent for intent in plan.intents if intent.role is IntentRole.ENTRY)
    if len(entry_intents) != 1:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            "Router add execution requires exactly one Entry",
        )
    return ExecutableTradePlan.create(
        plan_id=plan.plan_id,
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=entry_intents,
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
        input_hashes=plan.input_hashes,
    )


def _prepare_add_protection(
    coordinator: Any,
    *,
    plan: ExecutableTradePlan,
    metadata: VenueMetadataSnapshot,
    mark_price: Decimal,
    now: datetime,
    baseline_quantity: Decimal,
) -> ProtectionCoordinator:
    entry = next(
        (item for item in plan.intents if item.role is IntentRole.ENTRY),
        None,
    )
    if entry is None or entry.quantity is None:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            "Router add plan is missing Entry quantity",
        )
    entry_client_order_id = coordinator.id_factory.create(
        plan_id=plan.plan_id,
        intent_id=entry.intent_id,
        role=entry.role.value,
    )
    protection = ProtectionCoordinator(
        entry_client_order_id=entry_client_order_id,
        entry_requested_quantity=entry.quantity,
        baseline_position_quantity=baseline_quantity,
    )
    protection_intents = tuple(
        intent for intent in plan.intents if intent.role in {IntentRole.STOP, IntentRole.TARGET}
    )
    stop_count = sum(intent.role is IntentRole.STOP for intent in protection_intents)
    target_count = sum(intent.role is IntentRole.TARGET for intent in protection_intents)
    if stop_count != 1 or target_count > 1:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            "Router add protection requires one Stop and at most one Target",
        )
    for intent in protection_intents:
        client_order_id = coordinator.id_factory.create(
            plan_id=plan.plan_id,
            intent_id=intent.intent_id,
            role=intent.role.value,
        )
        normalized = coordinator.rule_engine.normalize(
            intent=intent,
            metadata=metadata,
            client_order_id=client_order_id,
            reference_price=mark_price,
            event_time=now,
        )
        quantity = normalized.audit.normalized_quantity
        trigger_price = normalized.audit.normalized_price
        expected_quantity = baseline_quantity + entry.quantity
        if quantity != expected_quantity or trigger_price is None:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "Router add protection does not cover the exact resulting position",
            )
        protection.register_leg(
            DeferredProtectionLeg(
                client_order_id=client_order_id,
                role=intent.role,
                planned_quantity=quantity,
                trigger_price=trigger_price,
                activation_policy=(
                    ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL
                    if intent.role is IntentRole.STOP
                    else ProtectionActivationPolicy.AFTER_ENTRY_TERMINAL
                ),
            )
        )
    return protection


def _stage_add_protection(
    coordinator: Any,
    *,
    plan: ExecutableTradePlan,
    protection: ProtectionCoordinator,
) -> None:
    scope = (plan.portfolio_id, plan.instrument)
    try:
        coordinator.orchestration_state.register_protection(protection)
    except Exception as exc:
        coordinator._require_full_reconciliation(scope)
        coordinator._lifecycle = LifecycleState.BLOCKED
        raise ReconciliationBlocked(
            PaperReasonCode.RECONCILIATION_BLOCKED,
            "Router add protection registration failed before Entry submission",
        ) from exc


def _rollback_staged_add_protection(
    coordinator: Any,
    protection: ProtectionCoordinator,
) -> None:
    entry_id = protection.entry_client_order_id
    existing = coordinator.orchestration_state.protection_by_entry.get(entry_id)
    if existing is protection:
        coordinator.orchestration_state.protection_by_entry.pop(entry_id)


def _commit_add_protection_journal(
    coordinator: Any,
    *,
    plan: ExecutableTradePlan,
    protection: ProtectionCoordinator,
    timestamp: datetime,
) -> None:
    scope = (plan.portfolio_id, plan.instrument)
    try:
        coordinator.journal.append(
            timestamp=timestamp,
            event_type="protection.deferred",
            aggregate_id=protection.entry_client_order_id,
            payload={
                "plan_id": plan.plan_id,
                "entry_client_order_id": protection.entry_client_order_id,
                "baseline_position_quantity": protection.baseline_position_quantity,
                "entry_requested_quantity": protection.entry_requested_quantity,
                "resulting_position_quantity": (
                    protection.baseline_position_quantity + protection.entry_requested_quantity
                ),
                "protection_legs": sorted(protection.legs),
            },
            correlation_id=plan.plan_id,
            symbol=plan.instrument,
        )
    except Exception as exc:
        coordinator._require_full_reconciliation(scope)
        coordinator._lifecycle = LifecycleState.BLOCKED
        raise ReconciliationBlocked(
            PaperReasonCode.RECONCILIATION_BLOCKED,
            "Router add Entry was accepted but durable protection record failed",
        ) from exc


class RouterExecutionAdapter:
    """Explicit Router-to-Paper boundary without an alternative plan authority."""

    def __init__(
        self,
        coordinator: Any,
        *,
        occ_provider: RouterOCCProvider,
        position_provider: RouterPositionProvider | None = None,
        plan_adapter: RouterPlanAdapter | None = None,
    ) -> None:
        for name in (
            "accept_plan",
            "config",
            "metadata_provider",
            "journal",
            "_plan_results",
        ):
            if not hasattr(coordinator, name):
                raise TypeError("coordinator does not expose the Paper execution boundary")
        if not isinstance(occ_provider, RouterOCCProvider):
            raise TypeError("occ_provider must implement RouterOCCProvider")
        if position_provider is not None and not isinstance(
            position_provider,
            RouterPositionProvider,
        ):
            raise TypeError("position_provider must implement RouterPositionProvider")
        self.coordinator = coordinator
        self.occ_provider = occ_provider
        self.position_provider = position_provider
        self.plan_adapter = plan_adapter or RouterPlanAdapter()
        self._evidence_by_plan = _load_router_evidence(coordinator)

    def accept(
        self,
        envelope: object,
        *,
        now: datetime,
        current_index: int,
        mark_price: Decimal,
        paper_versions: StateVersions,
    ) -> ExecutionResult:
        evidence, mapping = self.plan_adapter.inspect(envelope, now=now)
        existing_evidence = self._evidence_by_plan.get(evidence.plan_id)
        existing_result = self.coordinator._plan_results.get(evidence.plan_id)

        if existing_evidence is not None:
            if existing_evidence != evidence:
                raise PlanValidationError(
                    PaperReasonCode.PLAN_ID_CONTENT_MISMATCH,
                    "Router plan_id was already used with different envelope evidence",
                )
            if existing_result is not None:
                return cast(ExecutionResult, existing_result)
        elif existing_result is not None:
            raise PlanValidationError(
                PaperReasonCode.PLAN_ID_CONTENT_MISMATCH,
                "plan_id was already accepted outside the Router boundary",
            )

        occ_snapshot = self.occ_provider.snapshot(
            evidence.portfolio_id,
            evidence.instrument,
            now,
        )
        metadata = self.coordinator.metadata_provider.snapshot(
            evidence.instrument,
            now,
        )
        adaptation = self.plan_adapter.adapt(
            envelope,
            now=now,
            paper_versions=paper_versions,
            metadata_version=metadata.metadata_version,
            paper_configuration_hash=self.coordinator.config.canonical_hash,
            occ_snapshot=occ_snapshot,
        )
        if adaptation.evidence != evidence:
            raise PlanValidationError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "Router adaptation evidence changed during validation",
            )

        baseline_quantity: Decimal | None = None
        if evidence.action in _POSITION_ACTIONS:
            if self.position_provider is None:
                raise PlanValidationError(
                    PaperReasonCode.INVALID_CONFIGURATION,
                    "Router position-changing execution requires a factual position provider",
                )
            position_id, side, quantity = _position_contract(mapping)
            position_snapshot = self.position_provider.snapshot(
                evidence.portfolio_id,
                evidence.instrument,
                now,
            )
            position_snapshot.validate_for(
                portfolio_id=evidence.portfolio_id,
                instrument=evidence.instrument,
                position_id=position_id,
                side=side,
                quantity=quantity,
                timestamp=now,
            )
            baseline_quantity = quantity
            adaptation = _add_position_context(
                adaptation,
                baseline_quantity=quantity,
                position_id=position_id,
                side=side,
            )

        add_protection: ProtectionCoordinator | None = None
        execution_plan = adaptation.plan
        if adaptation.plan.action is PlanAction.ADD_TO_POSITION:
            if baseline_quantity is None:
                raise PlanValidationError(
                    PaperReasonCode.INVARIANT_VIOLATION,
                    "Router add baseline was not validated",
                )
            add_protection = _prepare_add_protection(
                self.coordinator,
                plan=adaptation.plan,
                metadata=metadata,
                mark_price=mark_price,
                now=now,
                baseline_quantity=baseline_quantity,
            )
            execution_plan = _entry_only_plan(adaptation.plan)

        staged_add_protection = False
        if add_protection is not None:
            _stage_add_protection(
                self.coordinator,
                plan=adaptation.plan,
                protection=add_protection,
            )
            staged_add_protection = True

        try:
            if existing_evidence is None:
                self.coordinator.journal.append(
                    timestamp=now,
                    event_type=_ROUTER_EVENT_TYPE,
                    aggregate_id=evidence.plan_id,
                    payload=evidence.journal_payload(),
                    correlation_id=evidence.plan_id,
                    symbol=evidence.instrument,
                )
                self._evidence_by_plan[evidence.plan_id] = evidence

            result = cast(
                ExecutionResult,
                self.coordinator.accept_plan(
                    execution_plan,
                    now=now,
                    current_index=current_index,
                    mark_price=mark_price,
                    versions=paper_versions,
                ),
            )
        except Exception:
            if staged_add_protection and add_protection is not None:
                _rollback_staged_add_protection(
                    self.coordinator,
                    add_protection,
                )
            raise

        if not result.accepted:
            if staged_add_protection and add_protection is not None:
                _rollback_staged_add_protection(
                    self.coordinator,
                    add_protection,
                )
            return result

        if add_protection is not None:
            _commit_add_protection_journal(
                self.coordinator,
                plan=adaptation.plan,
                protection=add_protection,
                timestamp=now,
            )
        return result
