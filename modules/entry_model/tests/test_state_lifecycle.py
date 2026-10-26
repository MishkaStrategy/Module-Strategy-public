from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest

from modular_modules.entry_model import EntryModelDataError, EntryModelModule, EntryPlan
from modular_modules.entry_model.state import EntryModelState, ProcessedRecord, SourceRecord

from .support import bar_at, next_feature, payload, setup_feature, view_at

FP_A = "a" * 64
FP_B = "b" * 64


def populated_state(max_items: int = 3) -> EntryModelState:
    state = EntryModelState(max_items, FP_A)
    state.bound_symbol = "BTCUSDT"
    state.bound_timeframe = "5m"
    state.last_bar_time = bar_at(0).timestamp
    state.last_bar_signature = (
        bar_at(0).timestamp.isoformat(),
        "100.0",
        "101.0",
        "99.0",
        "100.0",
        "1.0",
        "BTCUSDT",
        "5m",
    )
    state.remember("setup-1", FP_A, "planned", "plan-1")
    state.remember_feature("feature-1", FP_B, "planned")
    state.increment("plans")
    state.increment("bars_processed", 2)
    return state


def test_state_records_and_bounded_eviction() -> None:
    state = EntryModelState(2, FP_A)
    for index in range(4):
        fp = f"{index:x}" * 64
        state.remember(f"s{index}", fp, "planned", f"p{index}")
        state.remember_feature(f"f{index}", fp, "rejected")
    assert list(state.processed) == ["s2", "s3"]
    assert list(state.seen_features) == ["f2", "f3"]
    state.remember("s2", "e" * 64, "conflict")
    assert list(state.processed) == ["s3", "s2"]
    state.remember_feature("f2", "e" * 64, "duplicate")
    assert list(state.seen_features) == ["f3", "f2"]


def test_state_increment_and_snapshot_are_json_compatible() -> None:
    state = populated_state()
    state.increment("plans", 2)
    snapshot = state.snapshot()
    assert snapshot["state_schema_version"] == "2"
    assert snapshot["config_fingerprint"] == FP_A
    assert snapshot["stream"]["symbol"] == "BTCUSDT"  # type: ignore[index]
    assert snapshot["counters"] == {"bars_processed": 2, "plans": 3}
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "setup-1" in encoded


def test_state_restore_roundtrip() -> None:
    original = populated_state()
    snapshot = original.snapshot()
    restored = EntryModelState(3, FP_A)
    restored.restore(snapshot)
    assert restored.snapshot() == snapshot
    assert restored.processed["setup-1"] == ProcessedRecord(FP_A, "planned", "plan-1")
    assert restored.seen_features["feature-1"] == SourceRecord(FP_B, "planned")
    assert restored.last_bar_time == bar_at(0).timestamp


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda s: s.update({"unknown": 1}), "unknown entry model state keys"),
        (
            lambda s: s.update({"state_schema_version": "1"}),
            "incompatible entry model state schema",
        ),
        (lambda s: s.update({"config_fingerprint": FP_B}), "config fingerprint mismatch"),
        (lambda s: s.update({"processed": "bad"}), "processed must be a sequence"),
        (lambda s: s.update({"seen_features": "bad"}), "seen_features must be a sequence"),
        (lambda s: s.update({"stream": []}), "stream state must be a mapping"),
        (lambda s: s.update({"counters": []}), "counters must be a mapping"),
    ],
)
def test_restore_rejects_top_level_corruption(mutator: object, match: str) -> None:
    snapshot = copy.deepcopy(populated_state().snapshot())
    mutator(snapshot)  # type: ignore[operator]
    with pytest.raises(ValueError, match=match):
        EntryModelState(3, FP_A).restore(snapshot)


def test_restore_is_atomic_on_failure() -> None:
    state = populated_state()
    before = copy.deepcopy(state.snapshot())
    corrupt = copy.deepcopy(before)
    corrupt["processed"][0]["fingerprint"] = "bad"  # type: ignore[index]
    with pytest.raises(ValueError):
        state.restore(corrupt)
    assert state.snapshot() == before


def test_restore_rejects_oversized_state() -> None:
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["processed"] = [
        {"setup_id": f"s{i}", "fingerprint": FP_A, "outcome": "planned", "output_id": None}
        for i in range(4)
    ]
    with pytest.raises(ValueError, match="processed state exceeds"):
        EntryModelState(3, FP_A).restore(snapshot)
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["seen_features"] = [
        {"source_id": f"f{i}", "fingerprint": FP_A, "outcome": "planned"} for i in range(4)
    ]
    with pytest.raises(ValueError, match="seen-feature state exceeds"):
        EntryModelState(3, FP_A).restore(snapshot)


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("bad", "processed row must be a mapping"),
        (
            {"setup_id": "s", "fingerprint": FP_A, "outcome": "planned", "output_id": None, "x": 1},
            "unknown fields",
        ),
        (
            {"setup_id": "", "fingerprint": FP_A, "outcome": "planned", "output_id": None},
            "invalid entry model processed row",
        ),
        (
            {"setup_id": "s", "fingerprint": FP_A, "outcome": "planned", "output_id": ""},
            "invalid entry model processed row",
        ),
        (
            {"setup_id": "s", "fingerprint": "bad", "outcome": "planned", "output_id": None},
            "fingerprint",
        ),
        ({"setup_id": "s", "fingerprint": FP_A, "outcome": "bad", "output_id": None}, "outcome"),
    ],
)
def test_restore_rejects_invalid_processed_rows(row: object, match: str) -> None:
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["processed"] = [row]
    with pytest.raises(ValueError, match=match):
        EntryModelState(3, FP_A).restore(snapshot)


def test_restore_rejects_duplicate_processed_id() -> None:
    row = {"setup_id": "s", "fingerprint": FP_A, "outcome": "planned", "output_id": None}
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["processed"] = [row, dict(row)]
    with pytest.raises(ValueError, match="duplicate setup_id"):
        EntryModelState(3, FP_A).restore(snapshot)


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("bad", "seen-feature row must be a mapping"),
        ({"source_id": "f", "fingerprint": FP_A, "outcome": "planned", "x": 1}, "unknown fields"),
        (
            {"source_id": "", "fingerprint": FP_A, "outcome": "planned"},
            "invalid entry model seen-feature row",
        ),
        ({"source_id": "f", "fingerprint": "bad", "outcome": "planned"}, "fingerprint"),
        ({"source_id": "f", "fingerprint": FP_A, "outcome": "bad"}, "outcome"),
    ],
)
def test_restore_rejects_invalid_seen_rows(row: object, match: str) -> None:
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["seen_features"] = [row]
    with pytest.raises(ValueError, match=match):
        EntryModelState(3, FP_A).restore(snapshot)


def test_restore_rejects_duplicate_source_id() -> None:
    row = {"source_id": "f", "fingerprint": FP_A, "outcome": "planned"}
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["seen_features"] = [row, dict(row)]
    with pytest.raises(ValueError, match="duplicate source_id"):
        EntryModelState(3, FP_A).restore(snapshot)


@pytest.mark.parametrize(
    ("stream", "match"),
    [
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "last_bar_time": None,
                "last_bar_signature": None,
                "x": 1,
            },
            "unknown fields",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": None,
                "last_bar_time": None,
                "last_bar_signature": None,
            },
            "both set or both absent",
        ),
        (
            {"symbol": "", "timeframe": "5m", "last_bar_time": None, "last_bar_signature": None},
            "cannot be blank",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "last_bar_time": "bad",
                "last_bar_signature": ["x"] * 8,
            },
            "last_bar_time",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "last_bar_time": "2026-07-21T12:00:00",
                "last_bar_signature": ["x"] * 8,
            },
            "timezone-aware",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "last_bar_time": datetime(2026, 7, 21, 12, 0, tzinfo=UTC).isoformat(),
                "last_bar_signature": "bad",
            },
            "last_bar_signature must be a sequence",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "last_bar_time": datetime(2026, 7, 21, 12, 0, tzinfo=UTC).isoformat(),
                "last_bar_signature": ["x"] * 7,
            },
            "invalid entry model stream",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "last_bar_time": None,
                "last_bar_signature": ["x"] * 8,
            },
            "cursor must be complete",
        ),
        (
            {
                "symbol": None,
                "timeframe": None,
                "last_bar_time": datetime(2026, 7, 21, 12, 0, tzinfo=UTC).isoformat(),
                "last_bar_signature": ["x"] * 8,
            },
            "cursor requires bound stream",
        ),
    ],
)
def test_restore_rejects_invalid_stream(stream: object, match: str) -> None:
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["stream"] = stream
    with pytest.raises(ValueError, match=match):
        EntryModelState(3, FP_A).restore(snapshot)


@pytest.mark.parametrize(
    ("counters", "match"),
    [
        ({1: 1}, "counter names"),
        ({"": 1}, "counter names"),
        ({"x": True}, "non-negative integers"),
        ({"x": -1}, "non-negative integers"),
        ({"x": 1.2}, "non-negative integers"),
    ],
)
def test_restore_rejects_invalid_counters(counters: object, match: str) -> None:
    snapshot = copy.deepcopy(populated_state().snapshot())
    snapshot["counters"] = counters
    with pytest.raises(ValueError, match=match):
        EntryModelState(3, FP_A).restore(snapshot)


def test_validate_record_rejects_uppercase_and_bad_outcome() -> None:
    state = EntryModelState(1, FP_A)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        state.remember("s", "A" * 64, "planned")
    with pytest.raises(ValueError, match="outcome"):
        state.remember_feature("f", FP_A, "unknown")


def test_module_checkpoint_restore_replay_and_continuation() -> None:
    first = EntryModelModule(max_setup_age_seconds=600)
    feature0 = next_feature(0, setup_id="s0", feature_id="f0")
    decision0 = first.on_bar(view_at(0, features=(feature0,)))
    plan0 = EntryPlan.from_feature_value(decision0.features[0].value)
    snapshot = first.snapshot_state()

    restored = EntryModelModule(max_setup_age_seconds=600)
    restored.restore_state(snapshot)
    replay_feature = setup_feature(
        value=payload(setup_id="s0", confirmed_at=bar_at(0).close_time),
        index=1,
        feature_id="f-replay",
    )
    replay = restored.on_bar(view_at(1, features=(replay_feature,)))
    assert replay.features == ()
    assert "entry_model.duplicate_setup" in replay.skips

    feature2 = next_feature(2, setup_id="s2", feature_id="f2")
    continuation = restored.on_bar(view_at(2, features=(feature2,)))
    plan2 = EntryPlan.from_feature_value(continuation.features[0].value)
    assert plan2.setup_id == "s2"
    assert plan2.plan_id != plan0.plan_id


def test_module_restore_rejects_different_config_and_remains_unchanged() -> None:
    source = EntryModelModule(ttl_bars=2)
    source.on_bar(view_at(0, features=(next_feature(0),)))
    snapshot = source.snapshot_state()
    target = EntryModelModule(ttl_bars=3)
    before = copy.deepcopy(target.snapshot_state())
    with pytest.raises(ValueError, match="config fingerprint mismatch"):
        target.restore_state(snapshot)
    assert target.snapshot_state() == before


def test_module_restore_preserves_stream_cursor_barrier() -> None:
    module = EntryModelModule()
    module.on_bar(view_at(2))
    restored = EntryModelModule()
    restored.restore_state(module.snapshot_state())
    with pytest.raises(EntryModelDataError, match="out-of-order"):
        restored.on_bar(view_at(1))
    duplicate = restored.on_bar(view_at(2))
    assert duplicate.skips == ("entry_model.duplicate_event",)


def test_two_instances_are_independent() -> None:
    one = EntryModelModule()
    two = EntryModelModule()
    decision_one = one.on_bar(view_at(0, features=(next_feature(0, setup_id="same"),)))
    decision_two = two.on_bar(view_at(0, features=(next_feature(0, setup_id="same"),)))
    assert decision_one.features[0].feature_id == decision_two.features[0].feature_id
    assert len(one.snapshot_state()["processed"]) == 1  # type: ignore[arg-type]
    assert len(two.snapshot_state()["processed"]) == 1  # type: ignore[arg-type]


def test_bounded_module_state_under_many_events() -> None:
    module = EntryModelModule(max_processed_setups=3)
    for index in range(6):
        module.on_bar(view_at(index, features=(next_feature(index),)))
    snapshot = module.snapshot_state()
    assert [row["setup_id"] for row in snapshot["processed"]] == ["setup-3", "setup-4", "setup-5"]  # type: ignore[index]
    assert [row["source_id"] for row in snapshot["seen_features"]] == [
        "feature-3",
        "feature-4",
        "feature-5",
    ]  # type: ignore[index]
