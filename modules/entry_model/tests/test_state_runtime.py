from __future__ import annotations

import json
from copy import deepcopy

import pytest

from modular_modules.entry_model import EntryModelModule
from modular_modules.entry_model.state import EntryModelState

from .support import next_feature, view_at


def populated_module(*, max_items: int = 10) -> EntryModelModule:
    module = EntryModelModule(
        max_processed_setups=max_items,
        max_setup_age_seconds=600,
    )
    module.on_bar(view_at(features=(next_feature(0),)))
    return module


def test_snapshot_is_json_serializable() -> None:
    json.dumps(populated_module().snapshot_state())


def test_restore_continues_idempotently() -> None:
    source = populated_module()
    restored = EntryModelModule(max_processed_setups=10, max_setup_age_seconds=600)
    restored.restore_state(source.snapshot_state())
    decision = restored.on_bar(view_at(1, features=(next_feature(1, setup_id="setup-0"),)))
    assert "entry_model.setup_revision_conflict" in decision.skips


def test_restore_preserves_exact_duplicate_result() -> None:
    source = populated_module()
    restored = EntryModelModule(max_processed_setups=10, max_setup_age_seconds=600)
    restored.restore_state(source.snapshot_state())
    # Same canonical setup, a fresh source feature, later publication.
    duplicate = next_feature(1, setup_id="setup-0")
    duplicate_value = dict(duplicate.value)
    duplicate_value["confirmed_at"] = view_at().current.close_time.isoformat()
    from .support import setup_feature

    decision = restored.on_bar(
        view_at(
            1,
            features=(
                setup_feature(
                    value=duplicate_value,
                    index=1,
                    feature_id="fresh-source",
                ),
            ),
        )
    )
    assert "entry_model.duplicate_setup" in decision.skips


def test_config_fingerprint_mismatch_is_rejected() -> None:
    state = populated_module().snapshot_state()
    other = EntryModelModule(
        max_processed_setups=10,
        max_setup_age_seconds=600,
        retracement_fraction="0.5",
    )
    with pytest.raises(ValueError, match="config fingerprint"):
        other.restore_state(state)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(state_schema_version="1"),
        lambda state: state.update(unknown=True),
        lambda state: state.update(processed="bad"),
        lambda state: state.update(seen_features="bad"),
        lambda state: state.update(stream=[]),
        lambda state: state.update(counters=[]),
    ],
)
def test_corrupt_restore_is_atomic(mutation: object) -> None:
    module = populated_module()
    before = module.snapshot_state()
    corrupted = deepcopy(dict(before))
    mutation(corrupted)  # type: ignore[operator]
    with pytest.raises(ValueError):
        module.restore_state(corrupted)
    assert module.snapshot_state() == before


def test_duplicate_processed_row_rejected() -> None:
    module = populated_module()
    state = deepcopy(dict(module.snapshot_state()))
    state["processed"] = list(state["processed"]) * 2
    with pytest.raises(ValueError, match="duplicate setup_id"):
        module.restore_state(state)


def test_duplicate_source_row_rejected() -> None:
    module = populated_module()
    state = deepcopy(dict(module.snapshot_state()))
    state["seen_features"] = list(state["seen_features"]) * 2
    with pytest.raises(ValueError, match="duplicate source_id"):
        module.restore_state(state)


def test_oversized_state_rejected_not_trimmed() -> None:
    module = EntryModelModule(max_processed_setups=1)
    state = dict(module.snapshot_state())
    state["processed"] = [
        {"setup_id": "a", "fingerprint": "a" * 64, "outcome": "planned", "output_id": "x"},
        {"setup_id": "b", "fingerprint": "b" * 64, "outcome": "planned", "output_id": "y"},
    ]
    with pytest.raises(ValueError, match="exceeds"):
        module.restore_state(state)


@pytest.mark.parametrize(
    "bad_stream",
    [
        {"symbol": "BTCUSDT", "timeframe": None, "last_bar_time": None, "last_bar_signature": None},
        {"symbol": "", "timeframe": "5m", "last_bar_time": None, "last_bar_signature": None},
        {
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "last_bar_time": "bad",
            "last_bar_signature": ["x"] * 8,
        },
        {
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "last_bar_time": None,
            "last_bar_signature": ["x"] * 8,
        },
        {
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "last_bar_time": "2026-07-21T12:00:00+00:00",
            "last_bar_signature": ["x"],
        },
        {
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "last_bar_time": "2026-07-21T12:00:00",
            "last_bar_signature": ["x"] * 8,
        },
    ],
)
def test_invalid_stream_state_rejected(bad_stream: object) -> None:
    module = EntryModelModule()
    state = dict(module.snapshot_state())
    state["stream"] = bad_stream
    with pytest.raises(ValueError):
        module.restore_state(state)


@pytest.mark.parametrize("bad_counter", [{"x": -1}, {"x": True}, {1: 1}])
def test_invalid_counters_rejected(bad_counter: object) -> None:
    module = EntryModelModule()
    state = dict(module.snapshot_state())
    state["counters"] = bad_counter
    with pytest.raises(ValueError):
        module.restore_state(state)


def test_runtime_state_is_bounded() -> None:
    module = EntryModelModule(max_processed_setups=3, max_setup_age_seconds=600)
    for index in range(6):
        module.on_bar(view_at(index, features=(next_feature(index),)))
    state = module.snapshot_state()
    assert len(state["processed"]) == 3
    assert len(state["seen_features"]) == 3


def test_state_helpers_validate_fingerprint_and_outcome() -> None:
    state = EntryModelState(2, "c" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        state.remember("x", "bad", "planned")
    with pytest.raises(ValueError, match="outcome"):
        state.remember("x", "a" * 64, "unknown")


def test_state_lru_eviction_is_deterministic() -> None:
    state = EntryModelState(2, "c" * 64)
    state.remember("a", "a" * 64, "planned")
    state.remember("b", "b" * 64, "planned")
    state.remember("a", "a" * 64, "duplicate")
    state.remember("c", "c" * 64, "planned")
    assert tuple(state.processed) == ("a", "c")
