from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import make_bar, make_entry_feature, make_view
from modular_core import Candle

from modular_strategy_protection_target import (
    ProtectionTargetDataError,
    ProtectionTargetModule,
    ProtectionTargetStateError,
)


def signed_state(module: ProtectionTargetModule, bar: Candle) -> dict[str, object]:
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    return dict(module.snapshot_state())


def test_snapshot_v2_is_json_compatible_and_signed(bar: Candle) -> None:
    module = ProtectionTargetModule()
    state = signed_state(module, bar)
    assert state["state_version"] == 2
    assert state["module_version"] == "1.1.1"
    assert len(str(state["state_digest"])) == 64
    json.dumps(state, sort_keys=True)
    restored = ProtectionTargetModule()
    restored.restore_state(state)
    assert restored.snapshot_state() == state


def test_restore_prevents_duplicate_republication(bar: Candle) -> None:
    module = ProtectionTargetModule(require_same_bar_entry=False)
    entry = make_entry_feature(bar, valid_until=bar.close_time + timedelta(minutes=10))
    module.on_bar(make_view(bar, entry))
    restored = ProtectionTargetModule(require_same_bar_entry=False)
    restored.restore_state(module.snapshot_state())
    next_bar = make_bar(1)
    decision = restored.on_bar(make_view(next_bar, entry))
    assert decision.skips == ("protection_target.entry_plan_already_processed",)


def test_restore_continues_with_new_plan(bar: Candle) -> None:
    source = ProtectionTargetModule()
    source.on_bar(make_view(bar, make_entry_feature(bar)))
    target = ProtectionTargetModule()
    target.restore_state(source.snapshot_state())
    next_bar = make_bar(1)
    entry = make_entry_feature(next_bar, plan_id="entry-2", setup_id="setup-2")
    decision = target.on_bar(make_view(next_bar, entry))
    assert decision.features[0].value["entry_plan_id"] == "entry-2"


def test_restore_is_transactional_on_digest_error(bar: Candle) -> None:
    module = ProtectionTargetModule()
    before = module.snapshot_state()
    state = signed_state(ProtectionTargetModule(), bar)
    state["counters"]["bars_seen"] = 999
    with pytest.raises(ProtectionTargetStateError, match="digest mismatch"):
        module.restore_state(state)
    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda state: state.update(state_version=99), "state_version"),
        (lambda state: state.update(module_version="9.9.9"), "module_version"),
        (lambda state: state.update(extra="bad"), "unknown state keys"),
        (lambda state: state.update(state_digest="bad"), "state_digest"),
    ],
)
def test_top_level_corruptions_rejected(bar: Candle, mutator: object, message: str) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    mutator(state)
    with pytest.raises(ProtectionTargetStateError, match=message):
        ProtectionTargetModule().restore_state(state)


def test_config_change_blocks_restore(bar: Candle) -> None:
    source = ProtectionTargetModule(reward_r=3)
    state = signed_state(source, bar)
    with pytest.raises(ProtectionTargetStateError, match="config_fingerprint"):
        ProtectionTargetModule(reward_r=4).restore_state(state)


def resign(state: dict[str, object]) -> None:
    from modular_strategy_protection_target.state import _digest

    unsigned = dict(state)
    unsigned.pop("state_digest", None)
    state["state_digest"] = _digest(unsigned)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"bound_symbol": "BTCUSDT", "bound_timeframe": None}, "set together"),
        ({"last_bar_signature": ["x"]}, "eight"),
        ({"processed": "bad"}, "processed must be a sequence"),
        ({"counters": {}}, "counters fields"),
        ({"last_event_fingerprint": "bad"}, "last_event_fingerprint"),
    ],
)
def test_resigned_semantic_corruptions_rejected(
    bar: Candle, change: dict[str, object], message: str
) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    state.update(change)
    resign(state)
    with pytest.raises(ProtectionTargetStateError, match=message):
        ProtectionTargetModule().restore_state(state)


def test_processed_state_is_bounded() -> None:
    module = ProtectionTargetModule(max_processed_plans=2)
    for index in range(3):
        bar = make_bar(index)
        entry = make_entry_feature(
            bar,
            plan_id=f"entry-{index}",
            setup_id=f"setup-{index}",
        )
        module.on_bar(make_view(bar, entry))
    processed = module.snapshot_state()["processed"]
    assert [row["process_key"] for row in processed] == ["entry-1", "entry-2"]


def test_independent_instances_do_not_share_state() -> None:
    btc = make_bar(symbol="BTCUSDT")
    eth = make_bar(symbol="ETHUSDT")
    first = ProtectionTargetModule()
    second = ProtectionTargetModule()
    first.on_bar(make_view(btc, make_entry_feature(btc)))
    second.on_bar(make_view(eth, make_entry_feature(eth)))
    assert first.snapshot_state()["bound_symbol"] == "BTCUSDT"
    assert second.snapshot_state()["bound_symbol"] == "ETHUSDT"


def test_v1_snapshot_migration(bar: Candle) -> None:
    current = ProtectionTargetModule()
    current.on_bar(make_view(bar, make_entry_feature(bar)))
    state = dict(current.snapshot_state())
    state.pop("state_digest")
    state.pop("last_event_fingerprint")
    state["state_version"] = 1
    state["module_version"] = "1.0.0"
    state["config_fingerprint"] = current.config.legacy_fingerprint_v1()
    migrated = ProtectionTargetModule()
    migrated.restore_state(state)
    migrated_state = migrated.snapshot_state()
    assert migrated_state["state_version"] == 2
    assert migrated_state["last_event_fingerprint"] is None
    with pytest.raises(ProtectionTargetDataError, match="cannot be verified"):
        migrated.on_bar(make_view(bar, make_entry_feature(bar)))
    next_bar = make_bar(1)
    next_entry = make_entry_feature(next_bar, plan_id="entry-2", setup_id="setup-2")
    assert migrated.on_bar(make_view(next_bar, next_entry)).features


def test_legacy_wrong_version_and_config_rejected(bar: Candle) -> None:
    module = ProtectionTargetModule()
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    state = dict(module.snapshot_state())
    state.pop("state_digest")
    state.pop("last_event_fingerprint")
    state["state_version"] = 1
    state["module_version"] = "0.9.0"
    state["config_fingerprint"] = module.config.legacy_fingerprint_v1()
    with pytest.raises(ProtectionTargetStateError, match="legacy module_version"):
        ProtectionTargetModule().restore_state(state)
    state["module_version"] = "1.0.0"
    state["config_fingerprint"] = "0" * 64
    with pytest.raises(ProtectionTargetStateError, match="config_fingerprint"):
        ProtectionTargetModule().restore_state(state)


def test_restore_rejects_non_string_bar_signature_items(bar: Candle) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    signature = list(state["last_bar_signature"])
    signature[1] = 100
    state["last_bar_signature"] = signature
    resign(state)
    with pytest.raises(ProtectionTargetStateError, match="items must be strings"):
        ProtectionTargetModule().restore_state(state)


def test_restore_rejects_surrounding_whitespace_in_state_identity(bar: Candle) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    state["bound_symbol"] = " BTCUSDT"
    signature = list(state["last_bar_signature"])
    signature[6] = " BTCUSDT"
    state["last_bar_signature"] = signature
    resign(state)
    with pytest.raises(ProtectionTargetStateError, match="surrounding whitespace"):
        ProtectionTargetModule().restore_state(state)


def test_event_fingerprint_is_fixed_size_in_checkpoint(bar: Candle) -> None:
    features = tuple(
        make_entry_feature(
            bar,
            plan_id=f"entry-{index}",
            setup_id=f"setup-{index}",
            feature_id=f"feature-{index}",
        )
        for index in range(100)
    )
    module = ProtectionTargetModule(max_processed_plans=200)
    module.on_bar(make_view(bar, *features))
    state = module.snapshot_state()
    assert isinstance(state["last_event_fingerprint"], str)
    assert len(state["last_event_fingerprint"]) == 64


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("time_without_signature", "set together"),
        ("signature_without_time", "set together"),
        ("cursor_without_stream", "bar cursor requires"),
        ("timestamp_mismatch", "timestamp must match"),
        ("stream_mismatch", "stream identity"),
        ("nonfinite_price", "must be finite"),
        ("zero_price", "prices must be positive"),
        ("negative_volume", "volume must be non-negative"),
        ("bad_high", "OHLC geometry"),
        ("bad_low", "OHLC geometry"),
        ("processed_nonmapping", "rows must be mappings"),
        ("processed_bad_fields", "invalid fields"),
        ("processed_null_key", "cannot be null"),
        ("processed_oversized_key", "exceeds 512"),
        ("processed_duplicate_key", "must be unique"),
        ("processed_bad_hash", "lowercase SHA-256"),
        ("counters_nonmapping", "counters must be a mapping"),
        ("counter_bool", "non-negative integer"),
        ("counter_negative", "non-negative integer"),
        ("event_without_cursor", "requires a bar cursor"),
        ("naive_cursor", "timezone-aware"),
    ],
)
def test_resigned_adversarial_checkpoint_cases(bar: Candle, case: str, message: str) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    signature = list(state["last_bar_signature"])
    if case == "time_without_signature":
        state["last_bar_signature"] = None
    elif case == "signature_without_time":
        state["last_bar_time"] = None
    elif case == "cursor_without_stream":
        state["bound_symbol"] = None
        state["bound_timeframe"] = None
    elif case == "timestamp_mismatch":
        signature[0] = "2025-12-31T00:00:00+00:00"
        state["last_bar_signature"] = signature
    elif case == "stream_mismatch":
        signature[6] = "ETHUSDT"
        state["last_bar_signature"] = signature
    elif case == "nonfinite_price":
        signature[1] = "NaN"
        state["last_bar_signature"] = signature
    elif case == "zero_price":
        signature[1] = "0"
        state["last_bar_signature"] = signature
    elif case == "negative_volume":
        signature[5] = "-1"
        state["last_bar_signature"] = signature
    elif case == "bad_high":
        signature[2] = "90"
        state["last_bar_signature"] = signature
    elif case == "bad_low":
        signature[3] = "110"
        state["last_bar_signature"] = signature
    elif case == "processed_nonmapping":
        state["processed"] = ["bad"]
    elif case == "processed_bad_fields":
        state["processed"] = [{"process_key": "entry-1"}]
    elif case == "processed_null_key":
        state["processed"] = [{"process_key": None, "feature_fingerprint": "0" * 64}]
    elif case == "processed_oversized_key":
        state["processed"] = [{"process_key": "x" * 513, "feature_fingerprint": "0" * 64}]
    elif case == "processed_duplicate_key":
        row = dict(state["processed"][0])
        state["processed"] = [row, dict(row)]
    elif case == "processed_bad_hash":
        state["processed"][0]["feature_fingerprint"] = "BAD"
    elif case == "counters_nonmapping":
        state["counters"] = "bad"
    elif case == "counter_bool":
        state["counters"]["bars_seen"] = True
    elif case == "counter_negative":
        state["counters"]["bars_seen"] = -1
    elif case == "event_without_cursor":
        state["bound_symbol"] = None
        state["bound_timeframe"] = None
        state["last_bar_time"] = None
        state["last_bar_signature"] = None
        state["last_event_fingerprint"] = "0" * 64
    elif case == "naive_cursor":
        state["last_bar_time"] = "2026-01-01T00:00:00"
        signature[0] = "2026-01-01T00:00:00"
        state["last_bar_signature"] = signature
    else:
        raise AssertionError(case)
    resign(state)
    with pytest.raises(ProtectionTargetStateError, match=message):
        ProtectionTargetModule().restore_state(state)


@pytest.mark.parametrize("state_version", [True, 2.0, "2"])
def test_state_version_requires_exact_integer_type(bar: Candle, state_version: object) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    state["state_version"] = state_version
    with pytest.raises(ProtectionTargetStateError, match="state_version"):
        ProtectionTargetModule().restore_state(state)


def test_restore_wraps_non_json_state_values(bar: Candle) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    state["counters"]["bars_seen"] = object()
    with pytest.raises(ProtectionTargetStateError, match="JSON-compatible"):
        ProtectionTargetModule().restore_state(state)


def test_restore_accepts_compatible_1_1_0_state(bar: Candle) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    state["module_version"] = "1.1.0"
    resign(state)
    restored = ProtectionTargetModule()
    restored.restore_state(state)
    assert restored.snapshot_state()["module_version"] == "1.1.1"


def test_restore_rejects_oversized_checkpoint_strings(bar: Candle) -> None:
    state = signed_state(ProtectionTargetModule(), bar)
    state["bound_symbol"] = "x" * 513
    signature = list(state["last_bar_signature"])
    signature[6] = "x" * 513
    state["last_bar_signature"] = signature
    resign(state)
    with pytest.raises(ProtectionTargetStateError, match="512"):
        ProtectionTargetModule().restore_state(state)

    state = signed_state(ProtectionTargetModule(), bar)
    signature = list(state["last_bar_signature"])
    signature[1] = "1" * 1025
    state["last_bar_signature"] = signature
    resign(state)
    with pytest.raises(ProtectionTargetStateError, match="too large"):
        ProtectionTargetModule().restore_state(state)
