from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from .canonical import decimal_text
from .config import RouterConfiguration
from .decimal_math import (
    DecimalDomainError,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
)
from .enums import (
    ActivePlanPolicy,
    ExistingPositionPolicy,
    LongShortConflictPolicy,
    MultiSetupPolicy,
    PendingOrderPolicy,
    PlanVerdict,
    ReasonCode,
    ScalingAction,
    ScalingDirection,
    TradeAction,
    TradeSide,
)
from .errors import PlanInvariantError, RouterConfigurationError, RouterInputError
from .models import (
    AuditEvent,
    CancellationIntent,
    EntryCandidate,
    EntryIntent,
    ExecutableTradePlan,
    ExecutionPrecondition,
    PendingOrderState,
    PortfolioSnapshot,
    PositionIntent,
    PositionState,
    ProtectionDecision,
    ProtectionIntent,
    RejectedCandidate,
    RouterInputPackage,
    RoutingResult,
    ScalingDecision,
    SizingDecision,
    SizingIntent,
)


_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _safe_future_time(value: datetime, seconds: int) -> datetime:
    try:
        return value + timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise PlanInvariantError("plan TTL exceeds supported datetime range") from exc


def _utc_microsecond_rank(value: datetime) -> int:
    delta = value - _UTC_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _inventory_digest(rows: Iterable[tuple[str, str]]) -> str:
    digest = sha256(b"trade-plan-router-input-inventory-v1\0")
    for decision_id, input_hash in rows:
        for value in (decision_id, input_hash):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _inventory_row(rows: tuple[tuple[str, str], ...]) -> dict[str, object]:
    detail: dict[str, object] = {
        "count": len(rows),
        "digest": _inventory_digest(rows),
    }
    if len(rows) <= 256:
        detail["decision_ids"] = tuple(row[0] for row in rows)
    else:
        detail["decision_ids_omitted"] = True
    return detail


def _package_inventory_detail(package: RouterInputPackage) -> dict[str, object]:
    return {
        "correlation_id": package.correlation_id,
        "portfolio_content_hash": package.portfolio.input_hash,
        "entries": _inventory_row(
            tuple((item.candidate_id, item.input_hash) for item in package.entries)
        ),
        "protections": _inventory_row(
            tuple((item.decision_id, item.input_hash) for item in package.protections)
        ),
        "sizings": _inventory_row(
            tuple((item.decision_id, item.input_hash) for item in package.sizings)
        ),
        "scalings": _inventory_row(
            tuple((item.decision_id, item.input_hash) for item in package.scalings)
        ),
    }


def _attach_input_inventory(
    audit: list[AuditEvent],
    package: RouterInputPackage,
) -> None:
    inventory = _package_inventory_detail(package)
    if not audit:
        audit.append(
            AuditEvent(
                sequence=1,
                stage="input_inventory",
                outcome="recorded",
                detail={"input_inventory": inventory},
            )
        )
        return
    last = audit[-1]
    if "input_inventory" in last.detail:
        return
    audit[-1] = replace(
        last,
        detail={**dict(last.detail), "input_inventory": inventory},
    )


def _coalesce_rejected_candidates(
    values: Iterable[RejectedCandidate],
) -> tuple[RejectedCandidate, ...]:
    """Return one deterministic rejection record per logical candidate ID.

    Duplicate delivery can legitimately generate several validation records for
    one candidate.  The public plan/result schema requires candidate IDs to be
    unique, while the full per-delivery explanation remains available in the
    audit trace.
    """

    grouped: dict[str, list[RejectedCandidate]] = {}
    for value in values:
        grouped.setdefault(value.candidate_id, []).append(value)

    merged: list[RejectedCandidate] = []
    for candidate_id in sorted(grouped):
        rows = sorted(
            grouped[candidate_id],
            key=lambda item: (
                item.setup_id,
                tuple(reason.value for reason in item.reason_codes),
                item.detail,
            ),
        )
        setup_ids = {item.setup_id for item in rows}
        details = {item.detail for item in rows if item.detail}
        reasons = tuple(
            sorted(
                {reason for item in rows for reason in item.reason_codes},
                key=lambda item: item.value,
            )
        )
        if len(details) == 1:
            detail = next(iter(details))
        elif details:
            detail = f"{len(details)} deterministic rejection details merged; see audit trace"
        else:
            detail = ""
        merged.append(
            RejectedCandidate(
                candidate_id=candidate_id,
                setup_id=(next(iter(setup_ids)) if len(setup_ids) == 1 else "ambiguous"),
                reason_codes=reasons,
                detail=detail,
            )
        )
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    entry: EntryCandidate
    protection: ProtectionDecision
    sizing: SizingDecision
    scaling: ScalingDecision | None


@dataclass(frozen=True, slots=True)
class RiskReductionBundle:
    scaling: ScalingDecision
    position: PositionState
    strategy_id: str
    setup_id: str
    market: str
    selected_candidate_id: str
    input_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioEvaluation:
    action: TradeAction
    cancellations: tuple[CancellationIntent, ...]
    warnings: tuple[str, ...]
    target_position: PositionState | None = None
    sequencing_rules: tuple[str, ...] = ()
    risk_reducing: bool = False


class DecisionFreshnessValidator:
    def __init__(self, config: RouterConfiguration) -> None:
        self._config = config

    def entry_reason(self, entry: EntryCandidate, package: RouterInputPackage) -> ReasonCode | None:
        as_of = package.as_of
        if entry.signal_time > as_of or entry.available_at > as_of or entry.created_at > as_of:
            return ReasonCode.FUTURE_DECISION
        if entry.expires_at <= as_of:
            return ReasonCode.STALE_DECISION
        if as_of - entry.created_at > timedelta(seconds=self._config.max_decision_age_seconds):
            return ReasonCode.STALE_DECISION
        return None

    def linked_reason(
        self,
        created_at: object,
        expires_at: object,
        package: RouterInputPackage,
    ) -> ReasonCode | None:
        as_of = package.as_of
        if not hasattr(created_at, "tzinfo"):
            return ReasonCode.INVALID_INPUT
        if created_at > as_of:  # type: ignore[operator]
            return ReasonCode.FUTURE_DECISION
        if expires_at is not None and expires_at < as_of:  # type: ignore[operator]
            return ReasonCode.STALE_DECISION
        if as_of - created_at > timedelta(  # type: ignore[operator]
            seconds=self._config.max_decision_age_seconds
        ):
            return ReasonCode.STALE_DECISION
        return None

    def portfolio_reason(self, snapshot: PortfolioSnapshot, as_of: object) -> ReasonCode | None:
        if snapshot.timestamp > as_of:  # type: ignore[operator]
            return ReasonCode.FUTURE_DECISION
        if as_of - snapshot.timestamp > timedelta(  # type: ignore[operator]
            seconds=self._config.max_snapshot_age_seconds
        ):
            return ReasonCode.PORTFOLIO_STATE_STALE
        return None


class DecisionValidator:
    def __init__(self, config: RouterConfiguration) -> None:
        self._freshness = DecisionFreshnessValidator(config)

    def collect(
        self,
        package: RouterInputPackage,
    ) -> tuple[list[CandidateBundle], list[RejectedCandidate], list[AuditEvent]]:
        accepted: list[CandidateBundle] = []
        rejected: list[RejectedCandidate] = []
        audit: list[AuditEvent] = []
        duplicate_ids = self._duplicates(item.candidate_id for item in package.entries)
        duplicate_protection_ids = self._duplicates(
            item.decision_id for item in package.protections
        )
        duplicate_sizing_ids = self._duplicates(item.decision_id for item in package.sizings)
        duplicate_scaling_ids = self._duplicates(item.decision_id for item in package.scalings)
        entry_ids = {item.candidate_id for item in package.entries}
        entry_setup_symbols = {(item.setup_id, item.symbol) for item in package.entries}
        entry_scaling_links = {(item.symbol, item.side, item.setup_id) for item in package.entries}
        entry_symbol_sides = {(item.symbol, item.side) for item in package.entries}
        orphan_rows: list[tuple[str, str, str, str]] = []
        for protection in package.protections:
            if protection.entry_candidate_id not in entry_ids:
                orphan_rows.append(
                    (
                        "protection",
                        protection.decision_id,
                        protection.setup_id,
                        "protection decision is not linked to an entry candidate",
                    )
                )
        for sizing in package.sizings:
            if sizing.entry_candidate_id is not None:
                orphan = sizing.entry_candidate_id not in entry_ids
            else:
                orphan = (sizing.setup_id, sizing.symbol) not in entry_setup_symbols
            if orphan:
                orphan_rows.append(
                    (
                        "sizing",
                        sizing.decision_id,
                        sizing.setup_id,
                        "sizing decision is not linked to an entry candidate",
                    )
                )
        for scaling in package.scalings:
            if scaling.setup_id is None:
                orphan = (scaling.symbol, scaling.side) not in entry_symbol_sides
            else:
                orphan = (scaling.symbol, scaling.side, scaling.setup_id) not in entry_scaling_links
            if orphan:
                orphan_rows.append(
                    (
                        "scaling",
                        scaling.decision_id,
                        scaling.setup_id or "unlinked",
                        "scaling decision is not linked to an entry candidate",
                    )
                )
        orphan_setup_ids: dict[tuple[str, str], set[str]] = {}
        orphan_details: dict[tuple[str, str], set[str]] = {}
        orphan_counts: dict[tuple[str, str], int] = {}
        for decision_type, decision_id, setup_id, detail in orphan_rows:
            key = (decision_type, decision_id)
            orphan_setup_ids.setdefault(key, set()).add(setup_id)
            orphan_details.setdefault(key, set()).add(detail)
            orphan_counts[key] = orphan_counts.get(key, 0) + 1
        duplicate_ids_by_type = {
            "protection": duplicate_protection_ids,
            "sizing": duplicate_sizing_ids,
            "scaling": duplicate_scaling_ids,
        }
        for decision_type, decision_id in sorted(orphan_counts):
            key = (decision_type, decision_id)
            setup_ids = sorted(orphan_setup_ids[key])
            orphan_detail_values = sorted(orphan_details[key])
            delivery_count = orphan_counts[key]
            is_duplicate = delivery_count > 1 or decision_id in duplicate_ids_by_type[decision_type]
            orphan_reasons = (
                (ReasonCode.DUPLICATE_DECISION, ReasonCode.INCOMPATIBLE_DECISIONS)
                if is_duplicate
                else (ReasonCode.INCOMPATIBLE_DECISIONS,)
            )
            detail = "; ".join(orphan_detail_values)
            if is_duplicate:
                detail += f"; duplicate decision_id delivered {delivery_count} times"
            setup_id = setup_ids[0] if len(setup_ids) == 1 else "unlinked"
            rejection_id = f"{decision_type}:{decision_id}"
            rejected.append(
                RejectedCandidate(
                    candidate_id=rejection_id,
                    setup_id=setup_id,
                    reason_codes=orphan_reasons,
                    detail=detail,
                )
            )
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="orphan_decision_validation",
                    outcome="rejected",
                    reason_code=orphan_reasons[0],
                    candidate_id=rejection_id,
                    detail={
                        "decision_type": decision_type,
                        "decision_id": decision_id,
                        "detail": detail,
                        "delivery_count": delivery_count,
                    },
                )
            )
        protections_by_candidate: dict[str, list[ProtectionDecision]] = {}
        sizings_by_candidate: dict[str, list[SizingDecision]] = {}
        sizing_fallback_by_setup: dict[tuple[str, str], list[SizingDecision]] = {}
        scalings_by_setup: dict[tuple[str, TradeSide, str], list[ScalingDecision]] = {}
        global_scalings: dict[tuple[str, TradeSide], list[ScalingDecision]] = {}
        for protection in package.protections:
            protections_by_candidate.setdefault(protection.entry_candidate_id, []).append(
                protection
            )
        for sizing in package.sizings:
            if sizing.entry_candidate_id is None:
                sizing_fallback_by_setup.setdefault((sizing.setup_id, sizing.symbol), []).append(
                    sizing
                )
            else:
                sizings_by_candidate.setdefault(sizing.entry_candidate_id, []).append(sizing)
        for scaling in package.scalings:
            if scaling.setup_id is None:
                global_scalings.setdefault((scaling.symbol, scaling.side), []).append(scaling)
            else:
                scalings_by_setup.setdefault(
                    (scaling.symbol, scaling.side, scaling.setup_id), []
                ).append(scaling)
        for index in (
            protections_by_candidate,
            sizings_by_candidate,
            sizing_fallback_by_setup,
            scalings_by_setup,
            global_scalings,
        ):
            for rows in index.values():
                rows.sort(key=lambda item: (item.created_at, item.decision_id))
        sequence = len(audit) + 1
        for entry in package.entries:
            reasons: list[ReasonCode] = []
            details: list[str] = []
            if entry.candidate_id in duplicate_ids:
                reasons.append(ReasonCode.DUPLICATE_DECISION)
                details.append("duplicate candidate_id")
            if entry.portfolio_id != package.portfolio.portfolio_id:
                reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                details.append("portfolio ownership mismatch")
            freshness = self._freshness.entry_reason(entry, package)
            if freshness is not None:
                reasons.append(freshness)
                details.append("entry time validity failed")

            protections = protections_by_candidate.get(entry.candidate_id, [])
            sizings = sizings_by_candidate.get(entry.candidate_id, [])
            if not sizings:
                sizings = sizing_fallback_by_setup.get((entry.setup_id, entry.symbol), [])
            scalings = sorted(
                (
                    *global_scalings.get((entry.symbol, entry.side), []),
                    *scalings_by_setup.get((entry.symbol, entry.side, entry.setup_id), []),
                ),
                key=lambda item: (item.created_at, item.decision_id),
            )
            if any(item.decision_id in duplicate_protection_ids for item in protections):
                reasons.append(ReasonCode.DUPLICATE_DECISION)
                details.append("duplicate protection decision_id")
            if any(item.decision_id in duplicate_sizing_ids for item in sizings):
                reasons.append(ReasonCode.DUPLICATE_DECISION)
                details.append("duplicate sizing decision_id")
            if any(item.decision_id in duplicate_scaling_ids for item in scalings):
                reasons.append(ReasonCode.DUPLICATE_DECISION)
                details.append("duplicate scaling decision_id")
            if not protections:
                reasons.append(ReasonCode.MISSING_PROTECTION)
                details.append("no linked protection decision")
            elif len(protections) > 1:
                reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                details.append("multiple linked protection decisions")
            if not sizings:
                reasons.append(ReasonCode.MISSING_SIZING)
                details.append("no linked sizing decision")
            elif len(sizings) > 1:
                reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                details.append("multiple linked sizing decisions")
            if len(scalings) > 1:
                reasons.append(ReasonCode.INVALID_SCALING)
                details.append("multiple linked scaling approvals")

            selected_protection = protections[0] if len(protections) == 1 else None
            selected_sizing = sizings[0] if len(sizings) == 1 else None
            selected_scaling = scalings[0] if len(scalings) == 1 else None
            if selected_protection is not None:
                if (
                    selected_protection.symbol != entry.symbol
                    or selected_protection.setup_id != entry.setup_id
                ):
                    reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                    details.append("protection ownership mismatch")
                if selected_protection.side is not entry.side:
                    reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                    details.append("protection side mismatch")
                linked = self._freshness.linked_reason(
                    selected_protection.created_at,
                    selected_protection.expires_at,
                    package,
                )
                if linked is not None:
                    reasons.append(linked)
                    details.append("protection time validity failed")
                reference = entry.reference_price
                if reference is not None and reference != selected_protection.entry_price:
                    reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                    details.append("entry/protection price mismatch")
            if selected_sizing is not None:
                if (
                    selected_sizing.symbol != entry.symbol
                    or selected_sizing.setup_id != entry.setup_id
                ):
                    reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                    details.append("sizing ownership mismatch")
                if not selected_sizing.approved:
                    reasons.append(ReasonCode.SIZING_REJECTED)
                    details.append(selected_sizing.rejection_reason or "sizing rejected")
                linked = self._freshness.linked_reason(
                    selected_sizing.created_at,
                    selected_sizing.expires_at,
                    package,
                )
                if linked is not None:
                    reasons.append(linked)
                    details.append("sizing time validity failed")
                if selected_sizing.entry_candidate_id not in {
                    None,
                    entry.candidate_id,
                }:
                    reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                    details.append("sizing entry link mismatch")
                if (
                    selected_protection is not None
                    and selected_sizing.protection_decision_id
                    not in {None, selected_protection.decision_id}
                ):
                    reasons.append(ReasonCode.INCOMPATIBLE_DECISIONS)
                    details.append("sizing protection link mismatch")
            if selected_scaling is not None:
                linked = self._freshness.linked_reason(
                    selected_scaling.created_at,
                    selected_scaling.expires_at,
                    package,
                )
                if linked is not None:
                    reasons.append(linked)
                    details.append("scaling time validity failed")
                if (
                    selected_scaling.symbol != entry.symbol
                    or selected_scaling.side is not entry.side
                ):
                    reasons.append(ReasonCode.INVALID_SCALING)
                    details.append("scaling ownership mismatch")
                if selected_scaling.setup_id not in {None, entry.setup_id}:
                    reasons.append(ReasonCode.INVALID_SCALING)
                    details.append("scaling setup mismatch")

            unique_reasons = tuple(sorted(set(reasons), key=lambda item: item.value))
            if unique_reasons:
                rejected.append(
                    RejectedCandidate(
                        candidate_id=entry.candidate_id,
                        setup_id=entry.setup_id,
                        reason_codes=unique_reasons,
                        detail="; ".join(sorted(set(details))),
                    )
                )
                audit.append(
                    AuditEvent(
                        sequence=sequence,
                        stage="decision_validation",
                        outcome="rejected",
                        reason_code=unique_reasons[0],
                        candidate_id=entry.candidate_id,
                        detail={"reasons": tuple(item.value for item in unique_reasons)},
                    )
                )
            else:
                if selected_protection is None or selected_sizing is None:
                    raise PlanInvariantError(
                        "validated candidate is missing required linked decisions"
                    )
                accepted.append(
                    CandidateBundle(
                        entry,
                        selected_protection,
                        selected_sizing,
                        selected_scaling,
                    )
                )
                audit.append(
                    AuditEvent(
                        sequence=sequence,
                        stage="decision_validation",
                        outcome="accepted",
                        candidate_id=entry.candidate_id,
                        detail={"setup_id": entry.setup_id},
                    )
                )
            sequence += 1
        return accepted, rejected, audit

    @staticmethod
    def _duplicates(values: Iterable[str]) -> set[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        return duplicates


class ConflictResolver:
    def __init__(self, config: RouterConfiguration) -> None:
        self._config = config

    def resolve(
        self,
        bundles: list[CandidateBundle],
        portfolio_id: str,
        rejected: list[RejectedCandidate],
        audit: list[AuditEvent],
    ) -> CandidateBundle | None:
        if not bundles:
            return None
        sides = {item.entry.side for item in bundles}
        if len(sides) > 1 and self._config.long_short_policy is LongShortConflictPolicy.REJECT_ALL:
            for item in bundles:
                rejected.append(
                    RejectedCandidate(
                        item.entry.candidate_id,
                        item.entry.setup_id,
                        (ReasonCode.LONG_SHORT_CONFLICT,),
                        "long/short conflict policy rejects all candidates",
                    )
                )
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="conflict_resolution",
                    outcome="rejected",
                    reason_code=ReasonCode.LONG_SHORT_CONFLICT,
                    detail={"policy": self._config.long_short_policy.value},
                )
            )
            return None
        if (
            len(bundles) > 1
            and len(sides) == 1
            and self._config.multi_setup_policy is MultiSetupPolicy.REJECT
        ):
            for item in bundles:
                rejected.append(
                    RejectedCandidate(
                        item.entry.candidate_id,
                        item.entry.setup_id,
                        (ReasonCode.SETUP_CONFLICT,),
                        "multi-setup policy rejects ambiguity",
                    )
                )
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="conflict_resolution",
                    outcome="rejected",
                    reason_code=ReasonCode.SETUP_CONFLICT,
                    detail={"policy": self._config.multi_setup_policy.value},
                )
            )
            return None

        ordered = sorted(bundles, key=lambda item: self._sort_key(item.entry, portfolio_id))
        if len(sides) > 1:
            if self._config.long_short_policy is LongShortConflictPolicy.EARLIEST_SIGNAL:
                ordered = sorted(
                    bundles,
                    key=lambda item: (
                        item.entry.available_at,
                        *self._sort_key(item.entry, portfolio_id),
                    ),
                )
            elif self._config.long_short_policy is LongShortConflictPolicy.FRESHEST_SIGNAL:
                ordered = sorted(
                    bundles,
                    key=lambda item: (
                        -_utc_microsecond_rank(item.entry.available_at),
                        *self._sort_key(item.entry, portfolio_id),
                    ),
                )
        winner = ordered[0]
        for loser in ordered[1:]:
            reason = (
                ReasonCode.LONG_SHORT_CONFLICT
                if loser.entry.side is not winner.entry.side
                else ReasonCode.SETUP_CONFLICT
            )
            rejected.append(
                RejectedCandidate(
                    loser.entry.candidate_id,
                    loser.entry.setup_id,
                    (reason,),
                    f"candidate lost deterministic priority to {winner.entry.candidate_id}",
                )
            )
        audit.append(
            AuditEvent(
                sequence=len(audit) + 1,
                stage="conflict_resolution",
                outcome="selected",
                candidate_id=winner.entry.candidate_id,
                detail={
                    "long_short_policy": self._config.long_short_policy.value,
                    "multi_setup_policy": self._config.multi_setup_policy.value,
                    "ordered_candidates": tuple(item.entry.candidate_id for item in ordered),
                },
            )
        )
        return winner

    def _sort_key(self, entry: EntryCandidate, portfolio_id: str) -> tuple[object, ...]:
        policy = self._config.priority_for(
            portfolio_id=portfolio_id,
            strategy_id=entry.strategy_id,
            setup_id=entry.setup_id,
            module_id=entry.module_id,
            side=entry.side,
        )
        return (
            -policy[0],
            -policy[1],
            -policy[2],
            -policy[3],
            -policy[4],
            -entry.priority,
            entry.available_at,
            entry.created_at,
            entry.setup_id,
            entry.candidate_id,
        )


class PortfolioConstraintEvaluator:
    def __init__(self, config: RouterConfiguration) -> None:
        self._config = config
        self._freshness = DecisionFreshnessValidator(config)

    def evaluate(
        self,
        bundle: CandidateBundle,
        package: RouterInputPackage,
    ) -> tuple[PortfolioEvaluation | None, ReasonCode | None, str]:
        snapshot = package.portfolio
        stale = self._freshness.portfolio_reason(snapshot, package.as_of)
        if stale is not None:
            return None, stale, "portfolio snapshot time validity failed"

        positions = snapshot.positions_for(bundle.entry.symbol)
        action = TradeAction.OPEN_POSITION
        warnings: list[str] = []
        target_position: PositionState | None = None
        sequencing_rules: tuple[str, ...] = ()
        risk_reducing = bool(
            bundle.scaling is not None and bundle.scaling.direction is ScalingDirection.DECREASE
        )

        blockers = (
            (snapshot.circuit_breaker_active, ReasonCode.CIRCUIT_BREAKER_ACTIVE, "circuit_breaker"),
            (snapshot.emergency_state, ReasonCode.EMERGENCY_STATE_ACTIVE, "emergency_state"),
            (snapshot.portfolio_locked, ReasonCode.PORTFOLIO_LOCKED, "portfolio_lock"),
            (
                snapshot.daily_loss_limit_active,
                ReasonCode.DAILY_LOSS_LIMIT_ACTIVE,
                "daily_loss_limit",
            ),
            (snapshot.drawdown_limit_active, ReasonCode.DRAWDOWN_LIMIT_ACTIVE, "drawdown_limit"),
            (
                bundle.entry.symbol in snapshot.instrument_locks,
                ReasonCode.INSTRUMENT_LOCKED,
                "instrument_lock",
            ),
            (
                bundle.entry.strategy_id in snapshot.strategy_locks,
                ReasonCode.STRATEGY_LOCKED,
                "strategy_lock",
            ),
        )
        for active, reason, label in blockers:
            if not active:
                continue
            if risk_reducing and self._config.allow_risk_reducing_when_locked:
                warnings.append(f"risk_reducing_override:{label}")
                continue
            return None, reason, f"{label.replace('_', ' ')} is active"

        if bundle.scaling is not None:
            scaling_position = next(
                (item for item in positions if item.position_id == bundle.scaling.position_id), None
            )
            if scaling_position is None:
                return None, ReasonCode.INVALID_SCALING, "scaling position does not exist"
            if scaling_position.side is not bundle.entry.side:
                return None, ReasonCode.INVALID_SCALING, "scaling side conflicts with position"
            target_position = scaling_position
            if bundle.scaling.action is ScalingAction.ADD:
                if (
                    self._config.existing_position_policy
                    is not ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD
                ):
                    return None, ReasonCode.POSITION_CONFLICT, "same-side add is disabled"
                action = TradeAction.ADD_TO_POSITION
            elif bundle.scaling.action is ScalingAction.REDUCE:
                if bundle.scaling.approved_quantity > scaling_position.quantity:
                    return None, ReasonCode.INVALID_SCALING, "scale-out exceeds position quantity"
                action = (
                    TradeAction.CLOSE_POSITION
                    if bundle.scaling.approved_quantity == scaling_position.quantity
                    else TradeAction.REDUCE_POSITION
                )
            elif bundle.scaling.action is ScalingAction.REBALANCE:
                if bundle.scaling.direction is ScalingDirection.INCREASE:
                    if (
                        self._config.existing_position_policy
                        is not ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD
                    ):
                        return (
                            None,
                            ReasonCode.POSITION_CONFLICT,
                            "rebalance increase is disabled",
                        )
                    action = TradeAction.ADD_TO_POSITION
                    warnings.append("rebalance_increase")
                elif bundle.scaling.direction is ScalingDirection.DECREASE:
                    if bundle.scaling.approved_quantity > scaling_position.quantity:
                        return (
                            None,
                            ReasonCode.INVALID_SCALING,
                            "rebalance reduction exceeds position quantity",
                        )
                    action = (
                        TradeAction.CLOSE_POSITION
                        if bundle.scaling.approved_quantity == scaling_position.quantity
                        else TradeAction.REDUCE_POSITION
                    )
                    warnings.append("rebalance_decrease")
                else:
                    return (
                        None,
                        ReasonCode.INVALID_SCALING,
                        "rebalance direction is not executable",
                    )
            else:
                return None, ReasonCode.INVALID_SCALING, "scaling action is not executable"
        elif positions:
            same = [item for item in positions if item.side is bundle.entry.side]
            opposite = [item for item in positions if item.side is bundle.entry.side.opposite]
            if same and opposite:
                if self._config.existing_position_policy is ExistingPositionPolicy.HEDGE:
                    action = TradeAction.OPEN_POSITION
                    warnings.append("hedge_mode_required")
                else:
                    return (
                        None,
                        ReasonCode.POSITION_CONFLICT,
                        "both same-side and opposite positions exist",
                    )
            elif same:
                if len(same) != 1:
                    return (
                        None,
                        ReasonCode.POSITION_CONFLICT,
                        "multiple same-side positions require explicit scaling decision",
                    )
                if (
                    self._config.existing_position_policy
                    is ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD
                ):
                    action = TradeAction.ADD_TO_POSITION
                    target_position = same[0]
                else:
                    return None, ReasonCode.POSITION_CONFLICT, "same-side position already exists"
            elif opposite:
                if len(opposite) != 1:
                    return (
                        None,
                        ReasonCode.POSITION_CONFLICT,
                        "multiple opposite positions make reversal ambiguous",
                    )
                if (
                    self._config.existing_position_policy
                    is ExistingPositionPolicy.CLOSE_THEN_REVERSE
                ):
                    action = TradeAction.REVERSE_POSITION
                    target_position = opposite[0]
                    sequencing_rules = (
                        "close_target_position_before_open",
                        "open_only_after_flat_confirmation",
                    )
                elif self._config.existing_position_policy is ExistingPositionPolicy.HEDGE:
                    action = TradeAction.OPEN_POSITION
                    warnings.append("hedge_mode_required")
                else:
                    return None, ReasonCode.POSITION_CONFLICT, "opposite position already exists"

        if action not in {TradeAction.REDUCE_POSITION, TradeAction.CLOSE_POSITION}:
            if snapshot.active_plan_ids:
                if self._config.active_plan_policy is ActivePlanPolicy.REJECT_NEW_EXPOSURE:
                    return (
                        None,
                        ReasonCode.ACTIVE_PLAN_CONFLICT,
                        "portfolio has active trade plans without routable ownership metadata",
                    )
                warnings.append("multiple_active_plans_allowed")
            if bundle.sizing.risk_amount > snapshot.available_risk_budget:
                return (
                    None,
                    ReasonCode.RISK_LIMIT_EXCEEDED,
                    "sizing risk exceeds available risk budget",
                )
            if bundle.sizing.risk_fraction > self._config.max_portfolio_risk_fraction:
                return (
                    None,
                    ReasonCode.RISK_LIMIT_EXCEEDED,
                    "sizing risk fraction exceeds Router cap",
                )
            try:
                symbol_cap = exact_multiply(
                    snapshot.equity,
                    self._config.max_symbol_notional_fraction,
                )
                existing_notional = exact_sum(
                    (
                        exact_multiply(position.quantity, position.average_entry)
                        for position in snapshot.positions_for(bundle.entry.symbol)
                    ),
                    Decimal("0"),
                )
                if action is TradeAction.REVERSE_POSITION and target_position is not None:
                    # Reversal sequencing closes the explicit target before opening the new side.
                    existing_notional = exact_subtract(
                        existing_notional,
                        exact_multiply(
                            target_position.quantity,
                            target_position.average_entry,
                        ),
                    )
                incremental_notional = bundle.sizing.notional
                if (
                    bundle.scaling is not None
                    and bundle.scaling.direction is ScalingDirection.INCREASE
                ):
                    incremental_notional = exact_multiply(
                        bundle.scaling.approved_quantity,
                        bundle.scaling.desired_price,
                    )
                projected_notional = exact_add(
                    existing_notional,
                    incremental_notional,
                )
            except DecimalDomainError as exc:
                return None, ReasonCode.INVALID_INPUT, str(exc)
            if projected_notional > symbol_cap:
                return (
                    None,
                    ReasonCode.EXPOSURE_LIMIT_EXCEEDED,
                    "projected symbol notional exceeds Router cap",
                )

        elif snapshot.active_plan_ids:
            warnings.append("active_plan_risk_reduction_override")

        cancellations: list[CancellationIntent] = []
        for order in snapshot.pending_for(bundle.entry.symbol):
            if order.expires_at is not None and order.expires_at <= package.as_of:
                continue
            same_setup = order.setup_id == bundle.entry.setup_id
            if order.status == "cancel_pending":
                return (
                    None,
                    ReasonCode.PENDING_ORDER_CONFLICT,
                    "pending order cancellation is already in progress",
                )
            if order.side is bundle.entry.side:
                if (
                    same_setup
                    and self._config.pending_order_policy is PendingOrderPolicy.REPLACE_SAME_SETUP
                ):
                    cancellations.append(self._cancel(order, "replace_same_setup"))
                    if action is TradeAction.OPEN_POSITION:
                        action = TradeAction.REPLACE_PENDING_PLAN
                else:
                    return (
                        None,
                        ReasonCode.PENDING_ORDER_CONFLICT,
                        "active same-side order conflicts",
                    )
            else:
                if self._config.pending_order_policy is PendingOrderPolicy.CANCEL_OPPOSITE:
                    cancellations.append(self._cancel(order, "cancel_opposite"))
                    warnings.append("opposite_pending_order_cancel_required")
                else:
                    return (
                        None,
                        ReasonCode.PENDING_ORDER_CONFLICT,
                        "active opposite order conflicts",
                    )
        return (
            PortfolioEvaluation(
                action=action,
                cancellations=tuple(sorted(cancellations, key=lambda item: item.order_intent_id)),
                warnings=tuple(sorted(set(warnings))),
                target_position=target_position,
                sequencing_rules=sequencing_rules,
                risk_reducing=risk_reducing,
            ),
            None,
            "",
        )

    def evaluate_risk_reduction(
        self,
        bundle: RiskReductionBundle,
        package: RouterInputPackage,
    ) -> tuple[PortfolioEvaluation | None, ReasonCode | None, str]:
        snapshot = package.portfolio
        stale = self._freshness.portfolio_reason(snapshot, package.as_of)
        if stale is not None:
            return None, stale, "portfolio snapshot time validity failed"

        scaling = bundle.scaling
        position = bundle.position
        warnings: list[str] = []
        blockers = (
            (snapshot.circuit_breaker_active, ReasonCode.CIRCUIT_BREAKER_ACTIVE, "circuit_breaker"),
            (snapshot.emergency_state, ReasonCode.EMERGENCY_STATE_ACTIVE, "emergency_state"),
            (snapshot.portfolio_locked, ReasonCode.PORTFOLIO_LOCKED, "portfolio_lock"),
            (
                snapshot.daily_loss_limit_active,
                ReasonCode.DAILY_LOSS_LIMIT_ACTIVE,
                "daily_loss_limit",
            ),
            (snapshot.drawdown_limit_active, ReasonCode.DRAWDOWN_LIMIT_ACTIVE, "drawdown_limit"),
            (
                scaling.symbol in snapshot.instrument_locks,
                ReasonCode.INSTRUMENT_LOCKED,
                "instrument_lock",
            ),
            (
                bundle.strategy_id in snapshot.strategy_locks,
                ReasonCode.STRATEGY_LOCKED,
                "strategy_lock",
            ),
        )
        for active, reason, label in blockers:
            if not active:
                continue
            if self._config.allow_risk_reducing_when_locked:
                warnings.append(f"risk_reducing_override:{label}")
                continue
            return None, reason, f"{label.replace('_', ' ')} is active"

        if scaling.approved_quantity > position.quantity:
            return None, ReasonCode.INVALID_SCALING, "scale-out exceeds position quantity"
        if snapshot.active_plan_ids:
            warnings.append("active_plan_risk_reduction_override")
        cancellations: list[CancellationIntent] = []
        for order in snapshot.pending_for(scaling.symbol):
            if order.expires_at is not None and order.expires_at <= package.as_of:
                continue
            if order.status == "cancel_pending":
                return (
                    None,
                    ReasonCode.PENDING_ORDER_CONFLICT,
                    "pending order cancellation is already in progress",
                )
            if (
                self._config.pending_order_policy is PendingOrderPolicy.REPLACE_SAME_SETUP
                and order.setup_id == bundle.setup_id
            ):
                cancellations.append(self._cancel(order, "replace_same_setup"))
                continue
            return (
                None,
                ReasonCode.PENDING_ORDER_CONFLICT,
                "standalone risk reduction has an unreconciled active symbol order",
            )
        action = (
            TradeAction.CLOSE_POSITION
            if scaling.approved_quantity == position.quantity
            else TradeAction.REDUCE_POSITION
        )
        if scaling.action is ScalingAction.REBALANCE:
            warnings.append("rebalance_decrease")
        return (
            PortfolioEvaluation(
                action=action,
                cancellations=tuple(sorted(cancellations, key=lambda item: item.order_intent_id)),
                warnings=tuple(sorted(set(warnings))),
                target_position=position,
                sequencing_rules=("execute_reduce_only_against_target_position",),
                risk_reducing=True,
            ),
            None,
            "",
        )

    @staticmethod
    def _cancel(order: PendingOrderState, reason: str) -> CancellationIntent:
        return CancellationIntent(
            order_intent_id=order.order_intent_id,
            expected_order_version=order.version,
            reason=reason,
        )


class TradePlanComposer:
    MODULE_VERSION = "0.1.0"

    def __init__(self, config: RouterConfiguration) -> None:
        self._config = config

    def compose(
        self,
        bundle: CandidateBundle,
        evaluation: PortfolioEvaluation,
        package: RouterInputPackage,
        rejected: list[RejectedCandidate],
        audit: list[AuditEvent],
    ) -> ExecutableTradePlan:
        expiry_candidates = [
            bundle.entry.expires_at,
            _safe_future_time(package.as_of, self._config.plan_ttl_seconds),
        ]
        if (
            bundle.protection.expires_at is not None
            and bundle.protection.expires_at > package.as_of
        ):
            expiry_candidates.append(bundle.protection.expires_at)
        if bundle.sizing.expires_at is not None and bundle.sizing.expires_at > package.as_of:
            expiry_candidates.append(bundle.sizing.expires_at)
        if (
            bundle.scaling is not None
            and bundle.scaling.expires_at is not None
            and bundle.scaling.expires_at > package.as_of
        ):
            expiry_candidates.append(bundle.scaling.expires_at)
        expires_at = min(expiry_candidates)
        quantity = (
            bundle.scaling.approved_quantity
            if bundle.scaling is not None
            and bundle.scaling.action
            in {ScalingAction.ADD, ScalingAction.REDUCE, ScalingAction.REBALANCE}
            else bundle.sizing.quantity
        )
        input_hashes = [
            bundle.entry.input_hash,
            bundle.protection.input_hash,
            bundle.sizing.input_hash,
            package.portfolio.input_hash,
        ]
        if bundle.scaling is not None:
            input_hashes.append(bundle.scaling.input_hash)
        precondition_rows = [
            ExecutionPrecondition("portfolio_version", package.portfolio.version),
            ExecutionPrecondition("portfolio_content_hash", package.portfolio.input_hash),
            ExecutionPrecondition(
                "position_state_version", package.portfolio.position_state_version
            ),
            ExecutionPrecondition("order_state_version", package.portfolio.order_state_version),
            ExecutionPrecondition(
                "risk_reservation_version", package.portfolio.risk_reservation_version
            ),
            ExecutionPrecondition("configuration_hash", self._config.semantic_fingerprint),
            ExecutionPrecondition("candidate_not_expired", utc_marker(bundle.entry.expires_at)),
        ]
        position_intent = None
        if evaluation.target_position is not None:
            position_intent = PositionIntent(
                position_id=evaluation.target_position.position_id,
                expected_position_version=evaluation.target_position.version,
                current_side=evaluation.target_position.side,
                current_quantity=evaluation.target_position.quantity,
            )
            precondition_rows.extend(
                (
                    ExecutionPrecondition(
                        "target_position_id", evaluation.target_position.position_id
                    ),
                    ExecutionPrecondition(
                        "target_position_version", evaluation.target_position.version
                    ),
                )
            )
        preconditions = tuple(precondition_rows)
        audit.append(
            AuditEvent(
                sequence=len(audit) + 1,
                stage="portfolio_constraints",
                outcome="accepted",
                candidate_id=bundle.entry.candidate_id,
                detail={
                    "action": evaluation.action.value,
                    "cancellations": tuple(
                        item.order_intent_id for item in evaluation.cancellations
                    ),
                },
            )
        )
        audit.append(
            AuditEvent(
                sequence=len(audit) + 1,
                stage="plan_composition",
                outcome="accepted",
                candidate_id=bundle.entry.candidate_id,
                detail={"expires_at": utc_marker(expires_at)},
            )
        )
        if bundle.scaling is not None:
            (
                entry_order_type,
                entry_price,
                entry_trigger_price,
                entry_limit_price,
            ) = self._scaling_order_fields(bundle.scaling)
            entry_post_only = False
        else:
            entry_order_type = bundle.entry.order_type
            entry_price = bundle.entry.price
            entry_trigger_price = bundle.entry.trigger_price
            entry_limit_price = bundle.entry.limit_price
            entry_post_only = bundle.entry.post_only
        entry_intent = EntryIntent(
            order_type=entry_order_type,
            quantity=quantity,
            reference_entry_price=(
                bundle.scaling.desired_price
                if bundle.scaling is not None
                else bundle.protection.entry_price
            ),
            price=entry_price,
            trigger_price=entry_trigger_price,
            limit_price=entry_limit_price,
            post_only=entry_post_only,
            ttl_bars=bundle.entry.ttl_bars,
            reduce_only=evaluation.risk_reducing,
        )
        if evaluation.risk_reducing:
            try:
                sizing_notional = exact_multiply(
                    quantity,
                    entry_intent.reference_entry_price,
                )
            except DecimalDomainError as exc:
                raise PlanInvariantError(str(exc)) from exc
            sizing_risk_amount = Decimal("0")
            sizing_risk_fraction = Decimal("0")
            sizing_leverage = Decimal("0")
            sizing_constraints = tuple(
                sorted(set((*bundle.sizing.binding_constraints, "risk_reducing_action")))
            )
        else:
            sizing_notional = bundle.sizing.notional
            sizing_risk_amount = bundle.sizing.risk_amount
            sizing_risk_fraction = bundle.sizing.risk_fraction
            sizing_leverage = bundle.sizing.leverage
            sizing_constraints = bundle.sizing.binding_constraints
        _attach_input_inventory(audit, package)
        plan_audit = self._audit_trace(audit)
        return ExecutableTradePlan.create(
            module_version=self.MODULE_VERSION,
            portfolio_id=package.portfolio.portfolio_id,
            strategy_id=bundle.entry.strategy_id,
            setup_id=bundle.entry.setup_id,
            symbol=bundle.entry.symbol,
            market=bundle.entry.market,
            action=evaluation.action,
            side=bundle.entry.side,
            created_at=package.as_of,
            valid_from=package.as_of,
            expires_at=expires_at,
            expected_portfolio_version=package.portfolio.version,
            expected_position_version=package.portfolio.position_state_version,
            expected_order_state_version=package.portfolio.order_state_version,
            expected_risk_reservation_version=package.portfolio.risk_reservation_version,
            selected_candidate_id=bundle.entry.candidate_id,
            position_intent=position_intent,
            entry_intent=entry_intent,
            protection_intent=(
                None
                if evaluation.risk_reducing
                else ProtectionIntent(
                    stop_loss=bundle.protection.stop_loss,
                    targets=bundle.protection.targets,
                    max_holding_seconds=bundle.protection.max_holding_seconds,
                    no_progress_rule=bundle.protection.no_progress_rule,
                    invalidation_rule=bundle.protection.invalidation_rule,
                )
            ),
            sizing_intent=SizingIntent(
                quantity=quantity,
                notional=sizing_notional,
                risk_amount=sizing_risk_amount,
                risk_fraction=sizing_risk_fraction,
                leverage=sizing_leverage,
                binding_constraints=sizing_constraints,
            ),
            scaling_decision_id=None if bundle.scaling is None else bundle.scaling.decision_id,
            cancellation_intents=evaluation.cancellations,
            sequencing_rules=evaluation.sequencing_rules,
            preconditions=preconditions,
            reason_codes=(ReasonCode.ACCEPTED,),
            warnings=evaluation.warnings,
            input_hashes=tuple(input_hashes),
            configuration_hash=self._config.semantic_fingerprint,
            rejected_candidates=_coalesce_rejected_candidates(rejected),
            audit_trace=plan_audit,
        )

    def compose_risk_reduction(
        self,
        bundle: RiskReductionBundle,
        evaluation: PortfolioEvaluation,
        package: RouterInputPackage,
        rejected: list[RejectedCandidate],
        audit: list[AuditEvent],
    ) -> ExecutableTradePlan:
        expiry_candidates = [
            _safe_future_time(package.as_of, self._config.plan_ttl_seconds),
        ]
        if bundle.scaling.expires_at is not None:
            expiry_candidates.append(bundle.scaling.expires_at)
        expires_at = min(expiry_candidates)
        order_type, price, trigger_price, limit_price = self._scaling_order_fields(bundle.scaling)
        position_intent = PositionIntent(
            position_id=bundle.position.position_id,
            expected_position_version=bundle.position.version,
            current_side=bundle.position.side,
            current_quantity=bundle.position.quantity,
        )
        preconditions: tuple[ExecutionPrecondition, ...] = (
            ExecutionPrecondition("portfolio_version", package.portfolio.version),
            ExecutionPrecondition("portfolio_content_hash", package.portfolio.input_hash),
            ExecutionPrecondition(
                "position_state_version", package.portfolio.position_state_version
            ),
            ExecutionPrecondition("order_state_version", package.portfolio.order_state_version),
            ExecutionPrecondition(
                "risk_reservation_version",
                package.portfolio.risk_reservation_version,
            ),
            ExecutionPrecondition("configuration_hash", self._config.semantic_fingerprint),
            ExecutionPrecondition("target_position_id", bundle.position.position_id),
            ExecutionPrecondition("target_position_version", bundle.position.version),
            ExecutionPrecondition(
                "target_position_quantity_at_least",
                decimal_text(bundle.scaling.approved_quantity),
            ),
            ExecutionPrecondition("reduce_only", "true"),
        )
        if bundle.scaling.expires_at is not None:
            preconditions = (
                *preconditions,
                ExecutionPrecondition(
                    "scaling_decision_not_expired",
                    utc_marker(bundle.scaling.expires_at),
                ),
            )
        audit.append(
            AuditEvent(
                sequence=len(audit) + 1,
                stage="portfolio_constraints",
                outcome="accepted",
                candidate_id=bundle.selected_candidate_id,
                detail={"action": evaluation.action.value, "risk_reducing": True},
            )
        )
        audit.append(
            AuditEvent(
                sequence=len(audit) + 1,
                stage="plan_composition",
                outcome="accepted",
                candidate_id=bundle.selected_candidate_id,
                detail={
                    "expires_at": utc_marker(expires_at),
                    "source": "standalone_scaling_decision",
                },
            )
        )
        quantity = bundle.scaling.approved_quantity
        entry_intent = EntryIntent(
            order_type=order_type,
            quantity=quantity,
            reference_entry_price=bundle.scaling.desired_price,
            price=price,
            trigger_price=trigger_price,
            limit_price=limit_price,
            post_only=False,
            ttl_bars=None,
            reduce_only=True,
        )
        _attach_input_inventory(audit, package)
        plan_audit = self._audit_trace(audit)
        return ExecutableTradePlan.create(
            module_version=self.MODULE_VERSION,
            portfolio_id=package.portfolio.portfolio_id,
            strategy_id=bundle.strategy_id,
            setup_id=bundle.setup_id,
            symbol=bundle.scaling.symbol,
            market=bundle.market,
            action=evaluation.action,
            side=bundle.position.side,
            created_at=package.as_of,
            valid_from=package.as_of,
            expires_at=expires_at,
            expected_portfolio_version=package.portfolio.version,
            expected_position_version=package.portfolio.position_state_version,
            expected_order_state_version=package.portfolio.order_state_version,
            expected_risk_reservation_version=package.portfolio.risk_reservation_version,
            selected_candidate_id=bundle.selected_candidate_id,
            position_intent=position_intent,
            entry_intent=entry_intent,
            protection_intent=None,
            sizing_intent=SizingIntent(
                quantity=quantity,
                notional=self._risk_reduction_notional(
                    quantity,
                    bundle.scaling.desired_price,
                ),
                risk_amount=Decimal("0"),
                risk_fraction=Decimal("0"),
                leverage=Decimal("0"),
                binding_constraints=(
                    "risk_reducing_action",
                    "standalone_scaling_decision",
                ),
            ),
            scaling_decision_id=bundle.scaling.decision_id,
            cancellation_intents=evaluation.cancellations,
            sequencing_rules=evaluation.sequencing_rules,
            preconditions=preconditions,
            reason_codes=(ReasonCode.ACCEPTED,),
            warnings=evaluation.warnings,
            input_hashes=(
                *bundle.input_hashes,
                package.portfolio.input_hash,
            ),
            configuration_hash=self._config.semantic_fingerprint,
            rejected_candidates=_coalesce_rejected_candidates(rejected),
            audit_trace=plan_audit,
        )

    @staticmethod
    def _risk_reduction_notional(quantity: Decimal, price: Decimal) -> Decimal:
        try:
            return exact_multiply(quantity, price)
        except DecimalDomainError as exc:
            raise PlanInvariantError(str(exc)) from exc

    @staticmethod
    def _scaling_order_fields(
        scaling: ScalingDecision,
    ) -> tuple[str, Decimal | None, Decimal | None, Decimal | None]:
        if scaling.order_type == "market":
            return "market", None, None, None
        if scaling.order_type == "limit":
            return "limit", scaling.desired_price, None, scaling.desired_price
        if scaling.order_type == "stop_market":
            return "stop_market", None, scaling.trigger_price, None
        if scaling.order_type == "stop_limit":
            return (
                "stop_limit",
                scaling.desired_price,
                scaling.trigger_price,
                scaling.desired_price,
            )
        raise PlanInvariantError(f"unsupported normalized scaling order type: {scaling.order_type}")

    def _audit_trace(self, audit: list[AuditEvent]) -> tuple[AuditEvent, ...]:
        if self._config.audit_level == "full":
            return tuple(audit)
        return _renumber_audit(
            event
            for event in audit
            if event.outcome in {"rejected", "selected"}
            or event.stage in {"portfolio_constraints", "plan_composition"}
        )


def _renumber_audit(events: Iterable[AuditEvent]) -> tuple[AuditEvent, ...]:
    return tuple(replace(event, sequence=index) for index, event in enumerate(events, start=1))


class DecisionRouter:
    """Pure deterministic orchestration from upstream decisions to one plan or rejection."""

    def __init__(self, config: RouterConfiguration | None = None) -> None:
        if config is not None and not isinstance(config, RouterConfiguration):
            raise RouterConfigurationError("config must be RouterConfiguration or None")
        self.config = config if config is not None else RouterConfiguration()
        self._freshness = DecisionFreshnessValidator(self.config)
        self._validator = DecisionValidator(self.config)
        self._resolver = ConflictResolver(self.config)
        self._portfolio = PortfolioConstraintEvaluator(self.config)
        self._composer = TradePlanComposer(self.config)

    def route(self, package: RouterInputPackage) -> RoutingResult:
        if not isinstance(package, RouterInputPackage):
            raise RouterInputError("package must be RouterInputPackage")
        rejected: list[RejectedCandidate] = []
        audit: list[AuditEvent] = []
        limit_detail = self._input_limit_detail(package)
        if limit_detail is not None:
            audit.append(
                AuditEvent(
                    sequence=1,
                    stage="input_limits",
                    outcome="rejected",
                    reason_code=ReasonCode.INPUT_LIMIT_EXCEEDED,
                    detail={"detail": limit_detail},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INPUT_LIMIT_EXCEEDED,),
                rejected,
                audit,
            )
        reduction = self._route_standalone_risk_reduction(package, rejected, audit)
        if reduction is not None:
            return reduction
        if not package.entries:
            audit.append(
                AuditEvent(
                    sequence=1,
                    stage="candidate_collection",
                    outcome="rejected",
                    reason_code=ReasonCode.MISSING_ENTRY_DECISION,
                )
            )
            return self._rejected(package, (ReasonCode.MISSING_ENTRY_DECISION,), rejected, audit)
        bundles, rejected, audit = self._validator.collect(package)
        winner = self._resolver.resolve(
            bundles,
            package.portfolio.portfolio_id,
            rejected,
            audit,
        )
        if winner is None:
            reasons = self._aggregate_reasons(rejected)
            if not reasons:
                reasons = (ReasonCode.SETUP_CONFLICT,)
            return self._rejected(package, reasons, rejected, audit)
        evaluation, reason, detail = self._portfolio.evaluate(winner, package)
        if evaluation is None:
            if reason is None:
                raise PlanInvariantError("portfolio rejection is missing a reason code")
            rejected.append(
                RejectedCandidate(
                    winner.entry.candidate_id,
                    winner.entry.setup_id,
                    (reason,),
                    detail,
                )
            )
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="portfolio_constraints",
                    outcome="rejected",
                    reason_code=reason,
                    candidate_id=winner.entry.candidate_id,
                    detail={"detail": detail},
                )
            )
            return self._rejected(package, (reason,), rejected, audit)
        if evaluation.warnings and self.config.warning_escalation:
            escalation_reason = ReasonCode.INCOMPATIBLE_DECISIONS
            rejected.append(
                RejectedCandidate(
                    winner.entry.candidate_id,
                    winner.entry.setup_id,
                    (escalation_reason,),
                    "warnings escalated by configuration: " + ",".join(evaluation.warnings),
                )
            )
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="warning_escalation",
                    outcome="rejected",
                    reason_code=escalation_reason,
                    candidate_id=winner.entry.candidate_id,
                    detail={"warnings": evaluation.warnings},
                )
            )
            return self._rejected(package, (escalation_reason,), rejected, audit)
        try:
            plan = self._composer.compose(winner, evaluation, package, rejected, audit)
        except PlanInvariantError as exc:
            rejected.append(
                RejectedCandidate(
                    winner.entry.candidate_id,
                    winner.entry.setup_id,
                    (ReasonCode.PLAN_INVARIANT_FAILED,),
                    str(exc),
                )
            )
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="plan_invariants",
                    outcome="rejected",
                    reason_code=ReasonCode.PLAN_INVARIANT_FAILED,
                    candidate_id=winner.entry.candidate_id,
                    detail={"detail": str(exc)},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.PLAN_INVARIANT_FAILED,),
                rejected,
                audit,
            )
        return RoutingResult.create(
            verdict=PlanVerdict.ACCEPTED,
            created_at=package.as_of,
            portfolio_id=package.portfolio.portfolio_id,
            configuration_hash=self.config.fingerprint,
            plan=plan,
            reason_codes=(ReasonCode.ACCEPTED,),
            rejected_candidates=_coalesce_rejected_candidates(rejected),
            audit_trace=plan.audit_trace,
            warnings=plan.warnings,
        )

    def _route_standalone_risk_reduction(
        self,
        package: RouterInputPackage,
        rejected: list[RejectedCandidate],
        audit: list[AuditEvent],
    ) -> RoutingResult | None:
        reductions = tuple(
            scaling
            for scaling in package.scalings
            if scaling.direction is ScalingDirection.DECREASE
            and scaling.action in {ScalingAction.REDUCE, ScalingAction.REBALANCE}
        )
        if not reductions:
            return None
        if not package.entries and (package.protections or package.sizings):
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_collection",
                    outcome="rejected",
                    reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                    detail={
                        "detail": (
                            "standalone risk reduction cannot ignore orphan protection "
                            "or sizing decisions"
                        ),
                        "protection_count": len(package.protections),
                        "sizing_count": len(package.sizings),
                    },
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INCOMPATIBLE_DECISIONS,),
                rejected,
                audit,
            )
        if len(package.scalings) != 1 or len(reductions) != 1:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_collection",
                    outcome="rejected",
                    reason_code=ReasonCode.INVALID_SCALING,
                    detail={
                        "detail": "standalone risk reduction requires exactly one scaling decision",
                        "scaling_decision_ids": tuple(
                            item.decision_id for item in package.scalings
                        ),
                    },
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INVALID_SCALING,),
                rejected,
                audit,
            )
        scaling = reductions[0]
        freshness = self._freshness.linked_reason(
            scaling.created_at,
            scaling.expires_at,
            package,
        )
        if freshness is not None:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=freshness,
                    candidate_id=scaling.decision_id,
                    detail={"detail": "scaling time validity failed"},
                )
            )
            return self._rejected(package, (freshness,), rejected, audit)
        positions = tuple(
            position
            for position in package.portfolio.positions
            if position.position_id == scaling.position_id
        )
        if len(positions) != 1:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INVALID_SCALING,
                    candidate_id=scaling.decision_id,
                    detail={"detail": "target position does not exist"},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INVALID_SCALING,),
                rejected,
                audit,
            )
        position = positions[0]
        if position.symbol != scaling.symbol or position.side is not scaling.side:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INVALID_SCALING,
                    candidate_id=scaling.decision_id,
                    detail={"detail": "scaling ownership conflicts with target position"},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INVALID_SCALING,),
                rejected,
                audit,
            )
        if (
            scaling.setup_id is not None
            and position.setup_id is not None
            and scaling.setup_id != position.setup_id
        ):
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INVALID_SCALING,
                    candidate_id=scaling.decision_id,
                    detail={"detail": "scaling setup conflicts with target position"},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INVALID_SCALING,),
                rejected,
                audit,
            )
        setup_id = scaling.setup_id or position.setup_id
        entry_assessments = tuple(
            (entry, self._freshness.entry_reason(entry, package)) for entry in package.entries
        )
        needs_ownership_entry = position.strategy_id is None or position.market is None
        matching_assessments = tuple(
            (entry, reason)
            for entry, reason in entry_assessments
            if setup_id is not None
            and entry.setup_id == setup_id
            and entry.symbol == scaling.symbol
            and entry.side is scaling.side
        )
        valid_ownership_entries = tuple(
            entry for entry, reason in matching_assessments if reason is None
        )
        conflicting_ownership_entries = tuple(
            entry
            for entry in valid_ownership_entries
            if (position.strategy_id is not None and entry.strategy_id != position.strategy_id)
            or (position.market is not None and entry.market != position.market)
        )
        if conflicting_ownership_entries:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                    candidate_id=scaling.decision_id,
                    detail={
                        "detail": "current entry ownership conflicts with target position",
                        "entry_ids": tuple(
                            entry.candidate_id for entry in conflicting_ownership_entries
                        ),
                    },
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INCOMPATIBLE_DECISIONS,),
                rejected,
                audit,
            )
        ownership_entry: EntryCandidate | None = None
        if needs_ownership_entry:
            if len(valid_ownership_entries) > 1:
                audit.append(
                    AuditEvent(
                        sequence=len(audit) + 1,
                        stage="risk_reduction_validation",
                        outcome="rejected",
                        reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                        candidate_id=scaling.decision_id,
                        detail={
                            "detail": "multiple current entries provide ambiguous position ownership"
                        },
                    )
                )
                return self._rejected(
                    package,
                    (ReasonCode.INCOMPATIBLE_DECISIONS,),
                    rejected,
                    audit,
                )
            if not valid_ownership_entries:
                temporal_reasons = tuple(
                    sorted(
                        {reason for _entry, reason in matching_assessments if reason is not None},
                        key=lambda item: item.value,
                    )
                )
                if temporal_reasons:
                    audit.append(
                        AuditEvent(
                            sequence=len(audit) + 1,
                            stage="risk_reduction_validation",
                            outcome="rejected",
                            reason_code=temporal_reasons[0],
                            candidate_id=scaling.decision_id,
                            detail={
                                "detail": ("position ownership depends on a stale or future entry"),
                                "entry_ids": tuple(
                                    entry.candidate_id for entry, _reason in matching_assessments
                                ),
                            },
                        )
                    )
                    return self._rejected(
                        package,
                        temporal_reasons,
                        rejected,
                        audit,
                    )
                audit.append(
                    AuditEvent(
                        sequence=len(audit) + 1,
                        stage="risk_reduction_validation",
                        outcome="rejected",
                        reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                        candidate_id=scaling.decision_id,
                        detail={
                            "detail": (
                                "missing position ownership cannot be recovered from "
                                "one current matching entry"
                            )
                        },
                    )
                )
                return self._rejected(
                    package,
                    (ReasonCode.INCOMPATIBLE_DECISIONS,),
                    rejected,
                    audit,
                )
            ownership_entry = valid_ownership_entries[0]
        if (
            ownership_entry is not None
            and position.strategy_id is not None
            and ownership_entry.strategy_id != position.strategy_id
        ):
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                    candidate_id=scaling.decision_id,
                    detail={"detail": "entry strategy conflicts with target position owner"},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INCOMPATIBLE_DECISIONS,),
                rejected,
                audit,
            )
        if (
            ownership_entry is not None
            and position.market is not None
            and ownership_entry.market != position.market
        ):
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                    candidate_id=scaling.decision_id,
                    detail={"detail": "entry market conflicts with target position market"},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INCOMPATIBLE_DECISIONS,),
                rejected,
                audit,
            )
        strategy_id = position.strategy_id or (
            None if ownership_entry is None else ownership_entry.strategy_id
        )
        market = position.market or (None if ownership_entry is None else ownership_entry.market)
        if setup_id is None or strategy_id is None or market is None:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="risk_reduction_validation",
                    outcome="rejected",
                    reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                    candidate_id=scaling.decision_id,
                    detail={
                        "detail": (
                            "standalone risk reduction requires explicit strategy_id, "
                            "setup_id and market ownership"
                        )
                    },
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INCOMPATIBLE_DECISIONS,),
                rejected,
                audit,
            )
        # The scaling decision is the executable trade intent. Entry decisions are
        # always deferred; one current matching entry may be used only to recover
        # ownership fields that are absent from the factual position snapshot.
        selected_candidate_id = scaling.decision_id
        deferred_entries = tuple(
            sorted(
                entry_assessments,
                key=lambda item: (item[0].setup_id, item[0].candidate_id),
            )
        )
        for entry, temporal_reason in deferred_entries:
            conflict_reason = temporal_reason or (
                ReasonCode.LONG_SHORT_CONFLICT
                if entry.side is not scaling.side
                else ReasonCode.SETUP_CONFLICT
            )
            detail = (
                "entry used only as ownership evidence and deferred by "
                "reduce-before-open action priority"
                if entry is ownership_entry
                else "entry deferred because risk-reducing scaling has "
                "reduce-before-open action priority"
            )
            rejected.append(
                RejectedCandidate(
                    candidate_id=entry.candidate_id,
                    setup_id=entry.setup_id,
                    reason_codes=(conflict_reason,),
                    detail=detail,
                )
            )
        if package.entries:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="action_priority",
                    outcome="selected",
                    candidate_id=selected_candidate_id,
                    detail={
                        "policy": "reduce_before_open",
                        "scaling_decision_id": scaling.decision_id,
                        "ownership_entry_id": (
                            None if ownership_entry is None else ownership_entry.candidate_id
                        ),
                        "deferred_entry_ids": tuple(
                            entry.candidate_id for entry, _reason in deferred_entries
                        ),
                        "ignored_protection_decision_ids": tuple(
                            decision.decision_id for decision in package.protections
                        ),
                        "ignored_sizing_decision_ids": tuple(
                            decision.decision_id for decision in package.sizings
                        ),
                    },
                )
            )
        ownership_entry_used = ownership_entry is not None and (
            position.strategy_id is None or position.market is None
        )
        bundle = RiskReductionBundle(
            scaling=scaling,
            position=position,
            strategy_id=strategy_id,
            setup_id=setup_id,
            market=market,
            selected_candidate_id=selected_candidate_id,
            input_hashes=tuple(
                sorted(
                    {
                        scaling.input_hash,
                        *(
                            (ownership_entry.input_hash,)
                            if ownership_entry_used and ownership_entry is not None
                            else ()
                        ),
                    }
                )
            ),
        )
        audit.append(
            AuditEvent(
                sequence=len(audit) + 1,
                stage="risk_reduction_validation",
                outcome="accepted",
                candidate_id=selected_candidate_id,
                detail={
                    "position_id": position.position_id,
                    "scaling_decision_id": scaling.decision_id,
                    "priority": "reduce_before_open",
                },
            )
        )
        evaluation, reason, detail = self._portfolio.evaluate_risk_reduction(
            bundle,
            package,
        )
        if evaluation is None:
            if reason is None:
                raise PlanInvariantError("risk reduction rejection is missing reason code")
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="portfolio_constraints",
                    outcome="rejected",
                    reason_code=reason,
                    candidate_id=scaling.decision_id,
                    detail={"detail": detail},
                )
            )
            return self._rejected(package, (reason,), rejected, audit)
        if evaluation.warnings and self.config.warning_escalation:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="warning_escalation",
                    outcome="rejected",
                    reason_code=ReasonCode.INCOMPATIBLE_DECISIONS,
                    candidate_id=scaling.decision_id,
                    detail={"warnings": evaluation.warnings},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.INCOMPATIBLE_DECISIONS,),
                rejected,
                audit,
            )
        try:
            plan = self._composer.compose_risk_reduction(
                bundle,
                evaluation,
                package,
                rejected,
                audit,
            )
        except PlanInvariantError as exc:
            audit.append(
                AuditEvent(
                    sequence=len(audit) + 1,
                    stage="plan_invariants",
                    outcome="rejected",
                    reason_code=ReasonCode.PLAN_INVARIANT_FAILED,
                    candidate_id=scaling.decision_id,
                    detail={"detail": str(exc)},
                )
            )
            return self._rejected(
                package,
                (ReasonCode.PLAN_INVARIANT_FAILED,),
                rejected,
                audit,
            )
        return RoutingResult.create(
            verdict=PlanVerdict.ACCEPTED,
            created_at=package.as_of,
            portfolio_id=package.portfolio.portfolio_id,
            configuration_hash=self.config.fingerprint,
            plan=plan,
            reason_codes=(ReasonCode.ACCEPTED,),
            rejected_candidates=_coalesce_rejected_candidates(rejected),
            audit_trace=plan.audit_trace,
            warnings=plan.warnings,
        )

    def _input_limit_detail(self, package: RouterInputPackage) -> str | None:
        limits = (
            ("entry candidates", len(package.entries), self.config.max_entry_candidates),
            ("protection decisions", len(package.protections), self.config.max_decisions_per_type),
            ("sizing decisions", len(package.sizings), self.config.max_decisions_per_type),
            ("scaling decisions", len(package.scalings), self.config.max_decisions_per_type),
            (
                "portfolio positions",
                len(package.portfolio.positions),
                self.config.max_portfolio_positions,
            ),
            (
                "pending orders",
                len(package.portfolio.pending_orders),
                self.config.max_pending_orders,
            ),
        )
        for label, actual, maximum in limits:
            if actual > maximum:
                return f"{label} count {actual} exceeds configured maximum {maximum}"
        return None

    def _rejected(
        self,
        package: RouterInputPackage,
        reasons: tuple[ReasonCode, ...],
        rejected: list[RejectedCandidate],
        audit: list[AuditEvent],
    ) -> RoutingResult:
        _attach_input_inventory(audit, package)
        return RoutingResult.create(
            verdict=PlanVerdict.REJECTED,
            created_at=package.as_of,
            portfolio_id=package.portfolio.portfolio_id,
            configuration_hash=self.config.fingerprint,
            plan=None,
            reason_codes=tuple(sorted(set(reasons), key=lambda item: item.value)),
            rejected_candidates=_coalesce_rejected_candidates(rejected),
            audit_trace=self._filtered_audit(audit),
            warnings=(),
        )

    def _filtered_audit(self, audit: list[AuditEvent]) -> tuple[AuditEvent, ...]:
        if self.config.audit_level == "full":
            return tuple(audit)
        return _renumber_audit(
            event
            for event in audit
            if event.outcome == "rejected" or event.stage == "conflict_resolution"
        )

    @staticmethod
    def _aggregate_reasons(rejected: list[RejectedCandidate]) -> tuple[ReasonCode, ...]:
        return tuple(
            sorted(
                {reason for candidate in rejected for reason in candidate.reason_codes},
                key=lambda item: item.value,
            )
        )


def utc_marker(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
