from __future__ import annotations

import copy

import pytest

from conftest import bullish_triplet, monotonic_bars, view_for
from modular_strategy_fvg import FvgModule, FvgStateError


def _active_module() -> FvgModule:
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bullish_triplet()))
    return module


def _as_v2_state(module: FvgModule) -> dict[str, object]:
    current = copy.deepcopy(module.snapshot_state())
    last_processed = copy.deepcopy(current["last_processed"])
    for row in last_processed:
        row.pop("history_fingerprints")
    return {
        "state_schema_version": "2",
        "module_version": "0.2.0",
        "config_fingerprint": module._v2_config_fingerprint,  # noqa: SLF001
        "bound_symbol": current["bound_symbol"],
        "last_processed": last_processed,
        "zones": copy.deepcopy(current["zones"]),
    }


def _as_v3_state(module: FvgModule) -> dict[str, object]:
    current = copy.deepcopy(module.snapshot_state())
    return {
        "state_schema_version": "3",
        "module_version": "0.2.1",
        "config_fingerprint": module._v3_config_fingerprint,  # noqa: SLF001
        "bound_symbol": current["bound_symbol"],
        "last_processed": copy.deepcopy(current["last_processed"]),
        "zones": copy.deepcopy(current["zones"]),
    }


@pytest.mark.parametrize("schema", ["2", "3"])
def test_legacy_checkpoint_migrates_to_v4_and_preserves_active_zones(
    schema: str,
) -> None:
    source = _active_module()
    restored = FvgModule(timeframes=("15m",))
    state = _as_v2_state(source) if schema == "2" else _as_v3_state(source)

    restored.restore_state(state)

    migrated = restored.snapshot_state()
    assert migrated["state_schema_version"] == "4"
    assert migrated["module_version"] == "0.3.0"
    assert restored.active_zones == source.active_zones
    assert migrated["last_processed"][0]["history_fingerprints"]
    assert len(migrated["zone_lifecycle"]) == 1


def test_v2_migration_fails_closed_until_three_post_cursor_bars() -> None:
    source = _active_module()
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(_as_v2_state(source))

    bars = monotonic_bars(6)
    bars[:3] = list(bullish_triplet())
    existing_ids = {zone.zone_id for zone in source.active_zones}
    first = restored.on_bar(view_for(bars[:4]))
    second = restored.on_bar(view_for(bars[:5]))
    third = restored.on_bar(view_for(bars[:6]))

    def new_zone_ids(decision: object) -> set[str]:
        return {
            feature.feature_id
            for feature in decision.features  # type: ignore[attr-defined]
            if feature.key == "fvg.active_zone" and feature.feature_id not in existing_ids
        }

    assert not new_zone_ids(first)
    assert not new_zone_ids(second)
    assert new_zone_ids(third)


@pytest.mark.parametrize(
    "mutation",
    [
        "not_sequence",
        "non_mapping_row",
        "unknown_key",
        "too_many",
        "bad_fingerprint",
        "unordered",
        "after_cursor",
        "tail_mismatch",
        "history_without_current",
    ],
)
def test_v4_restore_rejects_corrupted_history_fingerprints(
    mutation: str,
) -> None:
    state = copy.deepcopy(_active_module().snapshot_state())
    row = state["last_processed"][0]
    history = row["history_fingerprints"]

    if mutation == "not_sequence":
        row["history_fingerprints"] = "bad"
    elif mutation == "non_mapping_row":
        row["history_fingerprints"] = [1]
    elif mutation == "unknown_key":
        history[0]["extra"] = "x"
    elif mutation == "too_many":
        row["history_fingerprints"] = [*history, *history]
    elif mutation == "bad_fingerprint":
        history[-1]["bar_fingerprint"] = "bad"
    elif mutation == "unordered":
        row["history_fingerprints"] = [history[-1], history[0]]
    elif mutation == "after_cursor":
        history[-1]["close_time"] = "2099-01-01T00:00:00+00:00"
    elif mutation == "tail_mismatch":
        history[-1]["bar_fingerprint"] = "0" * 64
    elif mutation == "history_without_current":
        row["bar_fingerprint"] = None
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)

    with pytest.raises(FvgStateError):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_v4_restore_is_atomic_when_history_validation_fails() -> None:
    target = _active_module()
    before = target.snapshot_state()
    corrupted = copy.deepcopy(before)
    corrupted["last_processed"][0]["history_fingerprints"][-1]["bar_fingerprint"] = "0" * 64

    with pytest.raises(FvgStateError):
        target.restore_state(corrupted)
    assert target.snapshot_state() == before
