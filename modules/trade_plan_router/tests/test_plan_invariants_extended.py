from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    ExistingPositionPolicy,
    ReasonCode,
    RouterConfiguration,
    ScalingAction,
    TradeAction,
    TradeSide,
)
from modular_strategy_trade_plan_router.errors import PlanInvariantError
from modular_strategy_trade_plan_router.models import (
    AuditEvent,
    EntryIntent,
    ExecutionPrecondition,
    PositionIntent,
    PositionState,
    ProtectionIntent,
    RejectedCandidate,
    RoutingResult,
    SizingIntent,
)

from conftest import make_package, make_portfolio, make_scaling


def _open_plan():
    result = DecisionRouter().route(make_package())
    assert result.plan is not None
    return result.plan


def _position(side: TradeSide = TradeSide.LONG, quantity: Decimal = Decimal("5")) -> PositionState:
    return PositionState(
        position_id="position-a",
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        average_entry=Decimal("100"),
        version="position-v1",
        setup_id="setup-a",
    )


def _add_plan():
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD)
    ).route(make_package(portfolio=make_portfolio(positions=(_position(),))))
    assert result.plan is not None
    return result.plan


def _reduce_plan():
    package = make_package()
    entry = package.entries[0]
    scaling = make_scaling(
        entry,
        action=ScalingAction.REDUCE,
        quantity=Decimal("2"),
    )
    result = DecisionRouter().route(
        replace(
            package,
            scalings=(scaling,),
            portfolio=make_portfolio(positions=(_position(),)),
            correlation_id="",
        )
    )
    assert result.plan is not None
    return result.plan


def _close_plan():
    package = make_package()
    entry = package.entries[0]
    scaling = make_scaling(
        entry,
        action=ScalingAction.REDUCE,
        quantity=Decimal("5"),
    )
    result = DecisionRouter().route(
        replace(
            package,
            scalings=(scaling,),
            portfolio=make_portfolio(positions=(_position(),)),
            correlation_id="",
        )
    )
    assert result.plan is not None
    return result.plan


def _reverse_plan():
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.CLOSE_THEN_REVERSE)
    ).route(make_package(portfolio=make_portfolio(positions=(_position(side=TradeSide.SHORT),))))
    assert result.plan is not None
    return result.plan


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": "2"}, "schema"),
        ({"action": "open_position"}, "TradeAction"),
        ({"side": "long"}, "TradeSide"),
        ({"action": TradeAction.NO_ACTION}, "non-executable"),
        ({"position_intent": "bad"}, "PositionIntent"),
        ({"action": TradeAction.ADD_TO_POSITION}, "requires explicit"),
        (
            {
                "position_intent": PositionIntent(
                    "position-a", "position-v1", TradeSide.LONG, Decimal("1")
                )
            },
            "cannot target",
        ),
        ({"entry_intent": "bad"}, "EntryIntent"),
        ({"protection_intent": "bad"}, "ProtectionIntent"),
        ({"sizing_intent": "bad"}, "SizingIntent"),
        ({"cancellation_intents": ("bad",)}, "CancellationIntent"),
        ({"sequencing_rules": ("",)}, "sequencing_rules"),
        ({"sequencing_rules": ("one", "one")}, "duplicates"),
        ({"preconditions": ("bad",)}, "ExecutionPrecondition"),
        ({"reason_codes": ()}, "reason_codes"),
        ({"reason_codes": ("accepted",)}, "reason_codes"),
        ({"warnings": ("",)}, "warnings"),
        ({"input_hashes": ()}, "input hashes"),
        ({"input_hashes": ("bad",)}, "SHA-256"),
        ({"rejected_candidates": ("bad",)}, "RejectedCandidate"),
        ({"audit_trace": ("bad",)}, "AuditEvent"),
        ({"content_hash": "bad"}, "SHA-256"),
        ({"configuration_hash": "bad"}, "SHA-256"),
    ],
)
def test_executable_plan_rejects_malformed_nested_state(changes, message):
    with pytest.raises(PlanInvariantError, match=message):
        replace(_open_plan(), **changes)


def test_add_plan_rejects_target_with_wrong_side():
    plan = _add_plan()
    wrong = PositionIntent(
        position_id="position-a",
        expected_position_version="position-v1",
        current_side=TradeSide.SHORT,
        current_quantity=Decimal("5"),
    )
    with pytest.raises(PlanInvariantError, match="side must match"):
        replace(plan, position_intent=wrong)


def test_reverse_plan_rejects_same_side_target():
    plan = _reverse_plan()
    wrong = PositionIntent(
        position_id="position-a",
        expected_position_version="position-v1",
        current_side=TradeSide.LONG,
        current_quantity=Decimal("5"),
    )
    with pytest.raises(PlanInvariantError, match="opposite"):
        replace(plan, position_intent=wrong)


def test_reduce_and_close_quantity_invariants_are_distinct():
    reduce_plan = _reduce_plan()
    reduce_entry = replace(reduce_plan.entry_intent, quantity=Decimal("5"))
    reduce_sizing = replace(reduce_plan.sizing_intent, quantity=Decimal("5"))
    with pytest.raises(PlanInvariantError, match="reduce quantity"):
        replace(
            reduce_plan,
            entry_intent=reduce_entry,
            sizing_intent=reduce_sizing,
        )

    close_plan = _close_plan()
    close_entry = replace(close_plan.entry_intent, quantity=Decimal("2"))
    close_sizing = replace(close_plan.sizing_intent, quantity=Decimal("2"))
    with pytest.raises(PlanInvariantError, match="close quantity"):
        replace(
            close_plan,
            entry_intent=close_entry,
            sizing_intent=close_sizing,
        )


def test_entry_and_sizing_quantity_must_match():
    plan = _open_plan()
    sizing = replace(plan.sizing_intent, quantity=Decimal("9"))
    with pytest.raises(PlanInvariantError, match="quantities must match"):
        replace(plan, sizing_intent=sizing)


def test_position_intent_and_nested_intent_validation():
    with pytest.raises(Exception, match="current_side"):
        PositionIntent("position", "v", "long", Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(Exception, match="current_quantity"):
        PositionIntent("position", "v", TradeSide.LONG, Decimal("0"))
    with pytest.raises(Exception, match="post_only"):
        EntryIntent(
            order_type="market",
            quantity=Decimal("1"),
            reference_entry_price=Decimal("100"),
            price=None,
            trigger_price=None,
            limit_price=None,
            post_only=True,
            ttl_bars=1,
        )
    with pytest.raises(Exception, match="trigger_price"):
        EntryIntent(
            order_type="stop_market",
            quantity=Decimal("1"),
            reference_entry_price=Decimal("100"),
            price=None,
            trigger_price=None,
            limit_price=None,
            post_only=False,
            ttl_bars=1,
        )
    with pytest.raises(Exception, match="trigger_price and limit_price"):
        EntryIntent(
            order_type="stop_limit",
            quantity=Decimal("1"),
            reference_entry_price=Decimal("100"),
            price=None,
            trigger_price=Decimal("100"),
            limit_price=None,
            post_only=False,
            ttl_bars=1,
        )
    with pytest.raises(Exception, match="TargetIntent"):
        ProtectionIntent(Decimal("95"), (), None, {}, {})
    with pytest.raises(Exception, match="max_holding"):
        ProtectionIntent(
            Decimal("95"),
            _open_plan().protection_intent.targets,
            0,
            {},
            {},
        )
    with pytest.raises(Exception, match="cannot exceed"):
        SizingIntent(
            Decimal("1"),
            Decimal("100"),
            Decimal("1"),
            Decimal("1.1"),
            Decimal("1"),
            (),
        )


def test_routing_result_rejects_malformed_contract_state():
    result = DecisionRouter().route(make_package())
    with pytest.raises(PlanInvariantError, match="schema"):
        replace(result, schema_version="2")
    with pytest.raises(PlanInvariantError, match="PlanVerdict"):
        replace(result, verdict="accepted")  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="ExecutableTradePlan"):
        replace(result, plan="bad")  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="requires a plan"):
        replace(result, plan=None)
    with pytest.raises(PlanInvariantError, match="ReasonCode"):
        replace(result, reason_codes=("accepted",))  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="RejectedCandidate"):
        replace(result, rejected_candidates=("bad",))  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="AuditEvent"):
        replace(result, audit_trace=("bad",))  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="warnings"):
        replace(result, warnings=("",))
    with pytest.raises(PlanInvariantError, match="SHA-256"):
        replace(result, configuration_hash="bad")

    rejected = RoutingResult.create(
        verdict=result.verdict.REJECTED,
        created_at=result.created_at,
        portfolio_id=result.portfolio_id,
        configuration_hash=result.configuration_hash,
        plan=None,
        reason_codes=(ReasonCode.INVALID_INPUT,),
        rejected_candidates=(),
        audit_trace=(AuditEvent(1, "input", "rejected", ReasonCode.INVALID_INPUT),),
        warnings=(),
    )
    with pytest.raises(PlanInvariantError, match="cannot contain"):
        replace(rejected, plan=result.plan)


def test_valid_plan_precondition_remains_typed():
    plan = _open_plan()
    assert all(isinstance(item, ExecutionPrecondition) for item in plan.preconditions)


def test_routing_result_identity_is_compact_and_replay_stable():
    result = DecisionRouter().route(make_package())
    assert result.plan is not None
    identity = dict(result.identity_mapping())
    assert identity["plan_identity"] == {
        "plan_id": result.plan.plan_id,
        "content_hash": result.plan.content_hash,
        "audit_hash": result.plan.audit_hash,
    }
    assert identity["rejection_hash"] is None
    assert "plan" not in identity
    assert result.result_id == DecisionRouter().route(make_package()).result_id


def test_rejected_routing_result_identity_hashes_rejection_evidence():
    package = make_package(entries=(), protections=(), sizings=())
    result = DecisionRouter().route(package)
    assert result.plan is None
    identity = dict(result.identity_mapping())
    assert identity["plan_identity"] is None
    assert isinstance(identity["rejection_hash"], str)
    assert len(identity["rejection_hash"]) == 64
    assert "audit_trace" not in identity


def test_domain_plan_and_result_require_contiguous_audit_sequences():
    result = DecisionRouter().route(make_package())
    assert result.plan is not None
    reordered = tuple(reversed(result.plan.audit_trace))

    with pytest.raises(PlanInvariantError, match="contiguous and start at 1"):
        replace(result.plan, audit_trace=reordered)
    with pytest.raises(PlanInvariantError, match="contiguous and start at 1"):
        replace(result, audit_trace=tuple(reversed(result.audit_trace)))


def test_plan_requires_unique_consistent_state_preconditions() -> None:
    plan = _open_plan()
    assert plan is not None
    without_portfolio_hash = tuple(
        item for item in plan.preconditions if item.kind != "portfolio_content_hash"
    )
    with pytest.raises(PlanInvariantError, match="missing execution preconditions"):
        replace(plan, preconditions=without_portfolio_hash)

    duplicate = (*plan.preconditions, plan.preconditions[0])
    with pytest.raises(PlanInvariantError, match="duplicate execution precondition"):
        replace(plan, preconditions=duplicate)

    conflicting = tuple(
        ExecutionPrecondition(item.kind, "different") if item.kind == "portfolio_version" else item
        for item in plan.preconditions
    )
    with pytest.raises(PlanInvariantError, match="conflicts with plan state"):
        replace(plan, preconditions=conflicting)


def test_plan_and_result_reject_duplicate_rejected_candidate_identity() -> None:
    plan = _open_plan()
    rejected = RejectedCandidate(
        candidate_id="duplicate-rejected-id",
        setup_id="setup-a",
        reason_codes=(ReasonCode.INCOMPATIBLE_DECISIONS,),
    )
    with pytest.raises(PlanInvariantError, match="unique by candidate_id"):
        replace(plan, rejected_candidates=(rejected, rejected))

    result = DecisionRouter().route(make_package(entries=(), protections=(), sizings=()))
    with pytest.raises(PlanInvariantError, match="unique by candidate_id"):
        replace(result, rejected_candidates=(rejected, rejected))
