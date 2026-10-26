from __future__ import annotations

from modular_strategy_position_exit_management import (
    ManagementAction,
    PositionExitManagementModule,
    PositionExitReason,
)


def test_flat_cancels_reduce_only_and_managed_orders(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    reduce_only = order_factory("reduce")
    tagged = order_factory("tagged", reduce_only=False, setup_id=None)
    setup_related = order_factory(
        "setup-related",
        reduce_only=False,
        tag="other",
        setup_id="setup-1",
    )
    unrelated = order_factory(
        "unrelated",
        reduce_only=False,
        tag="other",
        setup_id="other-setup",
    )
    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1),
            position=None,
            pending_orders=(unrelated, setup_related, tagged, reduce_only),
        )
    )
    assert [item.order_id for item in decision.cancellations] == [
        "reduce",
        "setup-related",
        "tagged",
    ]
    assert decision.features[0].value["action"] == ManagementAction.CANCEL_ORPHAN_ORDER.value
    assert decision.diagnostics["managed_pending_orders"] == 3


def test_cleanup_retry_is_rate_limited_and_retried(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule(cancel_retry_bars=2)
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    order = order_factory("orphan")
    first = module.on_bar(view_factory(1, candle=candle_factory(1), pending_orders=(order,)))
    blocked = module.on_bar(view_factory(2, candle=candle_factory(2), pending_orders=(order,)))
    retried = module.on_bar(view_factory(3, candle=candle_factory(3), pending_orders=(order,)))
    assert len(first.cancellations) == 1
    assert blocked.cancellations == ()
    assert len(retried.cancellations) == 1


def test_setup_context_is_retained_until_orphan_disappears(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule(
        cancel_reduce_only_orders_on_flat=False,
        cancel_managed_tag_orders_on_flat=False,
    )
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    setup_only = order_factory(
        "setup-only",
        reduce_only=False,
        tag="other",
        setup_id="setup-1",
    )
    first = module.on_bar(view_factory(1, candle=candle_factory(1), pending_orders=(setup_only,)))
    retry = module.on_bar(view_factory(2, candle=candle_factory(2), pending_orders=(setup_only,)))
    assert len(first.cancellations) == 1
    assert len(retry.cancellations) == 1
    assert retry.diagnostics["closed_position_id"] == "position-1"


def test_cleanup_state_is_cleared_after_orders_disappear(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1),
            pending_orders=(order_factory("orphan"),),
        )
    )
    flat = module.on_bar(view_factory(2, candle=candle_factory(2)))
    state = module.snapshot_state()
    assert flat.cancellations == ()
    assert state["active_position"] is None
    assert state["cancel_attempts"] == {}


def test_cancel_requests_are_deterministically_sorted_and_bounded(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule(
        max_tracked_cancels=3,
        max_cancel_requests_per_bar=2,
    )
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    orders = tuple(order_factory(order_id) for order_id in ("z", "a", "m", "b"))
    decision = module.on_bar(view_factory(1, candle=candle_factory(1), pending_orders=orders))
    assert [request.order_id for request in decision.cancellations] == ["a", "b"]
    assert len(module.snapshot_state()["cancel_attempts"]) <= 3
    assert decision.diagnostics["managed_pending_orders"] == 4


def test_cleanup_flags_can_disable_broad_matching(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule(
        cancel_reduce_only_orders_on_flat=False,
        cancel_managed_tag_orders_on_flat=False,
    )
    module.on_bar(
        view_factory(0, candle=candle_factory(0), position=position_factory(setup_id=None))
    )
    order = order_factory("not-managed", setup_id=None)
    decision = module.on_bar(view_factory(1, candle=candle_factory(1), pending_orders=(order,)))
    assert decision.cancellations == ()
    assert decision.features[0].value["reason"] == PositionExitReason.POSITION_FLAT.value


def test_idle_skip_is_opt_in(view_factory, candle_factory):
    quiet = PositionExitManagementModule()
    verbose = PositionExitManagementModule(emit_idle_skips=True)
    quiet_decision = quiet.on_bar(view_factory(0, candle=candle_factory(0)))
    verbose_decision = verbose.on_bar(view_factory(0, candle=candle_factory(0)))
    assert quiet_decision.skips == ()
    assert verbose_decision.skips == (PositionExitReason.NO_POSITION.value,)


def test_round_22_cleanup_selects_lowest_eligible_ids_without_full_sort(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule(
        cancel_retry_bars=3,
        max_cancel_requests_per_bar=2,
        max_tracked_cancels=8,
    )
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    orders = tuple(order_factory(order_id) for order_id in ("z", "a", "m", "b"))
    first = module.on_bar(view_factory(1, candle=candle_factory(1), pending_orders=orders))
    assert [item.order_id for item in first.cancellations] == ["a", "b"]
    second = module.on_bar(view_factory(2, candle=candle_factory(2), pending_orders=orders))
    assert [item.order_id for item in second.cancellations] == ["m", "z"]
    assert second.diagnostics["managed_pending_orders"] == 4
