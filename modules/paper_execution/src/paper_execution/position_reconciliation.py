from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from modular_core import PositionSide, PositionSnapshot

from .contracts import ReconciliationFinding
from .enums import FindingSeverity
from .state_machine import OrderAggregate


@dataclass(frozen=True, slots=True)
class ExpectedPositionState:
    side: PositionSide
    quantity: Decimal
    require_stop: bool = True
    require_target: bool = False

    def __post_init__(self) -> None:
        side = self.side if isinstance(self.side, PositionSide) else PositionSide(self.side)
        quantity = (
            self.quantity if isinstance(self.quantity, Decimal) else Decimal(str(self.quantity))
        )
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("expected position quantity must be positive and finite")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)


def reconcile_position(
    *,
    expected: ExpectedPositionState | None,
    observed: PositionSnapshot | None,
    orders: Mapping[str, OrderAggregate],
) -> tuple[ReconciliationFinding, ...]:
    findings: list[ReconciliationFinding] = []

    if expected is None:
        if observed is not None and Decimal(str(observed.quantity)) > 0:
            findings.append(
                _finding(
                    category="orphan_position",
                    severity=FindingSeverity.CRITICAL,
                    expected={},
                    observed={"side": observed.side.value, "quantity": str(observed.quantity)},
                    action="block scope and reconstruct the expected position from execution events",
                )
            )
        return tuple(findings)

    if observed is None:
        return (
            _finding(
                category="position_missing",
                severity=FindingSeverity.CRITICAL,
                expected={"side": expected.side.value, "quantity": str(expected.quantity)},
                observed={},
                action="block scope and replay position-producing fills",
            ),
        )

    observed_quantity = Decimal(str(observed.quantity))
    if observed.side is not expected.side:
        findings.append(
            _finding(
                category="position_side_mismatch",
                severity=FindingSeverity.CRITICAL,
                expected={"side": expected.side.value},
                observed={"side": observed.side.value},
                action="block scope; never auto-repair an ambiguous position side",
            )
        )
    if observed_quantity != expected.quantity:
        findings.append(
            _finding(
                category="position_quantity_mismatch",
                severity=FindingSeverity.CRITICAL,
                expected={"quantity": str(expected.quantity)},
                observed={"quantity": str(observed_quantity)},
                action="block scope and replay fills before accepting another plan",
            )
        )

    active = tuple(order for order in orders.values() if not order.state.terminal)
    stop_quantity = sum(
        (order.remaining_quantity for order in active if order.intent_role == "stop"),
        Decimal("0"),
    )
    target_quantity = sum(
        (order.remaining_quantity for order in active if order.intent_role == "target"),
        Decimal("0"),
    )
    has_position_stop = observed.stop_loss is not None
    has_position_target = observed.take_profit is not None

    if has_position_stop and stop_quantity > 0:
        findings.append(
            _finding(
                category="duplicate_stop_exit_paths",
                severity=FindingSeverity.CRITICAL,
                expected={"single_stop_path": True},
                observed={
                    "position_stop_loss": str(observed.stop_loss),
                    "active_stop_quantity": str(stop_quantity),
                },
                action="block scope and retain exactly one authoritative Stop path",
            )
        )
    elif expected.require_stop and not has_position_stop:
        if stop_quantity <= 0:
            findings.append(
                _finding(
                    category="position_unprotected",
                    severity=FindingSeverity.CRITICAL,
                    expected={"minimum_stop_quantity": str(observed_quantity)},
                    observed={"active_stop_quantity": str(stop_quantity)},
                    action="block scope and restore mandatory protection",
                )
            )
        elif stop_quantity < observed_quantity:
            findings.append(
                _finding(
                    category="position_underprotected",
                    severity=FindingSeverity.CRITICAL,
                    expected={"minimum_stop_quantity": str(observed_quantity)},
                    observed={"active_stop_quantity": str(stop_quantity)},
                    action="block scope and resize Stop protection to the full position",
                )
            )
        elif stop_quantity > observed_quantity:
            findings.append(
                _finding(
                    category="protection_overclose_risk",
                    severity=FindingSeverity.CRITICAL,
                    expected={"maximum_stop_quantity": str(observed_quantity)},
                    observed={"active_stop_quantity": str(stop_quantity)},
                    action="block scope and resolve reduce-only over-close risk",
                )
            )
    elif not has_position_stop and stop_quantity > observed_quantity:
        findings.append(
            _finding(
                category="protection_overclose_risk",
                severity=FindingSeverity.CRITICAL,
                expected={"maximum_stop_quantity": str(observed_quantity)},
                observed={"active_stop_quantity": str(stop_quantity)},
                action="block scope and resolve reduce-only over-close risk",
            )
        )

    if has_position_target and target_quantity > 0:
        findings.append(
            _finding(
                category="duplicate_target_exit_paths",
                severity=FindingSeverity.CRITICAL,
                expected={"single_target_path": True},
                observed={
                    "position_take_profit": str(observed.take_profit),
                    "active_target_quantity": str(target_quantity),
                },
                action="block scope and retain exactly one authoritative Target path",
            )
        )
    elif expected.require_target and not has_position_target:
        if target_quantity <= 0:
            findings.append(
                _finding(
                    category="target_missing",
                    severity=FindingSeverity.HIGH,
                    expected={"minimum_target_quantity": str(observed_quantity)},
                    observed={"active_target_quantity": str(target_quantity)},
                    action="block scope and restore the required target",
                )
            )
        elif target_quantity < observed_quantity:
            findings.append(
                _finding(
                    category="target_underallocated",
                    severity=FindingSeverity.HIGH,
                    expected={"minimum_target_quantity": str(observed_quantity)},
                    observed={"active_target_quantity": str(target_quantity)},
                    action="block scope and resize Target coverage to the full position",
                )
            )
        elif target_quantity > observed_quantity:
            findings.append(
                _finding(
                    category="target_overclose_risk",
                    severity=FindingSeverity.CRITICAL,
                    expected={"maximum_target_quantity": str(observed_quantity)},
                    observed={"active_target_quantity": str(target_quantity)},
                    action="block scope and resolve target over-close risk",
                )
            )
    elif not has_position_target and target_quantity > observed_quantity:
        findings.append(
            _finding(
                category="target_overclose_risk",
                severity=FindingSeverity.CRITICAL,
                expected={"maximum_target_quantity": str(observed_quantity)},
                observed={"active_target_quantity": str(target_quantity)},
                action="block scope and resolve target over-close risk",
            )
        )

    return tuple(findings)


def _finding(
    *,
    category: str,
    severity: FindingSeverity,
    expected: Mapping[str, object],
    observed: Mapping[str, object],
    action: str,
) -> ReconciliationFinding:
    return ReconciliationFinding(
        category=category,
        severity=severity,
        expected=expected,
        observed=observed,
        evidence={},
        automatic_resolution_eligible=False,
        recommended_action=action,
        blocking=True,
    )
