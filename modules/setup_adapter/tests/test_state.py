from __future__ import annotations

from copy import deepcopy

import pytest
from modular_core import capture_module_states, restore_module_states

from modular_setup_adapter import CanonicalSetupAdapterModule, SetupAdapterStateError

from .conftest import bar, breakout_feature, view


def test_snapshot_restore_preserves_replay_barrier() -> None:
    current = bar()
    feature = breakout_feature(current)
    original = CanonicalSetupAdapterModule(max_processed_sources=2)
    original.on_bar(view(current, (feature,)))
    snapshot = original.snapshot_state()

    restored = CanonicalSetupAdapterModule(max_processed_sources=2)
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    decision = restored.on_bar(view(current, (feature,)))
    assert decision.features == ()
    assert "setup_adapter.duplicate_event" in decision.skips


def test_core_capture_restore_helpers() -> None:
    module = CanonicalSetupAdapterModule()
    module.on_bar(view(bar()))
    states = capture_module_states((module,))
    restored = CanonicalSetupAdapterModule()
    restore_module_states((restored,), states)
    assert restored.snapshot_state() == module.snapshot_state()


def test_state_capacity_is_bounded() -> None:
    module = CanonicalSetupAdapterModule(max_processed_sources=2)
    for index in range(3):
        current = bar(index)
        feature = breakout_feature(
            current,
            feature_id=f"source-{index}",
            payload_overrides={
                "setup_id": f"setup-{index}",
                "confirmation_time": current.close_time.isoformat(),
            },
        )
        module.on_bar(view(current, (feature,)))
    assert len(module.snapshot_state()["processed"]) == 2


def test_corrupt_or_incompatible_state_rejected() -> None:
    module = CanonicalSetupAdapterModule()
    state = dict(module.snapshot_state())
    bad_schema = deepcopy(state)
    bad_schema["state_schema_version"] = "9"
    with pytest.raises(SetupAdapterStateError, match="unsupported"):
        module.restore_state(bad_schema)
    bad_config = deepcopy(state)
    bad_config["config_fingerprint"] = "0" * 64
    with pytest.raises(SetupAdapterStateError, match="fingerprint"):
        module.restore_state(bad_config)


@pytest.mark.parametrize(
    "mutator,match",
    [
        (lambda state: state.__setitem__("capacity", 999), "capacity"),
        (lambda state: state.__setitem__("processed", "bad"), "sequence"),
        (lambda state: state.__setitem__("processed", [["id", "short", "ok"]]), "row values"),
        (
            lambda state: state.__setitem__(
                "processed", [["id", "0" * 64, "ok"], ["id", "1" * 64, "ok"]]
            ),
            "duplicate",
        ),
        (lambda state: state.__setitem__("counters", "bad"), "mapping"),
        (lambda state: state.__setitem__("counters", {"x": True}), "counter"),
        (lambda state: state.__setitem__("counters", {"x": -1}), "negative"),
        (lambda state: state.__setitem__("last_bar_signature", ["short"]), "length"),
        (lambda state: state.__setitem__("bound_symbol", "BTCUSDT"), "together"),
    ],
)
def test_additional_corrupt_state_paths(mutator, match: str) -> None:
    module = CanonicalSetupAdapterModule()
    state = deepcopy(dict(module.snapshot_state()))
    mutator(state)
    with pytest.raises((SetupAdapterStateError, ValueError), match=match):
        module.restore_state(state)


def test_restore_rejects_signature_mismatch_and_bound_stream_conflict() -> None:
    original = CanonicalSetupAdapterModule()
    original.on_bar(view(bar()))
    state = deepcopy(dict(original.snapshot_state()))

    mismatched_time = deepcopy(state)
    mismatched_time["last_bar_signature"][0] = bar(1).timestamp.isoformat()
    with pytest.raises(ValueError, match="timestamp mismatch"):
        CanonicalSetupAdapterModule().restore_state(mismatched_time)

    conflicting_stream = deepcopy(state)
    conflicting_stream["last_bar_signature"][6] = "ETHUSDT"
    with pytest.raises(ValueError, match="contradicts"):
        CanonicalSetupAdapterModule().restore_state(conflicting_stream)
