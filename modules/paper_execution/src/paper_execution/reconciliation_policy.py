from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol

from .enums import ReconciliationMode


class LifecycleState(str, Enum):
    CREATED = "created"
    RECOVERING = "recovering"
    RECONCILING = "reconciling"
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    FAILED = "failed"
    STOPPED = "stopped"


class FindingLike(Protocol):
    @property
    def category(self) -> str: ...

    @property
    def automatic_resolution_eligible(self) -> bool: ...

    @property
    def blocking(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    lifecycle: LifecycleState
    block_scope: bool
    recovery_complete: bool
    repair_categories: tuple[str, ...] = ()


_SAFE_REPAIR_CATEGORIES = frozenset({"venue_order_missing_internally"})


def decide_reconciliation(
    *,
    mode: ReconciliationMode,
    findings: Iterable[FindingLike],
    recovery_pending: bool,
) -> PolicyDecision:
    rows = tuple(findings)
    unresolved_blocking = any(row.blocking for row in rows)
    any_unresolved = bool(rows)

    if mode is ReconciliationMode.REPORT_ONLY:
        lifecycle = LifecycleState.DEGRADED if any_unresolved else LifecycleState.READY
        return PolicyDecision(
            lifecycle=lifecycle,
            block_scope=False,
            recovery_complete=not any_unresolved,
        )

    if mode is ReconciliationMode.SAFE_AUTO_REPAIR:
        if not any_unresolved:
            return PolicyDecision(LifecycleState.READY, False, True)
        repairable = all(
            row.automatic_resolution_eligible and row.category in _SAFE_REPAIR_CATEGORIES
            for row in rows
        )
        if not repairable:
            return PolicyDecision(LifecycleState.BLOCKED, True, False)
        return PolicyDecision(
            LifecycleState.RECONCILING,
            True,
            False,
            tuple(row.category for row in rows),
        )

    if mode is ReconciliationMode.STRICT_BLOCK:
        if unresolved_blocking:
            return PolicyDecision(LifecycleState.BLOCKED, True, False)
        lifecycle = LifecycleState.DEGRADED if any_unresolved else LifecycleState.READY
        return PolicyDecision(
            lifecycle=lifecycle,
            block_scope=False,
            recovery_complete=not any_unresolved,
        )

    if mode is ReconciliationMode.RECOVERY_MODE:
        if any_unresolved:
            return PolicyDecision(LifecycleState.BLOCKED, True, False)
        return PolicyDecision(LifecycleState.READY, False, True)

    raise ValueError(f"unsupported reconciliation mode: {mode!r}")
