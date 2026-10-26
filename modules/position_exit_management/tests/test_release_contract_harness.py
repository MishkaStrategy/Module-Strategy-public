from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from importlib.metadata import entry_points
from typing import Any

from modular_core import ModuleRegistry, PositionSide

from modular_strategy_position_exit_management import (
    FEATURE_SCHEMA_VERSION,
    MODULE_API_VERSION,
    MODULE_NAME,
    MODULE_VERSION,
    STATE_SCHEMA_VERSION,
    STATUS_FEATURE_KEY,
    PositionExitManagementModule,
    PositionExitReason,
)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _position_exit_registry() -> ModuleRegistry:
    selected = tuple(
        entry_points().select(
            group="modular_strategy.modules",
            name=MODULE_NAME,
        )
    )
    assert len(selected) == 1
    registry = ModuleRegistry()
    registry.register(MODULE_NAME, selected[0].load())
    return registry


def test_round_29_full_public_contract_harness(
    view_factory,
    candle_factory,
    position_factory,
    order_factory,
):
    registry = _position_exit_registry()
    module = registry.load_named(
        MODULE_NAME,
        {
            "max_holding_minutes": 30,
            "partial_exits": [{"trigger_r": "1", "fraction": "0.5"}],
            "breakeven_trigger_r": "1",
            "minimum_stop_move_bps": "0",
            "market_buffer_bps": "0",
        },
    )
    manifest = module.manifest
    assert manifest.name == MODULE_NAME
    assert manifest.version == MODULE_VERSION == "0.2.1"
    assert manifest.api_version == MODULE_API_VERSION == "1.0"
    assert manifest.state_schema_version == STATE_SCHEMA_VERSION == "2"
    assert manifest.required_features == ()
    assert manifest.required_timeframes == ()
    assert manifest.produced_features == (STATUS_FEATURE_KEY,)
    assert FEATURE_SCHEMA_VERSION == "2"

    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4, stop_loss=95, take_profit=120),
        )
    )
    assert len(first.closes) == 1
    assert len(first.position_updates) == 1
    checkpoint = json.loads(json.dumps(module.snapshot_state()))

    restored = PositionExitManagementModule(
        max_holding_minutes=30,
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    restored.restore_state(checkpoint)
    duplicate = restored.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4, stop_loss=95, take_profit=120),
        )
    )
    assert duplicate.skips == (PositionExitReason.DUPLICATE_EVENT.value,)

    gap = restored.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=106, high=120, low=90, close=110),
            position=position_factory(quantity=2, stop_loss=100, take_profit=120),
            data_gap=True,
        )
    )
    assert gap.skips == (PositionExitReason.DATA_GAP.value,)

    cleanup = restored.on_bar(
        view_factory(
            2,
            candle=candle_factory(2),
            pending_orders=(order_factory("managed-final"),),
        )
    )
    assert [request.order_id for request in cleanup.cancellations] == ["managed-final"]
    assert restored.snapshot_state()["active_position"] is not None

    finished = restored.on_bar(view_factory(3, candle=candle_factory(3)))
    assert finished.cancellations == ()
    assert restored.snapshot_state()["active_position"] is None


def test_round_30_continuous_and_restarted_replay_have_identical_digest(
    view_factory,
    candle_factory,
    position_factory,
):
    config = {
        "partial_exits": [{"trigger_r": 1, "fraction": 0.5}],
        "breakeven_trigger_r": 1,
        "trailing_mode": "r_multiple",
        "trailing_activation_r": 2,
        "trailing_distance_r": 1,
        "minimum_stop_move_bps": 0,
        "market_buffer_bps": 0,
        "emit_status_feature": True,
    }
    views = [
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(quantity=4, stop_loss=95),
        ),
        view_factory(
            1,
            candle=candle_factory(1, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4, stop_loss=95),
        ),
        view_factory(
            2,
            candle=candle_factory(2, open_=109, high=111, low=108, close=110),
            position=position_factory(quantity=2, stop_loss=100),
        ),
        view_factory(
            3,
            candle=candle_factory(3, open_=111, high=116, low=110, close=115),
            position=position_factory(quantity=2, stop_loss=105),
        ),
    ]

    continuous = PositionExitManagementModule(**config)
    continuous_decisions = [continuous.on_bar(view) for view in views]

    restarted = PositionExitManagementModule(**config)
    restarted_decisions = [restarted.on_bar(view) for view in views[:2]]
    checkpoint = json.loads(json.dumps(restarted.snapshot_state()))
    replacement = PositionExitManagementModule(**config)
    replacement.restore_state(checkpoint)
    restarted_decisions.extend(replacement.on_bar(view) for view in views[2:])

    continuous_payload = (continuous_decisions, continuous.snapshot_state())
    restarted_payload = (restarted_decisions, replacement.snapshot_state())
    assert _digest(continuous_payload) == _digest(restarted_payload)
    assert replacement.snapshot_state()["active_position"]["side"] == PositionSide.LONG.value


def test_round_30_replay_evidence_tool_matches_contract():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "replay_evidence.py"
    spec = importlib.util.spec_from_file_location("position_exit_replay_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    evidence = module.run_evidence()
    assert evidence["deterministic"] is True
    assert evidence["continuous_digest"] == evidence["restarted_digest"]
    assert evidence["checkpoint_bytes"] < 2_048
