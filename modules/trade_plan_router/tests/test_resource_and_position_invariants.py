from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    ExistingPositionPolicy,
    PlanVerdict,
    PositionIntent,
    ReasonCode,
    RouterConfiguration,
    ScalingAction,
    TradeAction,
    TradeSide,
)
from modular_strategy_trade_plan_router.errors import PlanInvariantError, RouterInputError
from modular_strategy_trade_plan_router.models import PendingOrderState, PositionState

from conftest import (
    NOW,
    make_entry,
    make_package,
    make_portfolio,
    make_protection,
    make_scaling,
    make_sizing,
)


def _position(
    *,
    position_id: str = "position-a",
    side: TradeSide = TradeSide.LONG,
    quantity: Decimal = Decimal("5"),
) -> PositionState:
    return PositionState(
        position_id=position_id,
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        average_entry=Decimal("100"),
        version=f"version-{position_id}",
        setup_id="setup-a",
    )


def _pending(index: int) -> PendingOrderState:
    return PendingOrderState(
        order_intent_id=f"order-{index}",
        plan_id=f"plan-{index}",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        order_type="limit",
        quantity=Decimal("1"),
        status="pending",
        version=f"order-v{index}",
        created_at=NOW,
        price=Decimal("100"),
        setup_id=f"setup-{index}",
    )


def _assert_limit_rejection(result, label: str) -> None:
    assert result.plan is None
    assert result.verdict is PlanVerdict.REJECTED
    assert result.reason_codes == (ReasonCode.INPUT_LIMIT_EXCEEDED,)
    assert result.audit_trace[0].stage == "input_limits"
    assert label in result.audit_trace[0].detail["detail"]


def test_each_router_decision_collection_limit_is_enforced():
    entry = make_entry()
    protection_a = make_protection(entry, decision_id="protection-a")
    protection_b = make_protection(entry, decision_id="protection-b")
    sizing_a = make_sizing(entry, protection_a, decision_id="sizing-a")
    sizing_b = make_sizing(entry, protection_a, decision_id="sizing-b")
    scaling_a = make_scaling(entry, action=ScalingAction.REDUCE, quantity=Decimal("1"))
    scaling_b = replace(
        scaling_a, decision_id="scaling-b", source_decision_id="scaling-b", input_hash=""
    )

    _assert_limit_rejection(
        DecisionRouter(RouterConfiguration(max_decisions_per_type=1)).route(
            make_package(
                entries=(entry,),
                protections=(protection_a, protection_b),
                sizings=(sizing_a,),
            )
        ),
        "protection decisions",
    )
    _assert_limit_rejection(
        DecisionRouter(RouterConfiguration(max_decisions_per_type=1)).route(
            make_package(
                entries=(entry,),
                protections=(protection_a,),
                sizings=(sizing_a, sizing_b),
            )
        ),
        "sizing decisions",
    )
    _assert_limit_rejection(
        DecisionRouter(RouterConfiguration(max_decisions_per_type=1)).route(
            make_package(
                entries=(entry,),
                protections=(protection_a,),
                sizings=(sizing_a,),
                scalings=(scaling_a, scaling_b),
                portfolio=make_portfolio(positions=(_position(),)),
            )
        ),
        "scaling decisions",
    )


def test_each_router_portfolio_collection_limit_is_enforced():
    positions = (_position(position_id="position-a"), _position(position_id="position-b"))
    _assert_limit_rejection(
        DecisionRouter(RouterConfiguration(max_portfolio_positions=1)).route(
            make_package(portfolio=make_portfolio(positions=positions))
        ),
        "portfolio positions",
    )
    _assert_limit_rejection(
        DecisionRouter(RouterConfiguration(max_pending_orders=1)).route(
            make_package(portfolio=make_portfolio(pending_orders=(_pending(1), _pending(2))))
        ),
        "pending orders",
    )


def _open_plan():
    result = DecisionRouter().route(make_package())
    assert result.plan is not None
    return result.plan


def _add_plan():
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD)
    ).route(make_package(portfolio=make_portfolio(positions=(_position(),))))
    assert result.plan is not None
    assert result.plan.action is TradeAction.ADD_TO_POSITION
    return result.plan


def _reduce_plan(*, quantity: Decimal = Decimal("2")):
    entry = make_entry()
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    scaling = make_scaling(entry, action=ScalingAction.REDUCE, quantity=quantity)
    result = DecisionRouter().route(
        make_package(
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
            scalings=(scaling,),
            portfolio=make_portfolio(positions=(_position(),)),
        )
    )
    assert result.plan is not None
    return result.plan


def _reverse_plan():
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.CLOSE_THEN_REVERSE)
    ).route(
        make_package(
            portfolio=make_portfolio(
                positions=(_position(side=TradeSide.SHORT),),
            )
        )
    )
    assert result.plan is not None
    return result.plan


def test_position_intent_model_rejects_invalid_side_and_quantity():
    with pytest.raises(RouterInputError, match="TradeSide"):
        PositionIntent("position", "v1", "long", Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(RouterInputError, match="positive"):
        PositionIntent("position", "v1", TradeSide.LONG, Decimal("0"))


def test_position_changing_plan_requires_explicit_target():
    with pytest.raises(PlanInvariantError, match="requires explicit position_intent"):
        replace(_add_plan(), position_intent=None)


def test_open_plan_cannot_target_existing_position():
    target = PositionIntent("position-a", "v1", TradeSide.LONG, Decimal("5"))
    with pytest.raises(PlanInvariantError, match="cannot target"):
        replace(_open_plan(), position_intent=target)


def test_same_side_position_action_rejects_wrong_target_side():
    plan = _add_plan()
    assert plan.position_intent is not None
    wrong = replace(plan.position_intent, current_side=TradeSide.SHORT)
    with pytest.raises(PlanInvariantError, match="target position side"):
        replace(plan, position_intent=wrong)


def test_reversal_requires_opposite_target_side():
    plan = _reverse_plan()
    assert plan.position_intent is not None
    wrong = replace(plan.position_intent, current_side=plan.side)
    with pytest.raises(PlanInvariantError, match="reversal target"):
        replace(plan, position_intent=wrong)


def test_reduce_quantity_must_be_strictly_below_position_quantity():
    plan = _reduce_plan()
    assert plan.position_intent is not None
    quantity = plan.position_intent.current_quantity
    with pytest.raises(PlanInvariantError, match="reduce quantity"):
        replace(
            plan,
            entry_intent=replace(plan.entry_intent, quantity=quantity),
            sizing_intent=replace(plan.sizing_intent, quantity=quantity),
        )


def test_full_reduction_composes_exact_close_quantity():
    plan = _reduce_plan(quantity=Decimal("5"))
    assert plan.action is TradeAction.CLOSE_POSITION
    assert plan.position_intent is not None
    assert plan.entry_intent.quantity == plan.position_intent.current_quantity


def test_plan_rejects_duplicate_or_blank_sequencing_rules():
    plan = _reverse_plan()
    with pytest.raises(PlanInvariantError, match="duplicates"):
        replace(plan, sequencing_rules=("close", "close"))
    with pytest.raises(PlanInvariantError, match="non-blank"):
        replace(plan, sequencing_rules=("",))


def test_plan_rejects_untyped_nested_collections():
    plan = _open_plan()
    with pytest.raises(PlanInvariantError, match="CancellationIntent"):
        replace(plan, cancellation_intents=("bad",))  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="ExecutionPrecondition"):
        replace(plan, preconditions=("bad",))  # type: ignore[arg-type]
    with pytest.raises(PlanInvariantError, match="ReasonCode"):
        replace(plan, reason_codes=("accepted",))  # type: ignore[arg-type]


def test_package_correlation_uses_compact_versioned_portfolio_identity():
    from modular_strategy_trade_plan_router.models import RouterInputPackage

    entry = make_entry()
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    positions = tuple(
        PositionState(
            position_id=f"position-{index}",
            symbol=f"ALT{index}USDT",
            side=TradeSide.LONG,
            quantity=Decimal("1"),
            average_entry=Decimal("1"),
            version=f"position-v{index}",
        )
        for index in range(500)
    )
    orders = tuple(
        PendingOrderState(
            order_intent_id=f"order-{index}",
            plan_id=None,
            symbol=f"ALT{index}USDT",
            side=TradeSide.LONG,
            order_type="limit",
            quantity=Decimal("1"),
            status="pending",
            version=f"order-v{index}",
            created_at=NOW,
            price=Decimal("1"),
        )
        for index in range(1_000)
    )
    snapshot = make_portfolio(positions=positions, pending_orders=orders)
    package = RouterInputPackage(
        as_of=NOW,
        portfolio=snapshot,
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
    )
    assert package.correlation_id.startswith("corr-")
    changed_version = replace(snapshot, version="portfolio-v2", input_hash="")
    changed = replace(package, portfolio=changed_version, correlation_id="")
    assert changed.correlation_id != package.correlation_id


def test_default_maximum_snapshot_can_be_content_hashed_without_node_overflow() -> None:
    positions = tuple(
        PositionState(
            position_id=f"max-position-{index}",
            symbol=f"ALT{index}USDT",
            side=TradeSide.LONG,
            quantity=Decimal("1"),
            average_entry=Decimal("1"),
            version=f"max-position-v{index}",
        )
        for index in range(5_000)
    )
    pending_orders = tuple(
        PendingOrderState(
            order_intent_id=f"max-order-{index}",
            plan_id=None,
            symbol=f"ALT{index}USDT",
            side=TradeSide.LONG,
            order_type="limit",
            quantity=Decimal("1"),
            status="pending",
            version=f"max-order-v{index}",
            created_at=NOW,
            price=Decimal("1"),
        )
        for index in range(10_000)
    )

    snapshot = make_portfolio(
        positions=positions,
        pending_orders=pending_orders,
    )

    assert len(snapshot.input_hash) == 64
    assert len(snapshot.positions) == 5_000
    assert len(snapshot.pending_orders) == 10_000


def test_package_rejects_caller_supplied_noncanonical_correlation_id() -> None:
    package = make_package()

    with pytest.raises(RouterInputError, match="correlation_id"):
        replace(package, correlation_id="corr-user-supplied")

    assert replace(package, correlation_id=package.correlation_id) == package
