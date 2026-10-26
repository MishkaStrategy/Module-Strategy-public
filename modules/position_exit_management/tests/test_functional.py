from __future__ import annotations

import pytest
from modular_core import Candle, PositionSide

from modular_strategy_position_exit_management import (
    STATUS_FEATURE_KEY,
    ManagementAction,
    PositionExitManagementModule,
    PositionExitReason,
)


def _next_candle(candle_factory, index: int, close: float, *, side: PositionSide):
    if side is PositionSide.LONG:
        return candle_factory(
            index,
            open_=close - 1,
            high=close + 1,
            low=close - 2,
            close=close,
        )
    return candle_factory(
        index,
        open_=close + 1,
        high=close + 2,
        low=close - 1,
        close=close,
    )


@pytest.mark.parametrize(
    ("side", "entry", "stop", "trigger_close", "expected_quantity"),
    [
        (PositionSide.LONG, 100.0, 95.0, 105.0, 2.0),
        (PositionSide.SHORT, 100.0, 105.0, 95.0, 2.0),
    ],
)
def test_partial_exit_is_buy_sell_symmetric(
    view_factory,
    candle_factory,
    position_factory,
    side,
    entry,
    stop,
    trigger_close,
    expected_quantity,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    position = position_factory(
        side=side,
        entry_price=entry,
        stop_loss=stop,
        quantity=4,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=_next_candle(candle_factory, 0, trigger_close, side=side),
            position=position,
        )
    )
    assert len(decision.closes) == 1
    assert decision.closes[0].quantity == pytest.approx(expected_quantity)
    assert decision.closes[0].reason.endswith("stage_1")
    assert decision.features[0].key == STATUS_FEATURE_KEY
    assert decision.features[0].value["action"] == ManagementAction.PARTIAL_EXIT.value


def test_partial_fraction_is_based_on_initial_quantity(
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
    position = position_factory(quantity=4)
    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position,
        )
    )
    assert first.closes[0].quantity == pytest.approx(1)

    reduced = position_factory(quantity=3)
    confirmation = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=106, low=104, close=105),
            position=reduced,
        )
    )
    assert confirmation.closes == ()

    second = module.on_bar(
        view_factory(
            2,
            candle=candle_factory(2, open_=109, high=111, low=108, close=110),
            position=reduced,
        )
    )
    assert second.closes[0].quantity == pytest.approx(1)


def test_partial_retry_requests_only_residual_quantity(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4),
        )
    )
    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=106, low=104, close=105),
            position=position_factory(quantity=3),
        )
    )
    assert retry.closes[0].quantity == pytest.approx(1)
    assert retry.closes[0].reason.startswith(PositionExitReason.PARTIAL_EXIT_RETRY.value)


def test_partial_stage_waits_for_quantity_confirmation(
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
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=109, high=111, low=108, close=110),
            position=position_factory(quantity=4),
        )
    )
    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=110, high=111, low=109, close=110),
            position=position_factory(quantity=4),
        )
    )
    assert retry.closes[0].quantity == pytest.approx(1)
    assert "stage_1" in retry.closes[0].reason


def test_partial_overfill_marks_stage_complete(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4),
        )
    )
    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=106, low=104, close=105),
            position=position_factory(quantity=1.5),
        )
    )
    assert decision.closes == ()
    assert decision.diagnostics["partial_completed_index"] == 0


@pytest.mark.parametrize(
    ("side", "stop", "trigger_close", "expected_stop"),
    [
        (PositionSide.LONG, 95.0, 105.0, 100.0),
        (PositionSide.SHORT, 105.0, 95.0, 100.0),
    ],
)
def test_breakeven_is_symmetric_and_preserves_target(
    view_factory,
    candle_factory,
    position_factory,
    side,
    stop,
    trigger_close,
    expected_stop,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    position = position_factory(
        side=side, stop_loss=stop, take_profit=120 if side is PositionSide.LONG else 80
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=_next_candle(candle_factory, 0, trigger_close, side=side),
            position=position,
        )
    )
    assert len(decision.position_updates) == 1
    update = decision.position_updates[0]
    assert update.stop_loss == pytest.approx(expected_stop)
    assert update.take_profit == position.take_profit
    assert decision.features[0].value["action"] == ManagementAction.BREAKEVEN.value


def test_breakeven_offset_uses_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        breakeven_offset_r="0.2",
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(),
        )
    )
    assert decision.position_updates[0].stop_loss == pytest.approx(101)


def test_stop_never_loosens(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        minimum_stop_move_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=102),
        )
    )
    assert decision.position_updates == ()


def test_minimum_stop_move_filters_dust_update(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=0,
        breakeven_offset_r="0.001",
        minimum_stop_move_bps=10,
        market_buffer_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=101, low=99, close=100.5),
            position=position_factory(stop_loss=100),
        )
    )
    assert decision.position_updates == ()


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_r_multiple_trailing_is_symmetric(
    view_factory,
    candle_factory,
    position_factory,
    side,
):
    module = PositionExitManagementModule(
        trailing_mode="r_multiple",
        trailing_activation_r=2,
        trailing_distance_r=1,
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    if side is PositionSide.LONG:
        position = position_factory(side=side, stop_loss=95)
        candle = candle_factory(0, open_=109, high=112, low=108, close=110)
        expected = 107
    else:
        position = position_factory(side=side, stop_loss=105)
        candle = candle_factory(0, open_=91, high=92, low=88, close=90)
        expected = 93
    decision = module.on_bar(view_factory(0, candle=candle, position=position))
    assert decision.position_updates[0].stop_loss == pytest.approx(expected)
    assert decision.features[0].value["action"] == ManagementAction.TRAILING.value


def test_percent_trailing_can_activate_without_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.05",
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=110, low=103, close=105),
            position=position_factory(stop_loss=None),
        )
    )
    assert decision.position_updates[0].stop_loss == pytest.approx(104.5)
    assert PositionExitReason.MISSING_INITIAL_RISK.value not in decision.skips


def test_extreme_trigger_can_fire_when_close_trigger_does_not(
    view_factory,
    candle_factory,
    position_factory,
):
    close_module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        trigger_source="close",
    )
    extreme_module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        trigger_source="extreme",
    )
    candle = candle_factory(0, open_=100, high=106, low=99, close=101)
    position = position_factory()
    assert close_module.on_bar(view_factory(0, candle=candle, position=position)).closes == ()
    assert len(extreme_module.on_bar(view_factory(0, candle=candle, position=position)).closes) == 1


def test_data_gap_blocks_price_actions_but_not_time_exit(
    view_factory,
    candle_factory,
    position_factory,
):
    price_module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    price_decision = price_module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(),
            data_gap=True,
        )
    )
    assert price_decision.closes == ()
    assert price_decision.skips == (PositionExitReason.DATA_GAP.value,)

    time_module = PositionExitManagementModule(max_holding_minutes=5)
    time_decision = time_module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(),
            data_gap=True,
        )
    )
    assert len(time_decision.closes) == 1
    assert time_decision.closes[0].reason == PositionExitReason.TIME_EXIT.value


def test_time_exit_boundary_and_retry(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=10)
    before = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(),
        )
    )
    assert before.closes == ()
    first = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1),
            position=position_factory(),
        )
    )
    assert first.closes[0].reason == PositionExitReason.TIME_EXIT.value
    retry = module.on_bar(
        view_factory(
            2,
            candle=candle_factory(2),
            position=position_factory(),
        )
    )
    assert retry.closes[0].reason == PositionExitReason.TIME_EXIT_RETRY.value


def test_exit_pending_suppresses_new_close_request(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=5)
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(),
            exit_pending=True,
        )
    )
    assert decision.closes == ()
    assert decision.skips == (PositionExitReason.CLOSE_PENDING.value,)


def test_future_entry_time_fails_closed(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=5)
    future = position_factory(entry_minutes=30)
    decision = module.on_bar(view_factory(0, candle=candle_factory(0), position=future))
    assert decision.closes == ()
    assert decision.skips == (PositionExitReason.FUTURE_ENTRY_TIME.value,)


def test_missing_initial_risk_is_explainable(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=None),
        )
    )
    assert decision.closes == ()
    assert decision.position_updates == ()
    assert decision.skips == (PositionExitReason.MISSING_INITIAL_RISK.value,)


def test_late_valid_stop_keeps_initial_risk_unknown_across_restart(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(breakeven_trigger_r=1)
    first = module.on_bar(
        view_factory(0, candle=candle_factory(0), position=position_factory(stop_loss=None))
    )
    assert first.position_updates == ()
    second = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=95),
        )
    )
    assert second.position_updates == ()
    assert second.skips == (PositionExitReason.MISSING_INITIAL_RISK.value,)

    restored = PositionExitManagementModule(breakeven_trigger_r=1)
    restored.restore_state(module.snapshot_state())
    continued = restored.on_bar(
        view_factory(
            2,
            candle=candle_factory(2, open_=109, high=111, low=108, close=110),
            position=position_factory(stop_loss=95),
        )
    )
    assert continued.position_updates == ()
    assert continued.skips == (PositionExitReason.MISSING_INITIAL_RISK.value,)


def test_scale_in_resets_initial_quantity_and_risk_basis(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory(quantity=2)))
    scaled = position_factory(quantity=4, entry_price=101, stop_loss=96)
    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=107, low=104, close=106),
            position=scaled,
        )
    )
    assert decision.diagnostics["scale_in_quantity_increased"] is True
    assert decision.closes[0].quantity == pytest.approx(2)


def test_position_identity_change_resets_state(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    changed = position_factory(side=PositionSide.SHORT, stop_loss=105)
    decision = module.on_bar(view_factory(1, candle=candle_factory(1), position=changed))
    assert (
        decision.features[0].value["reason"] == PositionExitReason.POSITION_IDENTITY_CHANGED.value
    )


def test_status_feature_is_point_in_time_and_deterministic(
    view_factory,
    candle_factory,
    position_factory,
):
    left = PositionExitManagementModule(emit_status_feature=True)
    right = PositionExitManagementModule(emit_status_feature=True)
    view = view_factory(0, candle=candle_factory(0), position=position_factory())
    left_feature = left.on_bar(view).features[0]
    right_feature = right.on_bar(view).features[0]
    assert left_feature.feature_id == right_feature.feature_id
    assert left_feature.valid_until == left_feature.created_at
    assert left_feature.invalidate_on_gap is False


def test_status_feature_can_be_disabled(
    view_factory,
    position_factory,
):
    module = PositionExitManagementModule(emit_status_feature=False)
    assert module.on_bar(view_factory(position=position_factory())).features == ()


def test_exact_duplicate_is_idempotent(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=5)
    view = view_factory(0, candle=candle_factory(0), position=position_factory())
    first = module.on_bar(view)
    duplicate = module.on_bar(view)
    assert len(first.closes) == 1
    assert duplicate.closes == ()
    assert duplicate.features == ()
    assert duplicate.skips == (PositionExitReason.DUPLICATE_EVENT.value,)


def test_conflicting_duplicate_is_rejected(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    conflicting = Candle(
        candle_factory(0).timestamp,
        100,
        102,
        99,
        101,
        1,
        "BTCUSDT",
        "5m",
    )
    decision = module.on_bar(view_factory(0, candle=conflicting, position=position_factory()))
    assert decision.skips == (PositionExitReason.CONFLICTING_DUPLICATE.value,)


def test_out_of_order_event_is_rejected(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(2, candle=candle_factory(2), position=position_factory()))
    decision = module.on_bar(view_factory(1, candle=candle_factory(1), position=position_factory()))
    assert decision.skips == (PositionExitReason.OUT_OF_ORDER_EVENT.value,)


def test_stream_change_is_allowed_only_when_flat(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    changed = candle_factory(1, symbol="ETHUSDT")
    rejected = module.on_bar(view_factory(1, candle=changed, position=position_factory()))
    assert rejected.skips == (PositionExitReason.STREAM_CHANGED_WITH_POSITION.value,)

    flat_module = PositionExitManagementModule(emit_status_feature=True)
    flat_module.on_bar(view_factory(0, candle=candle_factory(0), position=None))
    accepted = flat_module.on_bar(view_factory(1, candle=changed, position=None))
    assert accepted.skips == ()
    assert accepted.features[0].value["symbol"] == "ETHUSDT"


def test_entry_time_uses_completed_bar_close_semantics(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=5)
    position = position_factory(entry_minutes=5)
    decision = module.on_bar(view_factory(1, candle=candle_factory(1), position=position))
    assert decision.closes[0].reason == PositionExitReason.TIME_EXIT.value


def test_future_data_does_not_change_past_decision(
    view_factory,
    candle_factory,
    position_factory,
):
    module_a = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    module_b = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    current = candle_factory(0, open_=100, high=102, low=99, close=101)
    decision_a = module_a.on_bar(view_factory(0, candle=current, position=position_factory()))
    future = candle_factory(1, open_=104, high=110, low=103, close=109)
    _ = future
    decision_b = module_b.on_bar(view_factory(0, candle=current, position=position_factory()))
    assert decision_a == decision_b
