from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import DecisionRouter, RouterConfiguration
from modular_strategy_trade_plan_router.enums import (
    ActivePlanPolicy,
    ExistingPositionPolicy,
    LongShortConflictPolicy,
    MultiSetupPolicy,
    PendingOrderPolicy,
    PlanVerdict,
    ReasonCode,
    ScalingAction,
    TradeAction,
    TradeSide,
)
from modular_strategy_trade_plan_router.errors import (
    PlanInvariantError,
    RouterConfigurationError,
    RouterInputError,
)
from modular_strategy_trade_plan_router.models import (
    AuditEvent,
    PendingOrderState,
    PositionState,
    RejectedCandidate,
)
from modular_strategy_trade_plan_router.router import (
    CandidateBundle,
    ConflictResolver,
    DecisionFreshnessValidator,
    PortfolioConstraintEvaluator,
    _attach_input_inventory,
    _coalesce_rejected_candidates,
    _inventory_row,
    _renumber_audit,
    _safe_future_time,
)

from conftest import (
    NOW,
    make_entry,
    make_package,
    make_portfolio,
    make_protection,
    make_scaling,
    make_sizing,
)


def bundle(
    *,
    candidate_id: str,
    setup_id: str,
    side: TradeSide,
    priority: int = 0,
    created_at=NOW,
    available_at=None,
) -> CandidateBundle:
    entry = make_entry(
        candidate_id=candidate_id,
        setup_id=setup_id,
        side=side,
        priority=priority,
        created_at=created_at,
        available_at=available_at or created_at,
    )
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    return CandidateBundle(entry, protection, sizing, None)


def package_for_bundles(
    *bundles: CandidateBundle,
    portfolio=None,
):
    return make_package(
        entries=tuple(item.entry for item in bundles),
        protections=tuple(item.protection for item in bundles),
        sizings=tuple(item.sizing for item in bundles),
        portfolio=portfolio or make_portfolio(),
    )


def position(
    *,
    side: TradeSide = TradeSide.LONG,
    position_id: str = "position-a",
    strategy_id: str | None = "strategy-a",
    setup_id: str | None = "setup-a",
    market: str | None = "usdt_perpetual",
    quantity: Decimal = Decimal("10"),
) -> PositionState:
    return PositionState(
        position_id=position_id,
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        average_entry=Decimal("100"),
        version=f"{position_id}-v1",
        market=market,
        strategy_id=strategy_id,
        setup_id=setup_id,
        initial_risk=Decimal("50"),
        remaining_risk=Decimal("40"),
        opened_at=NOW - timedelta(hours=1),
    )


def pending_order(
    *,
    side: TradeSide = TradeSide.LONG,
    setup_id: str | None = "setup-a",
    strategy_id: str | None = "strategy-a",
    plan_id: str | None = "plan-a",
    order_id: str = "order-a",
    reduce_only: bool = False,
) -> PendingOrderState:
    return PendingOrderState(
        order_intent_id=order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        side=side,
        order_type="limit",
        quantity=Decimal("1"),
        status="pending",
        version=f"{order_id}-v1",
        created_at=NOW - timedelta(minutes=1),
        price=Decimal("100"),
        reduce_only=reduce_only,
        strategy_id=strategy_id,
        setup_id=setup_id,
        expires_at=NOW + timedelta(minutes=5),
    )


def test_router_constructor_and_future_time_boundaries() -> None:
    with pytest.raises(RouterConfigurationError, match="RouterConfiguration"):
        DecisionRouter("bad")  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="plan TTL exceeds supported datetime range"):
        _safe_future_time(datetime.max.replace(tzinfo=NOW.tzinfo), 1)
    with pytest.raises(RouterInputError, match="RouterInputPackage"):
        DecisionRouter().route(object())  # type: ignore[arg-type]


def test_inventory_helpers_bound_and_deduplicate_audit() -> None:
    rows = tuple((str(index), f"hash-{index}") for index in range(300))
    inventory = _inventory_row(rows)
    assert inventory["count"] == 300
    assert inventory["decision_ids_omitted"] is True
    assert "decision_ids" not in inventory

    package = make_package()
    audit: list[AuditEvent] = []
    _attach_input_inventory(audit, package)
    assert len(audit) == 1
    assert audit[0].stage == "input_inventory"
    _attach_input_inventory(audit, package)
    assert len(audit) == 1


def test_same_bar_sizing_evidence_does_not_zero_executable_plan_ttl() -> None:
    package = make_package()
    sizing = package.sizings[0]
    object.__setattr__(sizing, "created_at", NOW)
    object.__setattr__(sizing, "expires_at", NOW)

    result = DecisionRouter().route(package)

    assert result.plan is not None
    assert result.plan.expires_at > NOW


def test_rejected_candidate_coalescing_merges_evidence() -> None:
    rows = [
        RejectedCandidate(
            "candidate-a",
            "setup-a",
            (ReasonCode.STALE_DECISION,),
            "stale",
        ),
        RejectedCandidate(
            "candidate-a",
            "setup-a",
            (ReasonCode.FUTURE_DECISION, ReasonCode.STALE_DECISION),
            "future",
        ),
        RejectedCandidate(
            "candidate-b",
            "setup-b",
            (ReasonCode.SIZING_REJECTED,),
            "sizing",
        ),
    ]
    merged = _coalesce_rejected_candidates(rows)
    assert len(merged) == 2
    assert merged[0].reason_codes == (
        ReasonCode.FUTURE_DECISION,
        ReasonCode.STALE_DECISION,
    )
    assert merged[0].detail == "2 deterministic rejection details merged; see audit trace"


def test_audit_renumbering_is_contiguous() -> None:
    events = (
        AuditEvent(10, "a", "accepted"),
        AuditEvent(30, "b", "rejected", ReasonCode.INVALID_INPUT),
    )
    result = _renumber_audit(events)
    assert tuple(item.sequence for item in result) == (1, 2)


def test_freshness_validator_entry_linked_and_portfolio_reasons() -> None:
    package = make_package()
    validator = DecisionFreshnessValidator(RouterConfiguration())
    entry = package.entries[0]

    object.__setattr__(entry, "available_at", NOW + timedelta(seconds=1))
    assert validator.entry_reason(entry, package) is ReasonCode.FUTURE_DECISION
    object.__setattr__(entry, "available_at", NOW)
    object.__setattr__(entry, "expires_at", NOW - timedelta(seconds=1))
    assert validator.entry_reason(entry, package) is ReasonCode.STALE_DECISION

    assert (
        validator.linked_reason(
            NOW + timedelta(seconds=1),
            NOW + timedelta(minutes=1),
            package,
        )
        is ReasonCode.FUTURE_DECISION
    )
    assert (
        validator.linked_reason(
            NOW - timedelta(minutes=2),
            NOW - timedelta(seconds=1),
            package,
        )
        is ReasonCode.STALE_DECISION
    )
    assert validator.linked_reason(NOW, None, package) is None
    assert validator.linked_reason(NOW, NOW, package) is None

    object.__setattr__(package.portfolio, "timestamp", NOW + timedelta(seconds=1))
    assert (
        validator.portfolio_reason(package.portfolio, package.as_of) is ReasonCode.FUTURE_DECISION
    )
    object.__setattr__(package.portfolio, "timestamp", NOW - timedelta(seconds=301))
    assert (
        validator.portfolio_reason(package.portfolio, package.as_of)
        is ReasonCode.PORTFOLIO_STATE_STALE
    )


def _resolve(config: RouterConfiguration, *bundles: CandidateBundle):
    rejected: list[RejectedCandidate] = []
    audit: list[AuditEvent] = []
    winner = ConflictResolver(config).resolve(
        list(bundles),
        "portfolio-a",
        rejected,
        audit,
    )
    return winner, rejected, audit


def test_conflict_resolver_empty_and_single_candidate() -> None:
    config = RouterConfiguration()
    winner, rejected, audit = _resolve(config)
    assert winner is None
    assert rejected == []
    assert audit == []

    only = bundle(candidate_id="a", setup_id="setup-a", side=TradeSide.LONG)
    winner, rejected, audit = _resolve(config, only)
    assert winner is only
    assert rejected == []
    assert audit[-1].outcome == "selected"


@pytest.mark.parametrize(
    ("policy", "expected"),
    (
        (LongShortConflictPolicy.REJECT_ALL, None),
        (LongShortConflictPolicy.HIGHER_PRIORITY, "short"),
        (LongShortConflictPolicy.EARLIEST_SIGNAL, "long"),
        (LongShortConflictPolicy.FRESHEST_SIGNAL, "short"),
    ),
)
def test_long_short_conflict_policies(policy, expected: str | None) -> None:
    long = bundle(
        candidate_id="long",
        setup_id="setup-long",
        side=TradeSide.LONG,
        priority=1,
        created_at=NOW - timedelta(minutes=2),
    )
    short = bundle(
        candidate_id="short",
        setup_id="setup-short",
        side=TradeSide.SHORT,
        priority=2,
        created_at=NOW - timedelta(minutes=1),
    )
    winner, rejected, audit = _resolve(
        RouterConfiguration(long_short_policy=policy),
        long,
        short,
    )
    if expected is None:
        assert winner is None
        assert {item.candidate_id for item in rejected} == {"long", "short"}
        assert all(ReasonCode.LONG_SHORT_CONFLICT in item.reason_codes for item in rejected)
    else:
        assert winner is not None
        assert winner.entry.candidate_id == expected
        assert len(rejected) == 1
        assert rejected[0].candidate_id != expected
        assert audit[-1].outcome == "selected"


def test_multi_setup_reject_and_highest_priority() -> None:
    first = bundle(
        candidate_id="a",
        setup_id="setup-a",
        side=TradeSide.LONG,
        priority=1,
    )
    second = bundle(
        candidate_id="b",
        setup_id="setup-b",
        side=TradeSide.LONG,
        priority=2,
    )
    winner, rejected, _audit = _resolve(
        RouterConfiguration(multi_setup_policy=MultiSetupPolicy.REJECT),
        first,
        second,
    )
    assert winner is None
    assert all(ReasonCode.SETUP_CONFLICT in item.reason_codes for item in rejected)

    winner, rejected, _audit = _resolve(
        RouterConfiguration(multi_setup_policy=MultiSetupPolicy.HIGHEST_PRIORITY),
        first,
        second,
    )
    assert winner is second
    assert rejected[0].candidate_id == "a"


def _evaluate(config: RouterConfiguration, bundle_value: CandidateBundle, portfolio):
    package = package_for_bundles(bundle_value, portfolio=portfolio)
    return PortfolioConstraintEvaluator(config).evaluate(bundle_value, package)


@pytest.mark.parametrize(
    ("override", "reason"),
    (
        ({"emergency_state": True}, ReasonCode.EMERGENCY_STATE_ACTIVE),
        ({"circuit_breaker_active": True}, ReasonCode.CIRCUIT_BREAKER_ACTIVE),
        ({"portfolio_locked": True}, ReasonCode.PORTFOLIO_LOCKED),
        ({"daily_loss_limit_active": True}, ReasonCode.DAILY_LOSS_LIMIT_ACTIVE),
        ({"drawdown_limit_active": True}, ReasonCode.DRAWDOWN_LIMIT_ACTIVE),
        ({"instrument_locks": ("BTCUSDT",)}, ReasonCode.INSTRUMENT_LOCKED),
        ({"strategy_locks": ("strategy-a",)}, ReasonCode.STRATEGY_LOCKED),
    ),
)
def test_portfolio_blockers_reject_new_exposure(override, reason: ReasonCode) -> None:
    selected = bundle(
        candidate_id="entry-a",
        setup_id="setup-a",
        side=TradeSide.LONG,
    )
    evaluation, actual_reason, detail = _evaluate(
        RouterConfiguration(),
        selected,
        make_portfolio(**override),
    )
    assert evaluation is None
    assert actual_reason is reason
    assert detail


def test_active_plan_policy_and_limits() -> None:
    selected = bundle(
        candidate_id="entry-a",
        setup_id="setup-a",
        side=TradeSide.LONG,
    )
    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(active_plan_policy=ActivePlanPolicy.REJECT_NEW_EXPOSURE),
        selected,
        make_portfolio(active_plan_ids=("plan-a",)),
    )
    assert evaluation is None
    assert reason is ReasonCode.ACTIVE_PLAN_CONFLICT

    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(active_plan_policy=ActivePlanPolicy.ALLOW_MULTIPLE),
        selected,
        make_portfolio(active_plan_ids=("plan-a",)),
    )
    assert evaluation is not None
    assert reason is None

    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(max_portfolio_risk_fraction=Decimal("0.001")),
        selected,
        make_portfolio(),
    )
    assert evaluation is None
    assert reason is ReasonCode.RISK_LIMIT_EXCEEDED

    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(max_symbol_notional_fraction=Decimal("0.05")),
        selected,
        make_portfolio(),
    )
    assert evaluation is None
    assert reason is ReasonCode.EXPOSURE_LIMIT_EXCEEDED


def test_existing_position_policies() -> None:
    selected = bundle(
        candidate_id="entry-a",
        setup_id="setup-a",
        side=TradeSide.LONG,
    )
    same_side = make_portfolio(positions=(position(),))
    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.REJECT),
        selected,
        same_side,
    )
    assert evaluation is None
    assert reason is ReasonCode.POSITION_CONFLICT

    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD),
        selected,
        same_side,
    )
    assert evaluation is not None
    assert reason is None
    assert evaluation.action is TradeAction.ADD_TO_POSITION

    opposite_portfolio = make_portfolio(positions=(position(side=TradeSide.SHORT),))
    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.CLOSE_THEN_REVERSE),
        selected,
        opposite_portfolio,
    )
    assert evaluation is not None
    assert reason is None
    assert evaluation.action is TradeAction.REVERSE_POSITION
    assert evaluation.sequencing_rules

    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.HEDGE),
        selected,
        opposite_portfolio,
    )
    assert evaluation is not None
    assert reason is None
    assert "hedge_mode_required" in evaluation.warnings


def test_pending_order_policies() -> None:
    selected = bundle(
        candidate_id="entry-a",
        setup_id="setup-a",
        side=TradeSide.LONG,
    )
    same = make_portfolio(pending_orders=(pending_order(),))
    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(pending_order_policy=PendingOrderPolicy.REJECT),
        selected,
        same,
    )
    assert evaluation is None
    assert reason is ReasonCode.PENDING_ORDER_CONFLICT

    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(pending_order_policy=PendingOrderPolicy.REPLACE_SAME_SETUP),
        selected,
        same,
    )
    assert evaluation is not None
    assert reason is None
    assert evaluation.action is TradeAction.REPLACE_PENDING_PLAN
    assert len(evaluation.cancellations) == 1

    opposite = make_portfolio(
        pending_orders=(pending_order(side=TradeSide.SHORT, setup_id="other"),)
    )
    evaluation, reason, _detail = _evaluate(
        RouterConfiguration(pending_order_policy=PendingOrderPolicy.CANCEL_OPPOSITE),
        selected,
        opposite,
    )
    assert evaluation is not None
    assert reason is None
    assert evaluation.cancellations


def test_end_to_end_missing_entry_input_limit_and_warning_escalation() -> None:
    rejected = DecisionRouter().route(make_package(entries=(), protections=(), sizings=()))
    assert rejected.verdict is PlanVerdict.REJECTED
    assert ReasonCode.MISSING_ENTRY_DECISION in rejected.reason_codes

    entry = make_entry()
    first = make_protection(entry, decision_id="protection-a")
    second = make_protection(entry, decision_id="protection-b")
    sizing = make_sizing(entry, first)
    limited = DecisionRouter(RouterConfiguration(max_decisions_per_type=1)).route(
        make_package(
            entries=(entry,),
            protections=(first, second),
            sizings=(sizing,),
        )
    )
    assert limited.verdict is PlanVerdict.REJECTED
    assert limited.reason_codes == (ReasonCode.INPUT_LIMIT_EXCEEDED,)

    selected = bundle(
        candidate_id="entry-a",
        setup_id="setup-a",
        side=TradeSide.LONG,
    )
    hedge_package = package_for_bundles(
        selected,
        portfolio=make_portfolio(positions=(position(side=TradeSide.SHORT),)),
    )
    escalated = DecisionRouter(
        RouterConfiguration(
            existing_position_policy=ExistingPositionPolicy.HEDGE,
            warning_escalation=True,
        )
    ).route(hedge_package)
    assert escalated.verdict is PlanVerdict.REJECTED
    assert escalated.reason_codes == (ReasonCode.INCOMPATIBLE_DECISIONS,)


def test_standalone_risk_reduction_accepts_and_rejects_key_shapes() -> None:
    target = position(quantity=Decimal("10"))
    portfolio = make_portfolio(positions=(target,))
    reduction = make_scaling(
        make_entry(),
        action=ScalingAction.REDUCE,
        quantity=Decimal("2"),
        position_id=target.position_id,
    )
    accepted = DecisionRouter().route(
        make_package(
            entries=(),
            protections=(),
            sizings=(),
            scalings=(reduction,),
            portfolio=portfolio,
        )
    )
    assert accepted.verdict is PlanVerdict.ACCEPTED
    assert accepted.plan is not None
    assert accepted.plan.action is TradeAction.REDUCE_POSITION
    assert accepted.plan.entry_intent.reduce_only is True

    orphan = DecisionRouter().route(
        make_package(
            entries=(),
            protections=(make_protection(make_entry()),),
            sizings=(),
            scalings=(reduction,),
            portfolio=portfolio,
        )
    )
    assert orphan.verdict is PlanVerdict.REJECTED
    assert orphan.reason_codes == (ReasonCode.INCOMPATIBLE_DECISIONS,)

    missing_position = DecisionRouter().route(
        make_package(
            entries=(),
            protections=(),
            sizings=(),
            scalings=(reduction,),
            portfolio=make_portfolio(),
        )
    )
    assert missing_position.verdict is PlanVerdict.REJECTED
    assert missing_position.reason_codes == (ReasonCode.INVALID_SCALING,)


def test_standalone_full_reduction_becomes_close() -> None:
    target = position(quantity=Decimal("2"))
    reduction = make_scaling(
        make_entry(),
        action=ScalingAction.REDUCE,
        quantity=Decimal("2"),
        position_id=target.position_id,
    )
    result = DecisionRouter().route(
        make_package(
            entries=(),
            protections=(),
            sizings=(),
            scalings=(reduction,),
            portfolio=make_portfolio(positions=(target,)),
        )
    )
    assert result.verdict is PlanVerdict.ACCEPTED
    assert result.plan is not None
    assert result.plan.action is TradeAction.CLOSE_POSITION
