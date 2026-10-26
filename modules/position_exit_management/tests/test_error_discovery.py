from __future__ import annotations

from copy import deepcopy

import pytest

from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitReason,
)


def test_late_valid_stop_does_not_reconstruct_unknown_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(breakeven_trigger_r=1)
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(stop_loss=None),
        )
    )

    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=95),
        )
    )

    assert decision.position_updates == ()
    assert decision.skips == (PositionExitReason.MISSING_INITIAL_RISK.value,)
    active = module.snapshot_state()["active_position"]
    assert active["initial_stop"] is None
    assert active["initial_risk_per_unit"] is None
    assert active["last_stop"] == "95"


def test_default_fast_path_does_not_hide_data_gap(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))

    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1),
            position=position_factory(),
            data_gap=True,
        )
    )

    assert decision.skips == (PositionExitReason.DATA_GAP.value,)
    assert decision.features
    assert decision.features[0].value["reason"] == PositionExitReason.DATA_GAP.value


def test_gap_extreme_does_not_contaminate_future_trailing_state(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.10",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    position = position_factory(stop_loss=99)
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=101, low=99, close=100),
            position=position,
        )
    )

    gap_decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=100, high=200, low=99, close=100),
            position=position,
            data_gap=True,
        )
    )
    assert gap_decision.position_updates == ()

    next_decision = module.on_bar(
        view_factory(
            2,
            candle=candle_factory(2, open_=105, high=110, low=104, close=109),
            position=position,
        )
    )
    assert next_decision.position_updates == ()
    assert module.snapshot_state()["active_position"]["favorable_extreme"] == "110"


def test_same_bar_changed_position_snapshot_is_conflicting_duplicate(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    candle = candle_factory(0)
    module.on_bar(view_factory(0, candle=candle, position=position_factory(quantity=4)))

    decision = module.on_bar(view_factory(0, candle=candle, position=position_factory(quantity=3)))

    assert decision.skips == (PositionExitReason.CONFLICTING_DUPLICATE.value,)


def test_same_bar_changed_exit_pending_is_conflicting_duplicate(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    candle = candle_factory(0)
    position = position_factory()
    module.on_bar(view_factory(0, candle=candle, position=position, exit_pending=False))

    decision = module.on_bar(view_factory(0, candle=candle, position=position, exit_pending=True))

    assert decision.skips == (PositionExitReason.CONFLICTING_DUPLICATE.value,)


def test_same_bar_changed_pending_orders_is_conflicting_duplicate(
    view_factory,
    candle_factory,
    order_factory,
):
    module = PositionExitManagementModule()
    candle = candle_factory(0)
    module.on_bar(view_factory(0, candle=candle, position=None, pending_orders=()))

    decision = module.on_bar(
        view_factory(
            0,
            candle=candle,
            position=None,
            pending_orders=(order_factory("order-1"),),
        )
    )

    assert decision.skips == (PositionExitReason.CONFLICTING_DUPLICATE.value,)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(last_event_fingerprint=None),
        lambda state: state.update(last_close_time=None),
        lambda state: state.update(stream_symbol=None, stream_timeframe=None),
    ],
)
def test_restore_rejects_semantically_partial_runtime_state(
    view_factory,
    candle_factory,
    position_factory,
    mutation,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    mutation(broken)

    with pytest.raises(ValueError):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_invalid_position_snapshot_is_rejected_before_cursor_mutation(
    view_factory,
    candle_factory,
    position_factory,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    invalid = replace(position_factory(), quantity=0)
    before = module.snapshot_state()

    with pytest.raises(ValueError, match="position.quantity"):
        module.on_bar(view_factory(0, candle=candle_factory(0), position=invalid))

    assert module.snapshot_state() == before
    accepted = module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    assert accepted.diagnostics["bars_managed"] == 1


def test_naive_position_time_is_rejected_before_cursor_mutation(
    view_factory,
    candle_factory,
    position_factory,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    invalid = replace(
        position_factory(), entry_time=position_factory().entry_time.replace(tzinfo=None)
    )
    before = module.snapshot_state()

    with pytest.raises(ValueError, match="position.entry_time"):
        module.on_bar(view_factory(0, candle=candle_factory(0), position=invalid))

    assert module.snapshot_state() == before


def test_future_position_is_rejected_without_creating_runtime_state(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(trailing_mode="percent", trailing_activation_r=0)
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=150, high=200, low=140, close=180),
            position=position_factory(entry_minutes=30),
        )
    )

    assert decision.skips == (PositionExitReason.FUTURE_ENTRY_TIME.value,)
    assert module.snapshot_state()["active_position"] is None

    accepted = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1),
            position=position_factory(entry_minutes=0),
        )
    )
    assert accepted.diagnostics["bars_managed"] == 1


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("id", " ", ValueError),
        ("id", " position-1 ", ValueError),
        ("side", "long", TypeError),
        ("quantity", True, TypeError),
        ("quantity", object(), TypeError),
        ("quantity", float("nan"), ValueError),
        ("entry_price", 0, ValueError),
        ("entry_time", "2026-01-01T00:00:00Z", TypeError),
        ("stop_loss", 0, ValueError),
        ("take_profit", float("inf"), ValueError),
        ("setup_id", " ", ValueError),
    ],
)
def test_malformed_position_fields_fail_before_state_mutation(
    view_factory,
    candle_factory,
    position_factory,
    field,
    value,
    error,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    invalid = replace(position_factory(), **{field: value})
    before = module.snapshot_state()
    with pytest.raises(error):
        module.on_bar(view_factory(0, candle=candle_factory(0), position=invalid))
    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("index", True, ValueError),
        ("index", -1, ValueError),
        ("exit_pending", 1, TypeError),
        ("data_gap", 1, TypeError),
        ("pending_orders", [], TypeError),
    ],
)
def test_malformed_view_fields_fail_before_state_mutation(
    view_factory,
    candle_factory,
    field,
    value,
    error,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    invalid = replace(view_factory(0, candle=candle_factory(0)), **{field: value})
    before = module.snapshot_state()
    with pytest.raises(error):
        module.on_bar(invalid)
    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("id", " ", ValueError),
        ("reduce_only", 1, TypeError),
        ("tag", 1, TypeError),
        ("setup_id", " ", ValueError),
    ],
)
def test_malformed_pending_order_fields_fail_before_state_mutation(
    view_factory,
    candle_factory,
    order_factory,
    field,
    value,
    error,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    invalid_order = replace(order_factory("order-1"), **{field: value})
    before = module.snapshot_state()
    with pytest.raises(error):
        module.on_bar(
            view_factory(
                0,
                candle=candle_factory(0),
                pending_orders=(invalid_order,),
            )
        )
    assert module.snapshot_state() == before


def test_restore_rejects_bound_stream_without_cursor(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["last_close_time"] = None
    broken["last_event_fingerprint"] = None
    with pytest.raises(ValueError, match="event cursor"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_restore_rejects_negative_cancel_attempt_index(
    view_factory,
    candle_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0)))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["cancel_attempts"] = {"order-1": -1}
    with pytest.raises(ValueError, match="negative"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_event_fingerprint_is_fixed_size_with_large_order_set(
    view_factory,
    candle_factory,
    order_factory,
):
    module = PositionExitManagementModule(max_tracked_cancels=1024)
    orders = tuple(order_factory(f"order-{index:04d}") for index in range(1000))
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            pending_orders=orders,
        )
    )
    fingerprint = module.snapshot_state()["last_event_fingerprint"]
    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")


def test_legacy_event_fingerprint_restores_with_fail_closed_replay_barrier(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    view = view_factory(0, candle=candle_factory(0), position=position_factory())
    module.on_bar(view)
    legacy = deepcopy(module.snapshot_state())
    candle = view.current
    legacy["last_event_fingerprint"] = (
        f"{view.index}|{candle.symbol}|{candle.timeframe}|"
        f"{candle.timestamp.isoformat()}|{candle.open!r}|{candle.high!r}|"
        f"{candle.low!r}|{candle.close!r}|{candle.volume!r}|0"
    )

    restored = PositionExitManagementModule()
    restored.restore_state(legacy)
    decision = restored.on_bar(view)
    assert decision.skips == (PositionExitReason.CONFLICTING_DUPLICATE.value,)


def test_active_position_duplicate_ignores_unconsumed_pending_order_changes(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    module = PositionExitManagementModule()
    candle = candle_factory(0)
    position = position_factory()
    module.on_bar(
        view_factory(
            0,
            candle=candle,
            position=position,
            pending_orders=(),
        )
    )

    decision = module.on_bar(
        view_factory(
            0,
            candle=candle,
            position=position,
            pending_orders=(order_factory("unconsumed-order"),),
        )
    )

    assert decision.skips == (PositionExitReason.DUPLICATE_EVENT.value,)


def test_breakeven_request_is_retried_after_retracement_when_snapshot_did_not_apply_it(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=95),
        )
    )
    assert first.position_updates[0].stop_loss == pytest.approx(100)

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=100, high=101, low=98, close=99),
            position=position_factory(stop_loss=95),
        )
    )
    assert retry.position_updates[0].stop_loss < 99
    assert retry.position_updates[0].stop_loss > 95
    assert module.snapshot_state()["active_position"]["breakeven_applied"] is True


def test_trailing_request_is_retried_after_retracement_when_snapshot_did_not_apply_it(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=1,
        trailing_distance_pct="0.10",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=108, high=120, low=107, close=115),
            position=position_factory(stop_loss=95),
        )
    )
    assert first.position_updates[0].stop_loss == pytest.approx(108)

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=101, high=102, low=99, close=100),
            position=position_factory(stop_loss=95),
        )
    )
    assert retry.position_updates[0].stop_loss < 100
    assert retry.position_updates[0].stop_loss > 95
    assert module.snapshot_state()["active_position"]["trailing_active"] is True


def test_pending_partial_does_not_close_scale_in_quantity_when_reset_is_disabled(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        reset_on_scale_in=False,
    )
    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=2, stop_loss=95),
        )
    )
    assert first.closes[0].quantity == pytest.approx(1)

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4, stop_loss=95),
        )
    )
    assert retry.closes[0].quantity == pytest.approx(1)
    active = module.snapshot_state()["active_position"]
    assert active["pending_partial_target_quantity"] == "3"


def test_combined_stop_action_reports_the_component_that_selected_the_price(
    view_factory,
    candle_factory,
    position_factory,
):
    from modular_strategy_position_exit_management import ManagementAction

    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        breakeven_offset_r=2,
        trailing_mode="r_multiple",
        trailing_activation_r=1,
        trailing_distance_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
        emit_status_feature=True,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=111, high=112, low=110, close=111),
            position=position_factory(stop_loss=95),
        )
    )
    assert decision.position_updates[0].stop_loss == pytest.approx(110)
    assert decision.features[0].value["action"] == ManagementAction.BREAKEVEN.value
    assert decision.position_updates[0].reason.endswith(ManagementAction.BREAKEVEN.value)


def test_restore_rejects_noncontiguous_completed_partial_stages(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[
            {"trigger_r": 1, "fraction": 0.25},
            {"trigger_r": 2, "fraction": 0.25},
        ]
    )
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["active_position"]["completed_partial_indices"] = (1,)
    with pytest.raises(ValueError, match="contiguous prefix"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_restore_rejects_pending_partial_target_not_below_last_quantity(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    active = broken["active_position"]
    active["pending_partial_index"] = 0
    active["pending_partial_target_quantity"] = active["last_seen_quantity"]
    with pytest.raises(ValueError, match="below last seen"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_on_trade_rejects_non_boolean_closed_flag_before_mutation(
    view_factory,
    candle_factory,
    position_factory,
):
    from dataclasses import replace
    from datetime import timedelta

    from modular_core import PositionSide, Trade

    module = PositionExitManagementModule(max_holding_minutes=5)
    position = position_factory()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    before = module.snapshot_state()
    trade = Trade(
        side=PositionSide.LONG,
        quantity=1,
        entry_time=position.entry_time,
        exit_time=position.entry_time + timedelta(minutes=5),
        entry_price=100,
        exit_price=101,
        gross_pnl=1,
        fees=0,
        net_pnl=1,
        exit_reason="test",
        position_id=position.id,
    )
    malformed = replace(trade, position_closed=1)
    with pytest.raises(TypeError, match="position_closed"):
        module.on_trade(malformed)
    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    "completed",
    [(0, 0), (1, 0)],
)
def test_restore_rejects_noncanonical_completed_partial_array(
    view_factory,
    candle_factory,
    position_factory,
    completed,
):
    module = PositionExitManagementModule(
        partial_exits=[
            {"trigger_r": 1, "fraction": 0.25},
            {"trigger_r": 2, "fraction": 0.25},
        ]
    )
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["active_position"]["completed_partial_indices"] = completed
    with pytest.raises(ValueError, match="sorted and unique"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_restore_rejects_active_position_with_zero_managed_bars(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["active_position"]["bars_managed"] = 0
    with pytest.raises(ValueError, match="bars_managed must be positive"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("full_close_requested", "time-exit"),
        ("breakeven_applied", "breakeven configuration"),
        ("trailing_active", "trailing configuration"),
    ],
)
def test_restore_rejects_runtime_flags_impossible_for_configuration(
    view_factory,
    candle_factory,
    position_factory,
    field,
    message,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["active_position"][field] = True
    with pytest.raises(ValueError, match=message):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_duplicate_pending_order_ids_fail_before_cursor_mutation(
    view_factory,
    candle_factory,
    order_factory,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    first = order_factory("duplicate-order")
    second = replace(first, tag="position-exit:other")
    before = module.snapshot_state()
    with pytest.raises(ValueError, match="pending_order.id values must be unique"):
        module.on_bar(
            view_factory(
                0,
                candle=candle_factory(0),
                pending_orders=(first, second),
            )
        )
    assert module.snapshot_state() == before


def test_cancel_retry_state_is_removed_when_order_becomes_unmanaged(
    view_factory,
    candle_factory,
    order_factory,
):
    from dataclasses import replace

    module = PositionExitManagementModule(
        cancel_reduce_only_orders_on_flat=True,
        cancel_managed_tag_orders_on_flat=False,
    )
    managed = order_factory("order-1", reduce_only=True, tag="external")
    first = module.on_bar(view_factory(0, candle=candle_factory(0), pending_orders=(managed,)))
    assert len(first.cancellations) == 1
    assert module.snapshot_state()["cancel_attempts"] == {"order-1": 0}

    unmanaged = replace(managed, reduce_only=False)
    second = module.on_bar(view_factory(1, candle=candle_factory(1), pending_orders=(unmanaged,)))
    assert second.cancellations == ()
    state = module.snapshot_state()
    assert state["cancel_attempts"] == {}
    assert state["active_position"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "p" * 257),
        ("setup_id", "s" * 257),
    ],
)
def test_oversized_position_identifiers_fail_before_state_mutation(
    view_factory,
    candle_factory,
    position_factory,
    field,
    value,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    invalid = replace(position_factory(), **{field: value})
    before = module.snapshot_state()
    with pytest.raises(ValueError, match="exceeds 256"):
        module.on_bar(view_factory(0, candle=candle_factory(0), position=invalid))
    assert module.snapshot_state() == before


def test_oversized_pending_order_identifier_and_tag_are_rejected(
    view_factory,
    candle_factory,
    order_factory,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    for invalid in (
        replace(order_factory("order-1"), id="o" * 257),
        replace(order_factory("order-1"), tag="t" * 4097),
    ):
        before = module.snapshot_state()
        with pytest.raises(ValueError, match="exceeds"):
            module.on_bar(view_factory(0, candle=candle_factory(0), pending_orders=(invalid,)))
        assert module.snapshot_state() == before


def test_oversized_managed_order_prefix_is_rejected():
    with pytest.raises(ValueError, match="exceeds 256"):
        PositionExitManagementModule(managed_order_tag_prefix="x" * 257)


def test_restore_rejects_oversized_position_identifier(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    valid = module.snapshot_state()
    broken = deepcopy(valid)
    broken["active_position"]["position_id"] = "p" * 257
    with pytest.raises(ValueError, match="exceeds 256"):
        module.restore_state(broken)
    assert module.snapshot_state() == valid


def test_round_26_duplicate_order_error_does_not_echo_identifier(
    view_factory,
    candle_factory,
    order_factory,
):
    sensitive_id = "customer-account-private-order"
    order = order_factory(sensitive_id)
    module = PositionExitManagementModule()
    with pytest.raises(ValueError) as error:
        module.on_bar(
            view_factory(
                0,
                candle=candle_factory(0),
                pending_orders=(order, order),
            )
        )
    assert sensitive_id not in str(error.value)
    assert str(error.value) == "pending_order.id values must be unique"
    assert module.snapshot_state()["last_close_time"] is None
