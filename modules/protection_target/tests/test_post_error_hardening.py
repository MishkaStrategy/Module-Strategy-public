from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from conftest import make_bar, make_entry_feature, make_structural_feature, make_view
from modular_core import Feature

from modular_strategy_protection_target import (
    InvalidationMode,
    ProtectionTargetConfig,
    ProtectionTargetConfigurationError,
    ProtectionTargetContractError,
    ProtectionTargetDataError,
    ProtectionTargetModule,
    ProtectionTargetReason,
    StructuralTargetSource,
    TargetMode,
)
from modular_strategy_protection_target.contracts import (
    parse_entry_plan_feature,
    select_structural_feature,
)
from modular_strategy_protection_target.state import Counters


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


def _signed_state() -> dict[str, object]:
    bar = make_bar()
    module = ProtectionTargetModule()
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    return deepcopy(dict(module.snapshot_state()))


def test_public_config_rejects_raw_enum_strings() -> None:
    with pytest.raises(ProtectionTargetConfigurationError, match="InvalidationMode"):
        ProtectionTargetConfig(invalidation_mode="entry_plan")  # type: ignore[arg-type]
    with pytest.raises(ProtectionTargetConfigurationError, match="TargetMode"):
        ProtectionTargetConfig(target_mode="fixed_r")  # type: ignore[arg-type]
    with pytest.raises(ProtectionTargetConfigurationError, match="StructuralTargetSource"):
        ProtectionTargetConfig(structural_target_source="entry_plan")  # type: ignore[arg-type]


def test_public_config_accepts_declared_enum_types() -> None:
    config = ProtectionTargetConfig(
        invalidation_mode=InvalidationMode.FIXED_PERCENT,
        target_mode=TargetMode.STRUCTURAL,
        structural_target_source=StructuralTargetSource.ENTRY_PLAN,
    )
    assert config.invalidation_mode is InvalidationMode.FIXED_PERCENT


def test_module_parameters_and_nested_schema_are_immutable() -> None:
    module = ProtectionTargetModule()
    with pytest.raises(TypeError):
        module.parameters["reward_r"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        module.manifest.parameters_schema["reward_r"]["minimum"] = -1  # type: ignore[index]


def test_entry_payload_schema_requires_exact_string() -> None:
    bar = make_bar()
    for invalid in (1, True, 1.0, b"1"):
        feature = make_entry_feature(bar, extra={"schema_version": invalid})
        with pytest.raises(ProtectionTargetContractError, match="schema_version"):
            parse_entry_plan_feature(
                feature,
                expected_symbol=bar.symbol,
                expected_timeframe=bar.timeframe,
            )


def test_boolean_structural_setup_id_never_matches_string_setup() -> None:
    bar = make_bar()
    feature = make_structural_feature(
        bar,
        key="structure.target",
        price="105",
        feature_id="bool-setup",
        setup_id=None,
    )
    raw = dict(feature.value)
    raw["setup_id"] = True
    bool_feature = Feature(
        key=feature.key,
        source_module=feature.source_module,
        created_at=feature.created_at,
        value=raw,
        symbol=feature.symbol,
        timeframe=feature.timeframe,
        valid_until=feature.valid_until,
        feature_id=feature.feature_id,
    )
    assert (
        select_structural_feature((bool_feature,), setup_id="True", require_setup_match=False)
        is None
    )


def test_unfingerprintable_entry_feature_fails_as_data_error() -> None:
    bar = make_bar()
    feature = make_entry_feature(bar, extra={"unsupported": object()})
    with pytest.raises(ProtectionTargetDataError, match="fingerprint"):
        ProtectionTargetModule().on_bar(make_view(bar, feature))


def test_unfingerprintable_structural_feature_fails_as_data_error() -> None:
    bar = make_bar()
    entry = make_entry_feature(bar, invalidation_price=None)
    structure = Feature(
        key="structure.invalidation",
        source_module="structure",
        created_at=bar.close_time,
        value={"setup_id": "setup-1", "price": "99", "unsupported": object()},
        symbol=bar.symbol,
        valid_until=bar.close_time,
        feature_id="bad-structure",
    )
    module = ProtectionTargetModule(
        invalidation_mode="feature",
        invalidation_feature_key="structure.invalidation",
    )
    with pytest.raises(ProtectionTargetDataError, match="fingerprint"):
        module.on_bar(make_view(bar, entry, structure))


def test_state_rejects_non_string_top_level_keys() -> None:
    state = _signed_state()
    state[1] = "bad"  # type: ignore[index]
    with pytest.raises(Exception, match="keys must be strings"):
        ProtectionTargetModule().restore_state(state)


def test_state_rejects_unbounded_or_non_string_module_version() -> None:
    for value in (["1.1.1"], "x" * 65, ""):
        state = _signed_state()
        state["module_version"] = value
        _resign(state)
        with pytest.raises(Exception, match="module_version"):
            ProtectionTargetModule().restore_state(state)


def test_state_requires_sha256_config_fingerprint() -> None:
    state = _signed_state()
    state["config_fingerprint"] = "not-a-hash"
    _resign(state)
    with pytest.raises(Exception, match="config_fingerprint"):
        ProtectionTargetModule().restore_state(state)


def test_state_digest_rejects_nan_before_semantic_restore() -> None:
    state = _signed_state()
    state["counters"]["bars_seen"] = float("nan")  # type: ignore[index]
    with pytest.raises(Exception, match="JSON-compatible"):
        ProtectionTargetModule().restore_state(state)


def test_state_digest_wraps_recursive_values() -> None:
    state = _signed_state()
    recursive: list[object] = []
    recursive.append(recursive)
    state["processed"] = recursive
    with pytest.raises(Exception, match="JSON-compatible"):
        ProtectionTargetModule().restore_state(state)


def test_counters_are_bounded_to_signed_64_bit() -> None:
    maximum = 2**63 - 1
    counters = Counters(bars_seen=maximum)
    with pytest.raises(OverflowError, match="signed 64-bit"):
        counters.increment(bars_seen=1)


def test_reason_codes_are_unique_and_namespaced() -> None:
    values = [reason.value for reason in ProtectionTargetReason]
    assert len(values) == len(set(values))
    assert all(value.startswith("protection_target.") for value in values)


def test_emitted_feature_payload_is_immutable_and_json_compatible() -> None:
    bar = make_bar()
    feature = ProtectionTargetModule().on_bar(make_view(bar, make_entry_feature(bar))).features[0]
    json.dumps(dict(feature.value), sort_keys=True)
    with pytest.raises(TypeError):
        feature.value["take_profit"] = "0"  # type: ignore[index]


def test_plan_identifier_is_hash_seed_independent() -> None:
    script = """
from datetime import UTC, datetime
from modular_core import Candle, Feature, FeatureView, MarketView
from modular_strategy_protection_target import ProtectionTargetModule
bar=Candle(timestamp=datetime(2026,1,1,tzinfo=UTC),open=100,high=101,low=99,close=100,volume=1,symbol='BTCUSDT',timeframe='5m')
feature=Feature(key='entry_model.entry_plan',source_module='entry_model',created_at=bar.close_time,value={'schema_version':'1','entry_plan_id':'entry-seed','setup_id':'setup-seed','symbol':'BTCUSDT','timeframe':'5m','side':'buy','entry_price':'100','invalidation_price':'99','created_at':bar.close_time.isoformat()},symbol='BTCUSDT',timeframe='5m',valid_until=bar.close_time,feature_id='entry-seed:feature')
view=MarketView(base_bars=(bar,),timeframe_bars={},index=0,cash=1,equity=1,position=None,pending_orders=(),features=FeatureView((feature,),bar.close_time))
print(ProtectionTargetModule().on_bar(view).features[0].value['protection_plan_id'])
"""
    outputs: list[str] = []
    for seed in ("1", "2", "999"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        outputs.append(
            subprocess.check_output([sys.executable, "-c", script], env=env, text=True).strip()
        )
    assert len(set(outputs)) == 1


def test_semantically_equal_decimal_configs_have_same_fingerprint() -> None:
    first = ProtectionTargetConfig(reward_r=Decimal("3.0"))
    second = ProtectionTargetConfig(reward_r=Decimal("3.000"))
    assert first.fingerprint() == second.fingerprint()


def test_semantic_config_change_changes_fingerprint() -> None:
    first = ProtectionTargetConfig(max_diagnostic_results=100)
    second = ProtectionTargetConfig(max_diagnostic_results=101)
    assert first.fingerprint() != second.fingerprint()


def test_randomized_buy_sell_symmetry_is_deterministic() -> None:
    generator = random.Random(20260721)
    for index in range(50):
        entry = Decimal(generator.randrange(10_000, 1_000_000)) / Decimal("100")
        distance = Decimal(generator.randrange(1, 5000)) / Decimal("100")
        if distance >= entry:
            continue
        maximum_safe_reward = (entry / distance) * Decimal("0.8")
        reward = min(
            Decimal(generator.randrange(50, 500)) / Decimal("100"),
            maximum_safe_reward,
        )
        bar = make_bar(index)
        buy = make_entry_feature(
            bar,
            plan_id=f"buy-{index}",
            setup_id=f"buy-setup-{index}",
            side="buy",
            entry_price=str(entry),
            invalidation_price=str(entry - distance),
        )
        sell = make_entry_feature(
            bar,
            plan_id=f"sell-{index}",
            setup_id=f"sell-setup-{index}",
            side="sell",
            entry_price=str(entry),
            invalidation_price=str(entry + distance),
        )
        module_buy = ProtectionTargetModule(
            reward_r=reward,
            min_reward_r="0.01",
            max_reward_r="100",
            min_stop_distance_bps=0,
            max_stop_distance_bps=9999,
        )
        module_sell = ProtectionTargetModule(
            reward_r=reward,
            min_reward_r="0.01",
            max_reward_r="100",
            min_stop_distance_bps=0,
            max_stop_distance_bps=9999,
        )
        buy_value = module_buy.on_bar(make_view(bar, buy)).features[0].value
        sell_value = module_sell.on_bar(make_view(bar, sell)).features[0].value
        assert Decimal(str(buy_value["risk_distance"])) == Decimal(str(sell_value["risk_distance"]))
        assert Decimal(str(buy_value["reward_distance"])) == Decimal(
            str(sell_value["reward_distance"])
        )


def test_checkpoint_digest_and_event_fingerprint_have_fixed_size() -> None:
    state = _signed_state()
    assert len(str(state["state_digest"])) == 64
    assert len(str(state["last_event_fingerprint"])) == 64


def test_snapshot_restore_snapshot_is_byte_stable() -> None:
    state = _signed_state()
    restored = ProtectionTargetModule()
    restored.restore_state(state)
    assert json.dumps(restored.snapshot_state(), sort_keys=True) == json.dumps(
        state, sort_keys=True
    )


def test_config_dataclass_is_frozen() -> None:
    config = ProtectionTargetConfig()
    with pytest.raises(FrozenInstanceError):
        config.reward_r = Decimal("4")  # type: ignore[misc]
