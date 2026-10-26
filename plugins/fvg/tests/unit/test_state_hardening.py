from __future__ import annotations

import copy
import json

import pytest

from conftest import bullish_triplet, view_for
from modular_strategy_fvg import FvgModule, FvgStateError


def active_module(**kwargs: object) -> FvgModule:
    module = FvgModule(timeframes=("15m",), **kwargs)
    module.on_bar(view_for(bullish_triplet()))
    return module


def test_v4_state_roundtrip_and_republish_once() -> None:
    original = active_module()
    state = original.snapshot_state()
    assert state["state_schema_version"] == "4"
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(state)
    first = restored.on_bar(view_for(bullish_triplet()))
    second = restored.on_bar(view_for(bullish_triplet()))
    assert len(first.features) == 2
    assert second.features == ()
    assert restored.snapshot_state() == original.snapshot_state()


def test_state_is_json_serializable_and_deterministic() -> None:
    module = active_module()
    first = json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":"))
    assert first == second


def test_v1_state_migration() -> None:
    module = active_module()
    current = module.snapshot_state()
    legacy = {
        "state_schema_version": "1",
        "module_version": "0.1.0",
        "config_fingerprint": module._legacy_config_fingerprint,  # noqa: SLF001
        "last_processed": [
            {
                key: value
                for key, value in row.items()
                if key
                not in {
                    "bar_fingerprint",
                    "history_fingerprints",
                    "barrier_close_time",
                }
            }
            for row in current["last_processed"]
        ],
        "zones": current["zones"],
    }
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(legacy)
    assert restored.bound_symbol == "BTCUSDT"
    assert restored.active_zones == module.active_zones
    migrated = restored.snapshot_state()
    assert migrated["state_schema_version"] == "4"
    assert migrated["module_version"] == "0.3.0"
    assert len(migrated["zone_lifecycle"]) == 1


@pytest.mark.parametrize(
    "missing",
    [
        "state_schema_version",
        "module_version",
        "config_fingerprint",
        "bound_symbol",
        "zone_lifecycle",
        "pre_evaluated",
        "pre_candidates",
        "last_processed",
        "zones",
    ],
)
def test_missing_v4_state_key_rejected(missing: str) -> None:
    state = dict(active_module().snapshot_state())
    state.pop(missing)
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_unknown_state_key_rejected() -> None:
    state = dict(active_module().snapshot_state())
    state["extra"] = 1
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("state_schema_version", "99"),
        ("module_version", "9.9.9"),
        ("config_fingerprint", "0" * 64),
        ("bound_symbol", 123),
        ("bound_symbol", ""),
    ],
)
def test_incompatible_top_level_state_rejected(key: str, value: object) -> None:
    state = dict(active_module().snapshot_state())
    state[key] = value
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_corrupt_fingerprint_rejected() -> None:
    state = copy.deepcopy(active_module().snapshot_state())
    state["last_processed"][0]["bar_fingerprint"] = "bad"
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_duplicate_cursor_rejected() -> None:
    state = copy.deepcopy(active_module().snapshot_state())
    state["last_processed"].append(dict(state["last_processed"][0]))
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_duplicate_zone_rejected() -> None:
    state = copy.deepcopy(active_module().snapshot_state())
    state["zones"].append(dict(state["zones"][0]))
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_multisymbol_state_rejected() -> None:
    state = copy.deepcopy(active_module().snapshot_state())
    state["zones"][0]["symbol"] = "ETHUSDT"
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_zone_after_cursor_rejected() -> None:
    state = copy.deepcopy(active_module().snapshot_state())
    state["last_processed"][0]["close_time"] = state["zones"][0]["source_b_open_time"]
    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_oversized_state_rejected_before_commit() -> None:
    target = FvgModule(timeframes=("15m",), max_state_bytes=1024)
    state = dict(active_module().snapshot_state())
    state["padding"] = "x" * 2000
    before = target.snapshot_state()
    with pytest.raises(FvgStateError, match="exceeds"):
        target.restore_state(state)
    assert target.snapshot_state() == before


def test_tampered_zone_id_rejected() -> None:
    state = copy.deepcopy(active_module().snapshot_state())
    state["zones"][0]["zone_id"] = "tampered"
    with pytest.raises(FvgStateError, match="canonical zone identity"):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_capacity_mismatch_state_rejected() -> None:
    source = active_module()
    state = copy.deepcopy(source.snapshot_state())
    target = FvgModule(timeframes=("15m",), max_active_zones_per_timeframe=1)
    state["config_fingerprint"] = target._config_fingerprint  # noqa: SLF001
    state["zones"].append({**state["zones"][0], "zone_id": "second"})
    with pytest.raises(FvgStateError):
        target.restore_state(state)
