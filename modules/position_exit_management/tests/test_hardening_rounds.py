from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from modular_core import PositionSide

from modular_strategy_position_exit_management import (
    PositionExitConfig,
    PositionExitManagementModule,
    PositionExitReason,
)


def test_round_01_short_pending_partial_preserves_scale_in_quantity(
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
            candle=candle_factory(0, open_=96, high=97, low=94, close=95),
            position=position_factory(
                side=PositionSide.SHORT,
                quantity=2,
                stop_loss=105,
            ),
        )
    )
    assert first.closes[0].quantity == pytest.approx(1)

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=96, high=97, low=94, close=95),
            position=position_factory(
                side=PositionSide.SHORT,
                quantity=4,
                stop_loss=105,
            ),
        )
    )
    assert retry.closes[0].quantity == pytest.approx(1)
    assert module.snapshot_state()["active_position"]["pending_partial_target_quantity"] == "3"


def test_round_02_decimal_extremes_remain_canonical_and_distinct():
    tiny = PositionExitConfig.from_parameters(
        breakeven_trigger_r="0.000000000000000000000000000001",
        trailing_distance_r="1000000000000000000000000000000",
        minimum_stop_move_bps="0.00000000000000000001",
    )
    equivalent = PositionExitConfig.from_parameters(
        breakeven_trigger_r=Decimal("1e-30"),
        trailing_distance_r=Decimal("1e30"),
        minimum_stop_move_bps=Decimal("1e-20"),
    )
    mapping = tiny.canonical_mapping()
    assert mapping["breakeven_trigger_r"] == "0.000000000000000000000000000001"
    assert mapping["trailing_distance_r"] == "1000000000000000000000000000000"
    assert mapping["minimum_stop_move_bps"] == "0.00000000000000000001"
    assert tiny.fingerprint == equivalent.fingerprint


@pytest.mark.parametrize(
    ("side", "stop", "close", "comparison"),
    [
        (PositionSide.LONG, 95, 105, "below"),
        (PositionSide.SHORT, 105, 95, "above"),
    ],
)
def test_round_02_zero_buffer_still_keeps_stop_strictly_behind_market(
    view_factory,
    candle_factory,
    position_factory,
    side,
    stop,
    close,
    comparison,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        breakeven_offset_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(
                0,
                open_=close,
                high=max(close + 1, 101),
                low=min(close - 1, 99),
                close=close,
            ),
            position=position_factory(side=side, stop_loss=stop),
        )
    )
    requested = decision.position_updates[0].stop_loss
    if comparison == "below":
        assert requested < close
    else:
        assert requested > close


def test_round_03_entry_exactly_at_completed_bar_close_is_not_future(
    view_factory,
    candle_factory,
    position_factory,
):
    from dataclasses import replace

    module = PositionExitManagementModule(max_holding_minutes=5)
    candle = candle_factory(0)
    position = replace(position_factory(), entry_time=candle.close_time)
    decision = module.on_bar(view_factory(0, candle=candle, position=position))
    assert PositionExitReason.FUTURE_ENTRY_TIME.value not in decision.skips
    assert decision.closes == ()
    assert decision.diagnostics["holding_minutes"] == 0
    assert module.snapshot_state()["active_position"]["entry_time"] == candle.close_time.isoformat()


def test_round_04_same_instant_with_different_timezone_offset_is_exact_duplicate(
    view_factory,
    candle_factory,
    position_factory,
):
    from dataclasses import replace
    from datetime import timedelta, timezone

    module = PositionExitManagementModule()
    candle = candle_factory(0)
    original = position_factory()
    module.on_bar(view_factory(0, candle=candle, position=original))
    equivalent = replace(
        original,
        entry_time=original.entry_time.astimezone(timezone(timedelta(hours=7))),
    )
    decision = module.on_bar(view_factory(0, candle=candle, position=equivalent))
    assert decision.skips == (PositionExitReason.DUPLICATE_EVENT.value,)


def test_round_05_percent_trailing_without_initial_stop_uses_only_declared_inputs(
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
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=108, high=110, low=107, close=109),
            position=position_factory(stop_loss=None),
        )
    )
    assert decision.position_updates[0].stop_loss == pytest.approx(99)
    assert PositionExitReason.MISSING_INITIAL_RISK.value not in decision.skips
    active = module.snapshot_state()["active_position"]
    assert active["initial_risk_per_unit"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag", "different-producer-tag"),
        ("unrealized_pnl", 123.45),
        ("realised_pnl", 67.89),
        ("entry_fill_count", 3),
        ("exit_fill_count", 2),
    ],
)
def test_round_06_unconsumed_position_metadata_is_producer_substitutable(
    view_factory,
    candle_factory,
    position_factory,
    field,
    value,
):
    from dataclasses import replace

    module = PositionExitManagementModule()
    candle = candle_factory(0)
    position = position_factory()
    module.on_bar(view_factory(0, candle=candle, position=position))
    equivalent = replace(position, **{field: value})
    decision = module.on_bar(view_factory(0, candle=candle, position=equivalent))
    assert decision.skips == (PositionExitReason.DUPLICATE_EVENT.value,)


def test_round_07_outputs_are_immutable_core_dtos(
    view_factory,
    candle_factory,
    position_factory,
):
    from dataclasses import FrozenInstanceError

    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
        emit_status_feature=True,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=95),
        )
    )
    assert isinstance(decision.features, tuple)
    assert isinstance(decision.position_updates, tuple)
    with pytest.raises(TypeError):
        decision.features[0].value["reason"] = "mutated"
    with pytest.raises(FrozenInstanceError):
        decision.position_updates[0].stop_loss = 1


def test_round_08_status_feature_id_is_deterministic_and_content_addressed(
    view_factory,
    candle_factory,
    position_factory,
):
    config = {
        "breakeven_trigger_r": 1,
        "minimum_stop_move_bps": 0,
        "market_buffer_bps": 0,
        "emit_status_feature": True,
    }
    view = view_factory(
        0,
        candle=candle_factory(0, open_=104, high=106, low=103, close=105),
        position=position_factory(stop_loss=95),
    )
    first = PositionExitManagementModule(**config).on_bar(view).features[0]
    second = PositionExitManagementModule(**config).on_bar(view).features[0]
    changed = (
        PositionExitManagementModule(**config)
        .on_bar(
            view_factory(
                0,
                candle=candle_factory(0, open_=109, high=111, low=108, close=110),
                position=position_factory(stop_loss=95),
            )
        )
        .features[0]
    )
    assert first.feature_id == second.feature_id
    assert first.value == second.value
    assert first.feature_id != changed.feature_id


def test_round_09_registry_loader_roundtrips_full_strict_configuration():
    from modular_core import ModuleRegistry

    parameters = {
        "max_holding_minutes": 90,
        "partial_exits": [
            {"trigger_r": "1.25", "fraction": "0.25"},
            {"trigger_r": 2.5, "fraction": 0.25},
        ],
        "breakeven_trigger_r": "1.5",
        "breakeven_offset_r": "0.1",
        "trailing_mode": "r_multiple",
        "trailing_activation_r": 2,
        "trailing_distance_r": "0.75",
        "trigger_source": "extreme",
        "minimum_stop_move_bps": "0.5",
        "market_buffer_bps": 3,
        "cancel_retry_bars": 2,
        "max_tracked_cancels": 32,
        "max_cancel_requests_per_bar": 8,
        "emit_status_feature": True,
    }
    module = ModuleRegistry().load_import_path(
        "modular_strategy_position_exit_management:PositionExitManagementModule",
        parameters,
    )
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    assert plan.modules == (module,)
    assert module.config.max_holding_minutes == 90
    assert module.parameters["breakeven_trigger_r"] == "1.5"
    assert module.parameters["partial_exits"][0]["trigger_r"] == "1.25"


def test_round_10_configuration_fingerprint_is_order_and_negative_zero_invariant():
    first = PositionExitConfig.from_parameters(
        partial_exits=[
            {"trigger_r": 2, "fraction": "0.25"},
            {"trigger_r": 1, "fraction": "0.25"},
        ],
        breakeven_offset_r="-0.0000",
    )
    second = PositionExitConfig.from_parameters(
        partial_exits=[
            {"fraction": Decimal("0.250"), "trigger_r": Decimal("1.0")},
            {"fraction": Decimal("0.25"), "trigger_r": Decimal("2.00")},
        ],
        breakeven_offset_r=0,
    )
    assert first.canonical_mapping() == second.canonical_mapping()
    assert first.fingerprint == second.fingerprint
    assert first.canonical_mapping()["breakeven_offset_r"] == "0"


def test_round_11_partial_state_advances_as_a_contiguous_single_stage_per_bar(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[
            {"trigger_r": 1, "fraction": 0.25},
            {"trigger_r": 2, "fraction": 0.25},
            {"trigger_r": 3, "fraction": 0.25},
        ]
    )

    def hot(index):
        return candle_factory(index, open_=119, high=121, low=118, close=120)

    first = module.on_bar(
        view_factory(0, candle=hot(0), position=position_factory(quantity=4, stop_loss=95))
    )
    assert first.closes[0].reason.endswith("stage_1")
    state = module.snapshot_state()["active_position"]
    assert state["completed_partial_indices"] == ()
    assert state["pending_partial_index"] == 0

    second = module.on_bar(
        view_factory(1, candle=hot(1), position=position_factory(quantity=3, stop_loss=95))
    )
    assert second.closes[0].reason.endswith("stage_2")
    state = module.snapshot_state()["active_position"]
    assert state["completed_partial_indices"] == (0,)
    assert state["pending_partial_index"] == 1

    third = module.on_bar(
        view_factory(2, candle=hot(2), position=position_factory(quantity=2, stop_loss=95))
    )
    assert third.closes[0].reason.endswith("stage_3")
    state = module.snapshot_state()["active_position"]
    assert state["completed_partial_indices"] == (0, 1)
    assert state["pending_partial_index"] == 2


def test_round_12_position_replacement_resets_all_position_owned_state(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
        trailing_mode="percent",
        trailing_activation_r=1,
        trailing_distance_pct="0.05",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=110, low=103, close=105),
            position=position_factory(position_id="old", quantity=4, stop_loss=95),
        )
    )
    old_state = module.snapshot_state()["active_position"]
    assert old_state["pending_partial_index"] == 0
    assert old_state["breakeven_applied"] is True
    assert old_state["trailing_active"] is True

    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=100, high=101, low=99, close=100),
            position=position_factory(position_id="new", quantity=2, stop_loss=96),
        )
    )
    assert decision.features[0].value["reason"] == PositionExitReason.POSITION_REPLACED.value
    state = module.snapshot_state()["active_position"]
    assert state["position_id"] == "new"
    assert state["initial_quantity"] == "2"
    assert state["completed_partial_indices"] == ()
    assert state["pending_partial_index"] is None
    assert state["breakeven_applied"] is False
    assert state["trailing_active"] is False
    assert state["bars_managed"] == 1


def test_round_13_checkpoint_json_roundtrip_preserves_active_lifecycle(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
        trailing_mode="percent",
        trailing_activation_r=1,
        trailing_distance_pct="0.05",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    position = position_factory(quantity=4, stop_loss=95)
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=110, low=103, close=105),
            position=position,
        )
    )
    encoded = json.dumps(module.snapshot_state(), sort_keys=True)
    restored = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
        trailing_mode="percent",
        trailing_activation_r=1,
        trailing_distance_pct="0.05",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    restored.restore_state(json.loads(encoded))
    assert restored.snapshot_state() == module.snapshot_state()

    continuation = restored.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=106, high=111, low=105, close=107),
            position=position_factory(quantity=2, stop_loss=104.5),
        )
    )
    assert continuation.closes == ()
    active = restored.snapshot_state()["active_position"]
    assert active["completed_partial_indices"] == (0,)
    assert active["pending_partial_index"] is None
    assert active["breakeven_applied"] is True
    assert active["trailing_active"] is True


def test_round_14_nested_restore_failure_is_atomic_and_recoverable(
    view_factory,
    candle_factory,
    position_factory,
):
    config = {
        "partial_exits": [
            {"trigger_r": 1, "fraction": 0.25},
            {"trigger_r": 2, "fraction": 0.25},
        ]
    }
    module = PositionExitManagementModule(**config)
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4, stop_loss=95),
        )
    )
    valid = deepcopy(module.snapshot_state())
    broken = deepcopy(valid)
    broken["active_position"]["completed_partial_indices"] = [1]
    before = deepcopy(module.snapshot_state())
    with pytest.raises(ValueError, match="contiguous"):
        module.restore_state(broken)
    assert module.snapshot_state() == before

    control = PositionExitManagementModule(**config)
    control.restore_state(valid)
    next_view = view_factory(
        1,
        candle=candle_factory(1, open_=106, high=111, low=105, close=110),
        position=position_factory(quantity=3, stop_loss=95),
    )
    assert module.on_bar(next_view) == control.on_bar(next_view)
    assert module.snapshot_state() == control.snapshot_state()


def test_round_15_exact_duplicate_after_restore_is_a_pure_noop(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(breakeven_trigger_r=1)
    view = view_factory(
        0,
        candle=candle_factory(0, open_=104, high=106, low=103, close=105),
        position=position_factory(stop_loss=95),
    )
    module.on_bar(view)
    restored = PositionExitManagementModule(breakeven_trigger_r=1)
    restored.restore_state(json.loads(json.dumps(module.snapshot_state())))
    before = deepcopy(restored.snapshot_state())
    duplicate = restored.on_bar(view)
    assert duplicate.skips == (PositionExitReason.DUPLICATE_EVENT.value,)
    assert duplicate.closes == ()
    assert duplicate.position_updates == ()
    assert duplicate.cancellations == ()
    assert restored.snapshot_state() == before


def test_round_16_restored_cursor_rejects_order_conflict_and_stream_change(
    view_factory,
    candle_factory,
    position_factory,
):
    source = PositionExitManagementModule()
    source.on_bar(
        view_factory(
            2,
            candle=candle_factory(2),
            position=position_factory(),
        )
    )
    checkpoint = json.loads(json.dumps(source.snapshot_state()))

    cases = (
        (
            view_factory(
                1,
                candle=candle_factory(1),
                position=position_factory(),
            ),
            PositionExitReason.OUT_OF_ORDER_EVENT,
        ),
        (
            view_factory(
                2,
                candle=candle_factory(2, close=100.5, high=101.5),
                position=position_factory(),
            ),
            PositionExitReason.CONFLICTING_DUPLICATE,
        ),
        (
            view_factory(
                3,
                candle=candle_factory(3, symbol="ETHUSDT"),
                position=position_factory(),
            ),
            PositionExitReason.STREAM_CHANGED_WITH_POSITION,
        ),
    )
    for view, reason in cases:
        module = PositionExitManagementModule()
        module.restore_state(checkpoint)
        before = deepcopy(module.snapshot_state())
        decision = module.on_bar(view)
        assert decision.skips == (reason.value,)
        assert module.snapshot_state() == before


def test_round_19_latest_external_take_profit_is_preserved_in_stop_update(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=2,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(stop_loss=95, take_profit=120),
        )
    )
    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=109, high=111, low=108, close=110),
            position=position_factory(stop_loss=95, take_profit=130),
        )
    )
    assert len(decision.position_updates) == 1
    update = decision.position_updates[0]
    assert update.stop_loss == pytest.approx(100)
    assert update.take_profit == pytest.approx(130)


def test_round_20_partial_and_stop_requests_share_one_typed_decision(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
        emit_status_feature=True,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4, stop_loss=95, take_profit=120),
        )
    )
    assert len(decision.closes) == 1
    assert decision.closes[0].quantity == pytest.approx(2)
    assert len(decision.position_updates) == 1
    assert decision.position_updates[0].stop_loss == pytest.approx(100)
    assert decision.position_updates[0].take_profit == pytest.approx(120)
    assert decision.features[0].value["action"] == "partial_exit"
    assert decision.features[0].value["reason"] == PositionExitReason.PARTIAL_EXIT.value
    assert decision.diagnostics["stop_components"] == ("breakeven",)


def test_round_25_time_exit_retry_status_is_structured_and_stable(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=5, emit_status_feature=True)
    position = position_factory()
    first = module.on_bar(view_factory(1, candle=candle_factory(1), position=position))
    second = module.on_bar(view_factory(2, candle=candle_factory(2), position=position))
    assert first.features[0].value["action"] == "time_exit"
    assert first.features[0].value["reason"] == PositionExitReason.TIME_EXIT.value
    assert second.features[0].value["action"] == "time_exit_retry"
    assert second.features[0].value["reason"] == PositionExitReason.TIME_EXIT_RETRY.value
    assert second.diagnostics["time_exit_retry"] is True
    assert isinstance(second.diagnostics["holding_minutes"], float)
    assert isinstance(second.diagnostics["bars_managed"], int)
