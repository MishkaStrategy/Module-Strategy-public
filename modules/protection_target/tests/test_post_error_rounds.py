from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import make_bar, make_entry_feature, make_structural_feature, make_view
from modular_core import Candle, Feature, FeatureStore, MarketView

from modular_strategy_protection_target import (
    ProtectionTargetModule,
    ProtectionTargetStateError,
)


def _plan_value(module: ProtectionTargetModule, view: MarketView) -> dict[str, object]:
    decision = module.on_bar(view)
    plans = [feature for feature in decision.features if feature.key == "protection_target.plan"]
    assert len(plans) == 1
    return dict(plans[0].value)


def _resign(state: dict[str, object]) -> None:
    unsigned = dict(state)
    unsigned.pop("state_digest", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    state["state_digest"] = hashlib.sha256(encoded).hexdigest()


def test_round_01_fixed_r_mathematical_identity() -> None:
    bar = make_bar()
    feature = make_entry_feature(bar, entry_price="123.456", invalidation_price="120.123")
    value = _plan_value(
        ProtectionTargetModule(reward_r="4.25", min_stop_distance_bps=0),
        make_view(bar, feature),
    )
    risk = Decimal(str(value["risk_distance"]))
    reward = Decimal(str(value["reward_distance"]))
    assert reward == risk * Decimal("4.25")
    assert Decimal(str(value["take_profit"])) == Decimal("123.456") + reward


def test_round_02_outward_buffer_monotonicity() -> None:
    bar = make_bar()
    for side, invalidation, comparator in (("buy", "99", -1), ("sell", "101", 1)):
        entry = make_entry_feature(bar, side=side, invalidation_price=invalidation)
        base = _plan_value(ProtectionTargetModule(stop_buffer_bps=0), make_view(bar, entry))
        buffered = _plan_value(ProtectionTargetModule(stop_buffer_bps=50), make_view(bar, entry))
        base_stop = Decimal(str(base["stop_loss"]))
        buffered_stop = Decimal(str(buffered["stop_loss"]))
        assert (buffered_stop - base_stop).compare(Decimal(0)) == Decimal(comparator)
        assert Decimal(str(buffered["risk_distance"])) > Decimal(str(base["risk_distance"]))


def test_round_03_core_timestamp_normalization_is_utc() -> None:
    offset = timezone(timedelta(hours=3))
    bar = Candle(
        timestamp=datetime(2026, 1, 1, 3, tzinfo=offset),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    feature = make_entry_feature(bar)
    output = ProtectionTargetModule().on_bar(make_view(bar, feature)).features[0]
    assert bar.timestamp.tzinfo is UTC
    assert output.created_at.tzinfo is UTC
    assert str(output.value["created_at"]).endswith("+00:00")


def test_round_04_future_structural_feature_is_not_visible() -> None:
    bar = make_bar()
    entry = make_entry_feature(bar, target_price=None)
    current = make_structural_feature(
        bar,
        key="structure.target",
        price="104",
        feature_id="current",
        setup_id="setup-1",
    )
    future = make_structural_feature(
        bar,
        key="structure.target",
        price="999",
        feature_id="future",
        setup_id="setup-1",
        created_at=bar.close_time + timedelta(minutes=5),
        valid_until=bar.close_time + timedelta(minutes=10),
    )
    value = _plan_value(
        ProtectionTargetModule(
            target_mode="structural",
            structural_target_source="feature",
            target_feature_key="structure.target",
        ),
        make_view(bar, entry, current, future),
    )
    assert value["take_profit"] == "104"


def test_round_05_module_rejects_non_string_payload_schema() -> None:
    bar = make_bar()
    feature = make_entry_feature(bar, extra={"schema_version": 1})
    decision = ProtectionTargetModule().on_bar(make_view(bar, feature))
    assert decision.features[0].key == "protection_target.rejected"
    assert decision.features[0].value["reason"] == "protection_target.invalid_entry_plan"


def test_round_06_producer_substitution_preserves_plan_identity() -> None:
    bar = make_bar()
    first = make_entry_feature(bar, source_module="entry_model", feature_id="stable:feature")
    second = make_entry_feature(bar, source_module="alternative_entry", feature_id="stable:feature")
    first_plan = _plan_value(ProtectionTargetModule(), make_view(bar, first))
    second_plan = _plan_value(ProtectionTargetModule(), make_view(bar, second))
    assert first_plan["protection_plan_id"] == second_plan["protection_plan_id"]


def test_round_07_plan_output_has_exact_public_fields() -> None:
    bar = make_bar()
    value = _plan_value(ProtectionTargetModule(), make_view(bar, make_entry_feature(bar)))
    assert set(value) == {
        "schema_version",
        "protection_plan_id",
        "entry_plan_id",
        "setup_id",
        "symbol",
        "timeframe",
        "side",
        "entry_price",
        "invalidation_price",
        "stop_loss",
        "take_profit",
        "risk_distance",
        "reward_distance",
        "stop_distance_bps",
        "reward_r",
        "invalidation_mode",
        "target_mode",
        "created_at",
        "valid_until",
        "source_entry_feature_id",
        "source_invalidation_feature_id",
        "source_target_feature_id",
    }


def test_round_08_rejection_feature_identifier_is_deterministic() -> None:
    bar = make_bar()
    malformed = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=bar.close_time,
        value={"entry_plan_id": "bad", "setup_id": "bad"},
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        valid_until=bar.close_time,
        feature_id="malformed",
    )
    first = ProtectionTargetModule().on_bar(make_view(bar, malformed)).features[0]
    second = ProtectionTargetModule().on_bar(make_view(bar, malformed)).features[0]
    assert first.feature_id == second.feature_id
    assert first.value == second.value


def test_round_09_unknown_constructor_parameter_is_rejected() -> None:
    with pytest.raises(TypeError, match="unexpected keyword"):
        ProtectionTargetModule(unknown_parameter=True)  # type: ignore[call-arg]


def test_round_10_configuration_fingerprint_is_semantic() -> None:
    equal_a = ProtectionTargetModule(reward_r="3.0")
    equal_b = ProtectionTargetModule(reward_r="3.000")
    changed = ProtectionTargetModule(reward_r="4")
    assert (
        equal_a.snapshot_state()["config_fingerprint"]
        == equal_b.snapshot_state()["config_fingerprint"]
    )
    assert (
        equal_a.snapshot_state()["config_fingerprint"]
        != changed.snapshot_state()["config_fingerprint"]
    )


def test_round_11_processed_state_remains_bounded_after_restore() -> None:
    module = ProtectionTargetModule(max_processed_plans=3)
    for index in range(5):
        bar = make_bar(index)
        module.on_bar(
            make_view(
                bar, make_entry_feature(bar, plan_id=f"entry-{index}", setup_id=f"setup-{index}")
            )
        )
    restored = ProtectionTargetModule(max_processed_plans=3)
    restored.restore_state(module.snapshot_state())
    next_bar = make_bar(5)
    restored.on_bar(
        make_view(next_bar, make_entry_feature(next_bar, plan_id="entry-5", setup_id="setup-5"))
    )
    rows = restored.snapshot_state()["processed"]
    assert len(rows) == 3
    assert [row["process_key"] for row in rows] == ["entry-3", "entry-4", "entry-5"]


def test_round_12_lifecycle_hooks_do_not_mutate_checkpoint() -> None:
    module = ProtectionTargetModule()
    before = module.snapshot_state()
    module.on_start()
    module.on_finish()
    assert module.snapshot_state() == before


def test_round_13_json_checkpoint_roundtrip_is_exact() -> None:
    bar = make_bar()
    module = ProtectionTargetModule()
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    encoded = json.dumps(module.snapshot_state(), sort_keys=True)
    decoded = json.loads(encoded)
    restored = ProtectionTargetModule()
    restored.restore_state(decoded)
    assert restored.snapshot_state() == decoded


def test_round_14_incompatible_patch_state_is_rejected() -> None:
    bar = make_bar()
    module = ProtectionTargetModule()
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    state = deepcopy(dict(module.snapshot_state()))
    state["module_version"] = "1.1.2"
    _resign(state)
    with pytest.raises(ProtectionTargetStateError, match="module_version mismatch"):
        ProtectionTargetModule().restore_state(state)


def test_round_15_exact_duplicate_after_restore_is_idempotent() -> None:
    bar = make_bar()
    view = make_view(bar, make_entry_feature(bar))
    module = ProtectionTargetModule()
    module.on_bar(view)
    restored = ProtectionTargetModule()
    restored.restore_state(module.snapshot_state())
    decision = restored.on_bar(view)
    assert decision.features == ()
    assert decision.skips == ("protection_target.duplicate_event_ignored",)
    assert restored.metrics["duplicate_bars"] == 1


def test_round_16_gap_does_not_consume_visible_plan() -> None:
    first_bar = make_bar(0)
    next_bar = make_bar(1)
    entry = make_entry_feature(first_bar, valid_until=next_bar.close_time)
    module = ProtectionTargetModule(require_same_bar_entry=False)
    assert module.on_bar(make_view(first_bar, entry, data_gap=True)).features == ()
    decision = module.on_bar(make_view(next_bar, entry))
    assert decision.features[0].value["entry_plan_id"] == "entry-1"


def test_round_17_manifest_features_are_unique_and_namespaced() -> None:
    manifest = ProtectionTargetModule().manifest
    all_keys = (*manifest.required_features, *manifest.produced_features)
    assert len(manifest.produced_features) == len(set(manifest.produced_features))
    assert all("." in key and not any(character.isspace() for character in key) for key in all_keys)
    assert set(manifest.required_features).isdisjoint(manifest.produced_features)


def test_round_18_expired_entry_is_absent_from_feature_store_snapshot() -> None:
    first_bar = make_bar(0)
    next_bar = make_bar(1)
    feature = make_entry_feature(first_bar, valid_until=first_bar.close_time)
    store = FeatureStore()
    store.publish(feature)
    view = MarketView(
        base_bars=(next_bar,),
        timeframe_bars={},
        index=1,
        cash=1,
        equity=1,
        position=None,
        pending_orders=(),
        features=store.snapshot(next_bar.close_time),
    )
    decision = ProtectionTargetModule().on_bar(view)
    assert decision.features == ()
    assert decision.skips == ("protection_target.no_entry_plan",)


def test_round_19_source_module_is_not_a_hidden_strategy_dependency() -> None:
    bar = make_bar()
    canonical = make_entry_feature(bar, source_module="entry_model", feature_id="same-id")
    alternative = make_entry_feature(bar, source_module="third_party_entry", feature_id="same-id")
    first = _plan_value(ProtectionTargetModule(), make_view(bar, canonical))
    second = _plan_value(ProtectionTargetModule(), make_view(bar, alternative))
    assert first == second


def test_round_20_downstream_payload_uses_decimal_strings_only() -> None:
    bar = make_bar()
    value = _plan_value(ProtectionTargetModule(), make_view(bar, make_entry_feature(bar)))
    decimal_fields = {
        "entry_price",
        "invalidation_price",
        "stop_loss",
        "take_profit",
        "risk_distance",
        "reward_distance",
        "stop_distance_bps",
        "reward_r",
    }
    assert all(isinstance(value[field], str) for field in decimal_fields)
    serialized = json.dumps(value, sort_keys=True)
    assert "NaN" not in serialized and "Infinity" not in serialized


def test_round_25_observability_counters_cover_accept_reject_duplicate_and_gap() -> None:
    module = ProtectionTargetModule(require_same_bar_entry=False)
    first_bar = make_bar(0)
    third_bar = make_bar(2)
    entry = make_entry_feature(first_bar, valid_until=third_bar.close_time)
    module.on_bar(make_view(first_bar, entry))

    second_bar = make_bar(1)
    malformed = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=second_bar.close_time,
        value={"entry_plan_id": "bad", "setup_id": "bad"},
        symbol=second_bar.symbol,
        timeframe=second_bar.timeframe,
        valid_until=second_bar.close_time,
        feature_id="bad:feature",
    )
    module.on_bar(make_view(second_bar, malformed))
    module.on_bar(make_view(third_bar, entry))

    fourth_bar = make_bar(3)
    module.on_bar(make_view(fourth_bar, data_gap=True))
    metrics = dict(module.metrics)
    assert metrics == {
        "bars_seen": 4,
        "gap_bars": 1,
        "accepted_plans": 1,
        "rejected_plans": 1,
        "duplicate_bars": 0,
        "duplicate_plans": 1,
        "missing_entry_bars": 0,
        "processed_plans": 2,
    }
