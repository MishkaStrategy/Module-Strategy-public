from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import AbstractSet, Iterable, Mapping

from modular_core import OrderSnapshot, OrderStatus

from .contracts import ReconciliationFinding
from .enums import FindingSeverity, PaperOrderState, ReconciliationMode
from .state_machine import OrderAggregate

OWNED_SOURCE_MODULE = "paper_execution_venue_reconciliation"
_TERMINAL_VENUE_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    mode: ReconciliationMode
    findings: tuple[ReconciliationFinding, ...]

    @property
    def blocked(self) -> bool:
        return any(finding.blocking for finding in self.findings)


class ReconciliationEngine:
    def compare(
        self,
        *,
        local_orders: Mapping[str, OrderAggregate],
        venue_orders: tuple[OrderSnapshot, ...],
        mode: ReconciliationMode = ReconciliationMode.STRICT_BLOCK,
    ) -> ReconciliationReport:
        """Perform strict identity-aware comparison of one owned order scope."""

        findings: list[ReconciliationFinding] = []
        self._append_local_identity_findings(local_orders, findings)
        self._append_venue_identity_findings(venue_orders, findings)

        venue_by_client = self._unique_venue_by_client(venue_orders)
        for mapping_key, aggregate in local_orders.items():
            client_id = aggregate.client_order_id
            venue = venue_by_client.get(client_id)
            if venue is None:
                if not aggregate.state.terminal:
                    findings.append(
                        _finding(
                            category="internal_order_missing_at_venue",
                            severity=FindingSeverity.CRITICAL,
                            expected={
                                "client_order_id": client_id,
                                "state": aggregate.state.value,
                            },
                            observed={},
                            evidence={"core_order_id": aggregate.core_order_id},
                            action="block scope and inspect venue/journal",
                            blocking=True,
                        )
                    )
                continue
            if aggregate.state.terminal:
                findings.append(
                    _finding(
                        category="terminal_local_order_still_pending",
                        severity=FindingSeverity.CRITICAL,
                        expected={"terminal": True},
                        observed={"venue_status": venue.status.value},
                        evidence={
                            "client_order_id": client_id,
                            "mapping_key": mapping_key,
                        },
                        action="block scope and reconcile stale venue state",
                        blocking=True,
                    )
                )
                continue
            self._append_order_mismatches(aggregate, venue, findings)

        local_client_ids = {aggregate.client_order_id for aggregate in local_orders.values()}
        duplicate_clients = Counter(
            snapshot.client_order_id
            for snapshot in venue_orders
            if snapshot.client_order_id is not None
        )
        for snapshot in venue_orders:
            venue_client_id = snapshot.client_order_id
            if snapshot.status in _TERMINAL_VENUE_STATUSES:
                findings.append(
                    _finding(
                        category="terminal_order_in_pending_snapshot",
                        severity=FindingSeverity.CRITICAL,
                        expected={"pending_view_terminal": False},
                        observed={"status": snapshot.status.value},
                        evidence={"core_order_id": snapshot.id},
                        action="block scope and repair pending-order projection",
                        blocking=True,
                    )
                )
            if venue_client_id is None:
                if snapshot.source_module == OWNED_SOURCE_MODULE:
                    findings.append(
                        _finding(
                            category="owned_venue_order_missing_client_id",
                            severity=FindingSeverity.CRITICAL,
                            expected={"client_order_id": "non-null"},
                            observed={"client_order_id": None},
                            evidence={"core_order_id": snapshot.id},
                            action="block scope; ownership cannot be reconstructed safely",
                            blocking=True,
                        )
                    )
                continue
            if venue_client_id in local_client_ids:
                continue
            if duplicate_clients[venue_client_id] > 1:
                continue
            if snapshot.source_module == OWNED_SOURCE_MODULE:
                findings.append(
                    _finding(
                        category="orphan_venue_order",
                        severity=FindingSeverity.CRITICAL,
                        expected={},
                        observed={
                            "client_order_id": venue_client_id,
                            "core_order_id": snapshot.id,
                        },
                        evidence={"status": snapshot.status.value},
                        action="restore the unique derived local order projection",
                        blocking=True,
                        automatic_resolution_eligible=(
                            snapshot.status not in _TERMINAL_VENUE_STATUSES
                        ),
                    )
                )
            else:
                findings.append(
                    _finding(
                        category="unknown_external_order",
                        severity=FindingSeverity.WARNING,
                        expected={},
                        observed={
                            "client_order_id": venue_client_id,
                            "source_module": snapshot.source_module,
                        },
                        evidence={"core_order_id": snapshot.id},
                        action="report foreign order without mutating it",
                        blocking=False,
                    )
                )

        return ReconciliationReport(mode=mode, findings=tuple(findings))

    def reconcile_orders(
        self,
        *,
        internal: Mapping[str, OrderAggregate],
        venue_orders: tuple[OrderSnapshot, ...],
        mode: ReconciliationMode,
        known_client_order_ids: AbstractSet[str] | None = None,
        owned_prefix: str | None = None,
    ) -> ReconciliationReport:
        """Compare a scoped local projection while ignoring known other scopes."""

        findings: list[ReconciliationFinding] = []
        known = set(internal) if known_client_order_ids is None else set(known_client_order_ids)
        relevant = tuple(
            snapshot
            for snapshot in venue_orders
            if self._is_relevant_snapshot(
                snapshot,
                internal=internal,
                known=known,
                owned_prefix=owned_prefix,
            )
        )
        client_counts = Counter(
            snapshot.client_order_id
            for snapshot in relevant
            if snapshot.client_order_id is not None
        )
        for client_id, count in client_counts.items():
            if count > 1:
                findings.append(
                    _finding(
                        category="duplicate_venue_order",
                        severity=FindingSeverity.CRITICAL,
                        expected={"count": 1},
                        observed={"count": count},
                        evidence={"client_order_id": client_id},
                        action="block scope and inspect duplicate venue orders",
                        blocking=True,
                    )
                )

        venue_by_client = self._unique_venue_by_client(relevant)
        for client_id, aggregate in internal.items():
            venue = venue_by_client.get(client_id)
            if venue is None:
                if not aggregate.state.terminal:
                    findings.append(
                        _finding(
                            category="internal_order_missing_at_venue",
                            severity=FindingSeverity.CRITICAL,
                            expected={
                                "client_order_id": client_id,
                                "state": aggregate.state.value,
                            },
                            observed={},
                            evidence={"core_order_id": aggregate.core_order_id},
                            action="block scope and inspect venue/journal",
                            blocking=True,
                        )
                    )
                continue
            if aggregate.filled_quantity != Decimal(str(venue.filled_quantity)):
                findings.append(
                    _finding(
                        category="filled_quantity_mismatch",
                        severity=FindingSeverity.CRITICAL,
                        expected={"filled_quantity": str(aggregate.filled_quantity)},
                        observed={"filled_quantity": str(venue.filled_quantity)},
                        evidence={"client_order_id": client_id},
                        action="block scope and replay execution events",
                        blocking=True,
                    )
                )

        for client_id, venue in venue_by_client.items():
            if client_id in internal or client_id in known:
                continue
            if owned_prefix is not None and not client_id.startswith(owned_prefix):
                continue
            findings.append(
                _finding(
                    category="venue_order_missing_internally",
                    severity=FindingSeverity.CRITICAL,
                    expected={},
                    observed={
                        "client_order_id": client_id,
                        "order_id": venue.id,
                    },
                    evidence={
                        "status": venue.status.value,
                        "source_module": venue.source_module,
                    },
                    action="restore the unique derived local order projection",
                    blocking=True,
                    automatic_resolution_eligible=(
                        venue.source_module == OWNED_SOURCE_MODULE
                        and client_counts[client_id] == 1
                        and venue.status not in _TERMINAL_VENUE_STATUSES
                    ),
                )
            )
        return ReconciliationReport(mode=mode, findings=tuple(findings))

    @staticmethod
    def _is_relevant_snapshot(
        snapshot: OrderSnapshot,
        *,
        internal: Mapping[str, OrderAggregate],
        known: set[str],
        owned_prefix: str | None,
    ) -> bool:
        client_id = snapshot.client_order_id
        if client_id is None:
            return snapshot.source_module == OWNED_SOURCE_MODULE
        if client_id in internal:
            return True
        if client_id in known:
            return False
        if owned_prefix is None:
            return True
        return client_id.startswith(owned_prefix)

    @staticmethod
    def _unique_venue_by_client(
        venue_orders: Iterable[OrderSnapshot],
    ) -> dict[str, OrderSnapshot]:
        result: dict[str, OrderSnapshot] = {}
        duplicates: set[str] = set()
        for snapshot in venue_orders:
            client_id = snapshot.client_order_id
            if client_id is None:
                continue
            if client_id in result:
                duplicates.add(client_id)
                continue
            result[client_id] = snapshot
        for client_id in duplicates:
            result.pop(client_id, None)
        return result

    @staticmethod
    def _append_local_identity_findings(
        local_orders: Mapping[str, OrderAggregate],
        findings: list[ReconciliationFinding],
    ) -> None:
        core_counts = Counter(aggregate.core_order_id for aggregate in local_orders.values())
        for mapping_key, aggregate in local_orders.items():
            if mapping_key != aggregate.client_order_id:
                findings.append(
                    _finding(
                        category="local_order_identity_mismatch",
                        severity=FindingSeverity.CRITICAL,
                        expected={"mapping_key": aggregate.client_order_id},
                        observed={"mapping_key": mapping_key},
                        evidence={"core_order_id": aggregate.core_order_id},
                        action="block scope and rebuild the local order mapping",
                        blocking=True,
                    )
                )
        for core_id, count in core_counts.items():
            if count > 1:
                findings.append(
                    _finding(
                        category="duplicate_local_core_order_id",
                        severity=FindingSeverity.CRITICAL,
                        expected={"count": 1},
                        observed={"count": count},
                        evidence={"core_order_id": core_id},
                        action="block scope and inspect local projection corruption",
                        blocking=True,
                    )
                )

    @staticmethod
    def _append_venue_identity_findings(
        venue_orders: tuple[OrderSnapshot, ...],
        findings: list[ReconciliationFinding],
    ) -> None:
        client_counts = Counter(
            snapshot.client_order_id
            for snapshot in venue_orders
            if snapshot.client_order_id is not None
        )
        core_counts = Counter(snapshot.id for snapshot in venue_orders)
        for client_id, count in client_counts.items():
            if count > 1:
                findings.append(
                    _finding(
                        category="duplicate_venue_client_order_id",
                        severity=FindingSeverity.CRITICAL,
                        expected={"count": 1},
                        observed={"count": count},
                        evidence={"client_order_id": client_id},
                        action="block scope and inspect duplicate venue identities",
                        blocking=True,
                    )
                )
        for core_id, count in core_counts.items():
            if count > 1:
                findings.append(
                    _finding(
                        category="duplicate_venue_core_order_id",
                        severity=FindingSeverity.CRITICAL,
                        expected={"count": 1},
                        observed={"count": count},
                        evidence={"core_order_id": core_id},
                        action="block scope and inspect duplicate venue identities",
                        blocking=True,
                    )
                )

    @staticmethod
    def _append_order_mismatches(
        aggregate: OrderAggregate,
        venue: OrderSnapshot,
        findings: list[ReconciliationFinding],
    ) -> None:
        evidence = {"client_order_id": aggregate.client_order_id}
        if aggregate.core_order_id != venue.id:
            findings.append(
                _finding(
                    category="core_order_id_mismatch",
                    severity=FindingSeverity.CRITICAL,
                    expected={"core_order_id": aggregate.core_order_id},
                    observed={"core_order_id": venue.id},
                    evidence=evidence,
                    action="block scope and resolve order identity conflict",
                    blocking=True,
                )
            )
        if aggregate.requested_quantity != Decimal(str(venue.quantity)):
            findings.append(
                _finding(
                    category="order_quantity_mismatch",
                    severity=FindingSeverity.CRITICAL,
                    expected={"quantity": str(aggregate.requested_quantity)},
                    observed={"quantity": str(venue.quantity)},
                    evidence=evidence,
                    action="block scope and replay the original order request",
                    blocking=True,
                )
            )
        if aggregate.filled_quantity != Decimal(str(venue.filled_quantity)):
            findings.append(
                _finding(
                    category="order_fill_mismatch",
                    severity=FindingSeverity.CRITICAL,
                    expected={"filled_quantity": str(aggregate.filled_quantity)},
                    observed={"filled_quantity": str(venue.filled_quantity)},
                    evidence=evidence,
                    action="block scope and replay fills",
                    blocking=True,
                )
            )
        expected_states = _expected_local_states(venue.status)
        if aggregate.state not in expected_states:
            findings.append(
                _finding(
                    category="order_state_mismatch",
                    severity=FindingSeverity.CRITICAL,
                    expected={"states": sorted(state.value for state in expected_states)},
                    observed={"state": aggregate.state.value},
                    evidence={**evidence, "venue_status": venue.status.value},
                    action="block scope and replay order lifecycle events",
                    blocking=True,
                )
            )


def _expected_local_states(status: OrderStatus) -> frozenset[PaperOrderState]:
    if status is OrderStatus.PENDING:
        return frozenset(
            {
                PaperOrderState.SUBMITTED,
                PaperOrderState.ACKNOWLEDGED,
                PaperOrderState.OPEN,
                PaperOrderState.CANCEL_PENDING,
            }
        )
    if status is OrderStatus.PARTIALLY_FILLED:
        return frozenset(
            {
                PaperOrderState.PARTIALLY_FILLED,
                PaperOrderState.CANCEL_PENDING,
            }
        )
    mapping = {
        OrderStatus.FILLED: PaperOrderState.FILLED,
        OrderStatus.CANCELLED: PaperOrderState.CANCELED,
        OrderStatus.EXPIRED: PaperOrderState.EXPIRED,
        OrderStatus.REJECTED: PaperOrderState.REJECTED,
    }
    mapped = mapping.get(status)
    return frozenset({mapped}) if mapped is not None else frozenset()


def _finding(
    *,
    category: str,
    severity: FindingSeverity,
    expected: Mapping[str, object],
    observed: Mapping[str, object],
    evidence: Mapping[str, object],
    action: str,
    blocking: bool,
    automatic_resolution_eligible: bool = False,
) -> ReconciliationFinding:
    return ReconciliationFinding(
        category=category,
        severity=severity,
        expected=expected,
        observed=observed,
        evidence=evidence,
        automatic_resolution_eligible=automatic_resolution_eligible,
        recommended_action=action,
        blocking=blocking,
    )
