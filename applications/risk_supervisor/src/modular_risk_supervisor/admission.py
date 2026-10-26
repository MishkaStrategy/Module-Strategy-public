from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from .contracts import SafetyDecision, SafetyState, utc

_RISK_INCREASING_ACTIONS = frozenset(
    {
        "open_position",
        "add_to_position",
        "reverse_position",
    }
)
_RISK_REDUCING_ACTIONS = frozenset(
    {
        "reduce_position",
        "close_position",
        "cancel_orders",
        "cancel_pending_plan",
        "replace_order",
        "update_protection",
    }
)


@runtime_checkable
class SafetyDecisionProvider(Protocol):
    def decision(self, *, now: datetime) -> SafetyDecision: ...


@runtime_checkable
class RunnerPlanExecutionBoundary(Protocol):
    def accept(self, envelope: object, **kwargs: object) -> object: ...


@runtime_checkable
class PaperPlanAdmissionBoundary(Protocol):
    def accept_plan(self, plan: object, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class RiskAdmissionBlocked(RuntimeError):
    action: str
    safety_state: str
    reason_codes: tuple[str, ...]
    decision_hash: str | None

    def __str__(self) -> str:
        reasons = ",".join(self.reason_codes) if self.reason_codes else "decision_unavailable"
        return f"system safety blocks {self.action}: state={self.safety_state} reasons={reasons}"


class RiskSupervisedPaperAdmission:
    """Read-only safety gate for Runner and direct Paper admission.

    It owns no portfolio/order/fill/runner state and performs no guard calculation.
    Risk-increasing actions require the authoritative current Risk Supervisor decision.
    Risk-reducing, cancellation and protection actions remain available during HALT.
    """

    def __init__(
        self,
        *,
        supervisor: SafetyDecisionProvider | None,
        runner_execution: RunnerPlanExecutionBoundary | None = None,
        paper_admission: PaperPlanAdmissionBoundary | None = None,
    ) -> None:
        if supervisor is not None and not isinstance(supervisor, SafetyDecisionProvider):
            raise TypeError("supervisor must implement decision(now=...)")
        if runner_execution is None and paper_admission is None:
            raise ValueError("at least one downstream admission boundary is required")
        if runner_execution is not None and not isinstance(
            runner_execution, RunnerPlanExecutionBoundary
        ):
            raise TypeError("runner_execution must implement accept()")
        if paper_admission is not None and not isinstance(
            paper_admission, PaperPlanAdmissionBoundary
        ):
            raise TypeError("paper_admission must implement accept_plan()")
        self._supervisor = supervisor
        self._runner_execution = runner_execution
        self._paper_admission = paper_admission

    async def accept(self, envelope: object, **kwargs: object) -> object:
        downstream = self._runner_execution
        if downstream is None:
            raise RuntimeError("runner execution boundary is not configured")
        now = _required_now(kwargs)
        self._enforce(envelope, now=now)
        result = downstream.accept(envelope, **kwargs)
        return await _maybe_await(result)

    def accept_plan(self, plan: object, **kwargs: object) -> object:
        downstream = self._paper_admission
        if downstream is None:
            raise RuntimeError("paper admission boundary is not configured")
        now = _required_now(kwargs)
        self._enforce(plan, now=now)
        result = downstream.accept_plan(plan, **kwargs)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("direct Paper admission must remain synchronous")
        return result

    def _enforce(self, plan: object, *, now: datetime) -> None:
        action = _action_value(plan)
        if action in _RISK_REDUCING_ACTIONS:
            return
        if action not in _RISK_INCREASING_ACTIONS:
            raise RiskAdmissionBlocked(
                action=action,
                safety_state="unknown",
                reason_codes=("unclassified_action",),
                decision_hash=None,
            )
        supervisor = self._supervisor
        if supervisor is None:
            raise RiskAdmissionBlocked(
                action=action,
                safety_state="unavailable",
                reason_codes=("supervisor_unavailable",),
                decision_hash=None,
            )
        try:
            decision = supervisor.decision(now=now)
        except Exception as exc:
            raise RiskAdmissionBlocked(
                action=action,
                safety_state="unavailable",
                reason_codes=("supervisor_error",),
                decision_hash=None,
            ) from exc
        if not isinstance(decision, SafetyDecision):
            raise RiskAdmissionBlocked(
                action=action,
                safety_state="invalid",
                reason_codes=("invalid_supervisor_decision",),
                decision_hash=None,
            )
        if decision.state is not SafetyState.HEALTHY or not decision.allow_risk_increase:
            raise RiskAdmissionBlocked(
                action=action,
                safety_state=decision.state.value,
                reason_codes=tuple(code.value for code in decision.guard_codes),
                decision_hash=decision.decision_hash,
            )


def _required_now(kwargs: Mapping[str, object]) -> datetime:
    value = kwargs.get("now")
    if not isinstance(value, datetime):
        raise RiskAdmissionBlocked(
            action="unknown",
            safety_state="invalid",
            reason_codes=("missing_authoritative_time",),
            decision_hash=None,
        )
    return utc(value, "now")


def _action_value(plan: object) -> str:
    raw = getattr(plan, "action", None)
    value = getattr(raw, "value", raw)
    if not isinstance(value, str) or not value.strip():
        raise RiskAdmissionBlocked(
            action="unknown",
            safety_state="invalid",
            reason_codes=("missing_action",),
            decision_hash=None,
        )
    return value.strip().lower()


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value
