from __future__ import annotations

from modular_core import ModuleRegistry

from modular_strategy_position_exit_management import (
    MODULE_NAME,
    PositionExitManagementModule,
    PositionExitReason,
)


def test_full_operational_lifecycle_checkpoint_replay_and_cleanup(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    config = {
        "max_holding_minutes": 60,
        "partial_exits": [{"trigger_r": 1, "fraction": 0.5}],
        "breakeven_trigger_r": 1,
        "minimum_stop_move_bps": 0,
        "market_buffer_bps": 0,
    }
    module = PositionExitManagementModule(**config)
    position = position_factory(quantity=4, stop_loss=95)
    trigger_view = view_factory(
        0,
        candle=candle_factory(0, open_=104, high=106, low=103, close=105),
        position=position,
    )
    decision = module.on_bar(trigger_view)
    assert decision.closes[0].quantity == 2
    assert decision.position_updates[0].stop_loss == 100

    checkpoint = module.snapshot_state()
    restored = PositionExitManagementModule(**config)
    restored.restore_state(checkpoint)
    duplicate = restored.on_bar(trigger_view)
    assert duplicate.skips == (PositionExitReason.DUPLICATE_EVENT.value,)

    managed = order_factory("protective-order", setup_id=position.setup_id)
    cleanup = restored.on_bar(
        view_factory(1, candle=candle_factory(1), position=None, pending_orders=(managed,))
    )
    assert [request.order_id for request in cleanup.cancellations] == [managed.id]

    finished = restored.on_bar(
        view_factory(2, candle=candle_factory(2), position=None, pending_orders=())
    )
    assert finished.cancellations == ()
    assert restored.snapshot_state()["active_position"] is None


def test_registry_manifest_and_dependency_plan_have_no_hidden_inputs():
    module = PositionExitManagementModule()
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    assert plan.modules == (module,)
    assert plan.required_timeframes == ()
    assert module.manifest.required_features == ()
    assert module.manifest.optional_features == ()
    assert module.manifest.produced_features == (f"{MODULE_NAME}.status",)


def test_independent_instances_do_not_share_stream_or_runtime_state(
    view_factory,
    candle_factory,
    position_factory,
):
    btc = PositionExitManagementModule()
    eth = PositionExitManagementModule()
    btc.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, symbol="BTCUSDT"),
            position=position_factory(position_id="btc-position"),
        )
    )
    eth.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, symbol="ETHUSDT"),
            position=position_factory(position_id="eth-position"),
        )
    )
    btc_state = btc.snapshot_state()
    eth_state = eth.snapshot_state()
    assert btc_state["stream_symbol"] == "BTCUSDT"
    assert eth_state["stream_symbol"] == "ETHUSDT"
    assert btc_state["active_position"]["position_id"] == "btc-position"
    assert eth_state["active_position"]["position_id"] == "eth-position"
    assert btc_state != eth_state
