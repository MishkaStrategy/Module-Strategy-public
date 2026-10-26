from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from modular_risk_supervisor import (
    RiskAdmissionBlocked,
    RiskGuardCode,
    RiskSupervisedPaperAdmission,
    SafetyDecision,
    SafetyState,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Action:
    value: str


@dataclass(frozen=True, slots=True)
class Plan:
    action: Action


class DecisionProvider:
    def __init__(self, decision: SafetyDecision | None = None, *, fail: bool = False) -> None:
        self._decision = decision
        self._fail = fail
        self.calls = 0

    def decision(self, *, now: datetime) -> SafetyDecision:
        self.calls += 1
        assert now == NOW
        if self._fail:
            raise RuntimeError("owner unavailable")
        assert self._decision is not None
        return self._decision


class RunnerDownstream:
    def __init__(self) -> None:
        self.calls = 0

    async def accept(self, envelope: object, **kwargs: object) -> str:
        self.calls += 1
        return "runner-accepted"


class PaperDownstream:
    def __init__(self) -> None:
        self.calls = 0

    def accept_plan(self, plan: object, **kwargs: object) -> str:
        self.calls += 1
        return "paper-accepted"


def decision(state: SafetyState, *codes: RiskGuardCode) -> SafetyDecision:
    return SafetyDecision(
        sequence=7,
        state=state,
        evaluated_at=NOW,
        guard_codes=tuple(codes),
        snapshot_id="snapshot-7",
        snapshot_hash="a" * 64,
        source_state_ids={"paper": "paper-v7"},
    )


@pytest.mark.asyncio
async def test_runner_boundary_delegates_new_exposure_only_when_owner_is_healthy() -> None:
    downstream = RunnerDownstream()
    provider = DecisionProvider(decision(SafetyState.HEALTHY))
    boundary = RiskSupervisedPaperAdmission(
        supervisor=provider,
        runner_execution=downstream,
    )

    result = await boundary.accept(Plan(Action("open_position")), now=NOW)

    assert result == "runner-accepted"
    assert provider.calls == 1
    assert downstream.calls == 1


@pytest.mark.asyncio
async def test_runner_boundary_missing_owner_fails_closed_before_side_effect() -> None:
    downstream = RunnerDownstream()
    boundary = RiskSupervisedPaperAdmission(
        supervisor=None,
        runner_execution=downstream,
    )

    with pytest.raises(RiskAdmissionBlocked) as exc:
        await boundary.accept(Plan(Action("add_to_position")), now=NOW)

    assert exc.value.safety_state == "unavailable"
    assert exc.value.reason_codes == ("supervisor_unavailable",)
    assert downstream.calls == 0


@pytest.mark.asyncio
async def test_runner_boundary_owner_exception_fails_closed_before_side_effect() -> None:
    downstream = RunnerDownstream()
    boundary = RiskSupervisedPaperAdmission(
        supervisor=DecisionProvider(fail=True),
        runner_execution=downstream,
    )

    with pytest.raises(RiskAdmissionBlocked) as exc:
        await boundary.accept(Plan(Action("open_position")), now=NOW)

    assert exc.value.reason_codes == ("supervisor_error",)
    assert downstream.calls == 0


@pytest.mark.asyncio
async def test_halt_reason_codes_and_decision_identity_cross_boundary_without_recalculation() -> (
    None
):
    downstream = RunnerDownstream()
    owner_decision = decision(
        SafetyState.HALT,
        RiskGuardCode.DRAWDOWN_LIMIT,
        RiskGuardCode.RECOVERY_REQUIRED,
    )
    boundary = RiskSupervisedPaperAdmission(
        supervisor=DecisionProvider(owner_decision),
        runner_execution=downstream,
    )

    with pytest.raises(RiskAdmissionBlocked) as exc:
        await boundary.accept(Plan(Action("open_position")), now=NOW)

    assert exc.value.safety_state == "halt"
    assert exc.value.reason_codes == ("drawdown_limit", "recovery_required")
    assert exc.value.decision_hash == owner_decision.decision_hash
    assert downstream.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "reduce_position",
        "close_position",
        "cancel_orders",
        "cancel_pending_plan",
        "replace_order",
        "update_protection",
    ],
)
async def test_halt_never_blocks_safe_existing_exposure_actions(action: str) -> None:
    downstream = RunnerDownstream()
    provider = DecisionProvider(decision(SafetyState.HALT, RiskGuardCode.OPERATOR_EMERGENCY_HALT))
    boundary = RiskSupervisedPaperAdmission(
        supervisor=provider,
        runner_execution=downstream,
    )

    result = await boundary.accept(Plan(Action(action)), now=NOW)

    assert result == "runner-accepted"
    assert provider.calls == 0
    assert downstream.calls == 1


def test_direct_paper_boundary_enforces_same_owner_decision() -> None:
    downstream = PaperDownstream()
    boundary = RiskSupervisedPaperAdmission(
        supervisor=DecisionProvider(
            decision(SafetyState.DEGRADED, RiskGuardCode.RESOURCE_PRESSURE)
        ),
        paper_admission=downstream,
    )

    with pytest.raises(RiskAdmissionBlocked) as exc:
        boundary.accept_plan(Plan(Action("open_position")), now=NOW)

    assert exc.value.safety_state == "degraded"
    assert exc.value.reason_codes == ("resource_pressure",)
    assert downstream.calls == 0


def test_direct_paper_close_remains_available_if_supervisor_is_absent() -> None:
    downstream = PaperDownstream()
    boundary = RiskSupervisedPaperAdmission(
        supervisor=None,
        paper_admission=downstream,
    )

    result = boundary.accept_plan(Plan(Action("close_position")), now=NOW)

    assert result == "paper-accepted"
    assert downstream.calls == 1


@pytest.mark.asyncio
async def test_unknown_action_fails_closed_without_querying_or_delegating() -> None:
    downstream = RunnerDownstream()
    provider = DecisionProvider(decision(SafetyState.HEALTHY))
    boundary = RiskSupervisedPaperAdmission(
        supervisor=provider,
        runner_execution=downstream,
    )

    with pytest.raises(RiskAdmissionBlocked) as exc:
        await boundary.accept(Plan(Action("future_magic_action")), now=NOW)

    assert exc.value.reason_codes == ("unclassified_action",)
    assert provider.calls == 0
    assert downstream.calls == 0


@pytest.mark.asyncio
async def test_missing_authoritative_time_fails_closed_before_owner_or_downstream() -> None:
    downstream = RunnerDownstream()
    provider = DecisionProvider(decision(SafetyState.HEALTHY))
    boundary = RiskSupervisedPaperAdmission(
        supervisor=provider,
        runner_execution=downstream,
    )

    with pytest.raises(RiskAdmissionBlocked) as exc:
        await boundary.accept(Plan(Action("open_position")))

    assert exc.value.reason_codes == ("missing_authoritative_time",)
    assert provider.calls == 0
    assert downstream.calls == 0
