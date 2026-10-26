from __future__ import annotations

from dataclasses import dataclass

import pytest

from paper_execution.enums import ReconciliationMode
from paper_execution.reconciliation_policy import LifecycleState, decide_reconciliation


@dataclass(frozen=True)
class Finding:
    category: str
    automatic_resolution_eligible: bool
    blocking: bool


def test_report_only_degrades_without_blocking() -> None:
    decision = decide_reconciliation(
        mode=ReconciliationMode.REPORT_ONLY,
        findings=[Finding("position_quantity_mismatch", False, True)],
        recovery_pending=False,
    )
    assert decision.lifecycle is LifecycleState.DEGRADED
    assert not decision.block_scope
    assert not decision.recovery_complete

    clean_recovery = decide_reconciliation(
        mode=ReconciliationMode.REPORT_ONLY,
        findings=[],
        recovery_pending=True,
    )
    assert clean_recovery.lifecycle is LifecycleState.READY
    assert clean_recovery.recovery_complete


def test_safe_auto_repair_requires_actual_repair_and_reconciliation() -> None:
    safe = decide_reconciliation(
        mode=ReconciliationMode.SAFE_AUTO_REPAIR,
        findings=[Finding("venue_order_missing_internally", True, True)],
        recovery_pending=False,
    )
    assert safe.lifecycle is LifecycleState.RECONCILING
    assert safe.block_scope
    assert not safe.recovery_complete
    assert safe.repair_categories == ("venue_order_missing_internally",)

    unsafe = decide_reconciliation(
        mode=ReconciliationMode.SAFE_AUTO_REPAIR,
        findings=[Finding("position_quantity_mismatch", True, True)],
        recovery_pending=False,
    )
    assert unsafe.lifecycle is LifecycleState.BLOCKED
    assert unsafe.block_scope
    assert unsafe.repair_categories == ()

    clean = decide_reconciliation(
        mode=ReconciliationMode.SAFE_AUTO_REPAIR,
        findings=[],
        recovery_pending=True,
    )
    assert clean.lifecycle is LifecycleState.READY
    assert clean.recovery_complete


def test_strict_block_blocks_only_blocking_findings() -> None:
    warning = decide_reconciliation(
        mode=ReconciliationMode.STRICT_BLOCK,
        findings=[Finding("average_price_mismatch", False, False)],
        recovery_pending=False,
    )
    assert warning.lifecycle is LifecycleState.DEGRADED
    assert not warning.block_scope

    critical = decide_reconciliation(
        mode=ReconciliationMode.STRICT_BLOCK,
        findings=[Finding("position_quantity_mismatch", False, True)],
        recovery_pending=False,
    )
    assert critical.lifecycle is LifecycleState.BLOCKED
    assert critical.block_scope

    clean_recovery = decide_reconciliation(
        mode=ReconciliationMode.STRICT_BLOCK,
        findings=[],
        recovery_pending=True,
    )
    assert clean_recovery.recovery_complete


def test_recovery_mode_requires_exact_match() -> None:
    clean = decide_reconciliation(
        mode=ReconciliationMode.RECOVERY_MODE,
        findings=[],
        recovery_pending=True,
    )
    assert clean.lifecycle is LifecycleState.READY
    assert clean.recovery_complete

    mismatch = decide_reconciliation(
        mode=ReconciliationMode.RECOVERY_MODE,
        findings=[Finding("target_missing", False, False)],
        recovery_pending=True,
    )
    assert mismatch.lifecycle is LifecycleState.BLOCKED
    assert mismatch.block_scope
    assert not mismatch.recovery_complete


def test_unknown_mode_fails_closed() -> None:
    with pytest.raises(ValueError):
        decide_reconciliation(mode="bogus", findings=[], recovery_pending=False)  # type: ignore[arg-type]
