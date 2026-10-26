from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta

from modular_modules.entry_model import EntryKind, EntryModelModule
from strategy_modules.fvg_rt import (
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
    Frozen1hShortModule,
    Frozen4hShortModule,
)

from modular_setup_adapter import CanonicalSetupAdapterModule

from .conftest import bar, view


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _adapter(
    *,
    feature_key: str,
    profile: str,
    source_module: str,
) -> CanonicalSetupAdapterModule:
    return CanonicalSetupAdapterModule(
        sources=(
            {
                "feature_key": feature_key,
                "profile": profile,
                "source_module": source_module,
            },
        )
    )


def _feature(module, current):
    raw = module._snapshot(
        current.symbol,
        current.close_time,
        entry=101.0,
        stop=103.0,
    )
    assert raw is not None
    return module._feature(raw, current.close_time, current.symbol)


def _raw_snapshot(module, current) -> dict[str, object]:
    raw = module._snapshot(
        current.symbol,
        current.close_time,
        entry=101.0,
        stop=103.0,
    )
    assert raw is not None
    return dict(raw)


def _rekeyed_feature(module, current, value: dict[str, object]):
    identity = {
        "available_at": value["signal_available_at"],
        "entry_price": value["entry_price"],
        "module_id": value["module_id"],
        "module_version": value["module_version"],
        "setup_id": value["setup_id"],
        "stop_price": value["stop_price"],
        "symbol": value["symbol"],
    }
    value["snapshot_id"] = _digest(identity)
    value["semantic_input_digest"] = _digest(
        {
            "available_at": value["signal_available_at"],
            "entry": round(float(str(value["entry_price"])), 12),
            "module_id": value["module_id"],
            "module_version": value["module_version"],
            "setup_id": value["setup_id"],
            "stop": round(float(str(value["stop_price"])), 12),
        }
    )
    return module._feature(value, current.close_time, current.symbol)


def _rejected_1h(source, current):
    return _adapter(
        feature_key=FROZEN_1H_FEATURE_KEY,
        profile="fvg_rt_frozen_1h_v1",
        source_module="frozen_1h_short",
    ).on_bar(view(current, (source,)))


def test_frozen_1h_snapshot_maps_to_exact_explicit_limit_cutoff() -> None:
    current = bar()
    module = Frozen1hShortModule()
    source = _feature(module, current)
    adapted = _adapter(
        feature_key=FROZEN_1H_FEATURE_KEY,
        profile="fvg_rt_frozen_1h_v1",
        source_module="frozen_1h_short",
    ).on_bar(view(current, (source,)))

    assert len(adapted.features) == 1
    setup = adapted.features[0]
    assert setup.key == "setup.confirmed"
    assert setup.value["entry_kind"] == "limit_explicit"
    assert setup.value["entry_price"] == "101"
    assert (
        setup.value["entry_cutoff_at"]
        == (current.close_time + timedelta(minutes=5 * 48)).isoformat()
    )
    assert setup.value["metadata"]["priority"] == -20
    assert setup.value["metadata"]["historical_priority"] == 2
    assert setup.value["metadata"]["historical_setup_priority"] == 0
    assert setup.value["metadata"]["strategy_id"] == "fvg_rt"
    assert setup.value["metadata"]["module_id"] == "frozen_1h_short"
    assert setup.value["metadata"]["invalidation_price"] == "103"
    assert setup.value["metadata"]["target_price"] == "89"
    assert setup.value["metadata"]["target_r"] == "6"
    assert setup.value["metadata"]["entry_ttl_bars"] == 48
    assert setup.value["metadata"]["max_holding_seconds"] == 43_200
    assert setup.value["metadata"]["no_progress_rule"] == {
        "after_seconds": 21_600,
        "minimum_mfe_r": "0.5",
    }

    entry = EntryModelModule(
        default_entry_kind=EntryKind.LIMIT_EXPLICIT,
        ttl_bars=576,
        allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
    ).on_bar(view(current, (setup,)))
    assert len(entry.features) == 1
    assert entry.features[0].value["expires_at"] == setup.value["entry_cutoff_at"]


def test_frozen_4h_snapshot_maps_to_exact_explicit_limit_cutoff() -> None:
    current = bar()
    module = Frozen4hShortModule()
    source = _feature(module, current)
    adapted = _adapter(
        feature_key=FROZEN_4H_FEATURE_KEY,
        profile="fvg_rt_frozen_4h_v1",
        source_module="frozen_4h_short",
    ).on_bar(view(current, (source,)))

    assert len(adapted.features) == 1
    setup = adapted.features[0]
    assert setup.key == "setup.confirmed"
    assert setup.value["entry_kind"] == "limit_explicit"
    assert setup.value["entry_price"] == "101"
    assert (
        setup.value["entry_cutoff_at"]
        == (current.close_time + timedelta(minutes=5 * 576)).isoformat()
    )
    assert setup.value["metadata"]["priority"] == -30
    assert setup.value["metadata"]["historical_priority"] == 3
    assert setup.value["metadata"]["historical_setup_priority"] == 0
    assert setup.value["metadata"]["strategy_id"] == "fvg_rt"
    assert setup.value["metadata"]["module_id"] == "frozen_4h_short"
    assert setup.value["metadata"]["invalidation_price"] == "103"
    assert setup.value["metadata"]["target_price"] == "89"
    assert setup.value["metadata"]["target_r"] == "6"
    assert setup.value["metadata"]["entry_ttl_bars"] == 576
    assert setup.value["metadata"]["max_holding_seconds"] == 172_800
    assert setup.value["metadata"]["no_progress_rule"] == {
        "after_seconds": 129_600,
        "minimum_mfe_r": "0.34999999999999998",
    }

    entry = EntryModelModule(
        default_entry_kind=EntryKind.LIMIT_EXPLICIT,
        ttl_bars=576,
        allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
    ).on_bar(view(current, (setup,)))
    assert len(entry.features) == 1
    assert entry.features[0].value["expires_at"] == setup.value["entry_cutoff_at"]


def test_frozen_profile_rejects_drifted_execution_contract() -> None:
    current = bar()
    module = Frozen4hShortModule()
    raw = module._snapshot(
        current.symbol,
        current.close_time,
        entry=101.0,
        stop=103.0,
    )
    assert raw is not None
    value = dict(raw)
    value["ttl_bars_5m"] = 48
    source = module._feature(value, current.close_time, current.symbol)

    rejected = _adapter(
        feature_key=FROZEN_4H_FEATURE_KEY,
        profile="fvg_rt_frozen_4h_v1",
        source_module="frozen_4h_short",
    ).on_bar(view(current, (source,)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.invalid_source_payload" in rejected.skips


def test_frozen_profile_rejects_drifted_completed_timeframes() -> None:
    current = bar()
    module = Frozen1hShortModule()
    raw = module._snapshot(
        current.symbol,
        current.close_time,
        entry=101.0,
        stop=103.0,
    )
    assert raw is not None
    value = dict(raw)
    value["completed_timeframes"] = ("5m", "1h")
    source = module._feature(value, current.close_time, current.symbol)

    rejected = _adapter(
        feature_key=FROZEN_1H_FEATURE_KEY,
        profile="fvg_rt_frozen_1h_v1",
        source_module="frozen_1h_short",
    ).on_bar(view(current, (source,)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.timeframe_mismatch" in rejected.skips


def test_frozen_profile_rejects_stop_distance_outside_historical_contract() -> None:
    current = bar()
    module = Frozen1hShortModule()
    raw = module._snapshot(
        current.symbol,
        current.close_time,
        entry=101.0,
        stop=103.0,
    )
    assert raw is not None
    value = dict(raw)
    value["stop_price"] = "110"
    identity = {
        "available_at": value["signal_available_at"],
        "entry_price": value["entry_price"],
        "module_id": value["module_id"],
        "module_version": value["module_version"],
        "setup_id": value["setup_id"],
        "stop_price": value["stop_price"],
        "symbol": value["symbol"],
    }
    value["snapshot_id"] = _digest(identity)
    value["semantic_input_digest"] = _digest(
        {
            "available_at": value["signal_available_at"],
            "entry": 101.0,
            "module_id": "frozen_1h_short",
            "module_version": "1.0.0",
            "setup_id": "short",
            "stop": 110.0,
        }
    )
    source = module._feature(value, current.close_time, current.symbol)

    rejected = _adapter(
        feature_key=FROZEN_1H_FEATURE_KEY,
        profile="fvg_rt_frozen_1h_v1",
        source_module="frozen_1h_short",
    ).on_bar(view(current, (source,)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.invalid_price_geometry" in rejected.skips


def test_frozen_profile_fail_closed_guard_matrix() -> None:
    current = bar()
    module = Frozen1hShortModule()
    valid = _feature(module, current)

    direct_variants = (
        replace(valid, schema_version="2"),
        replace(valid, timeframe="4h"),
        replace(valid, value="not-a-mapping"),
    )
    for source in direct_variants:
        rejected = _rejected_1h(source, current)
        assert rejected.features[0].key == "setup_adapter.rejection"

    unknown = _raw_snapshot(module, current)
    unknown["unexpected"] = True
    rejected = _rejected_1h(module._feature(unknown, current.close_time, current.symbol), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    identity = _raw_snapshot(module, current)
    identity["snapshot_id"] = "0" * 64
    rejected = _rejected_1h(module._feature(identity, current.close_time, current.symbol), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    module_id = _raw_snapshot(module, current)
    module_id["module_id"] = "wrong_module"
    rejected = _rejected_1h(_rekeyed_feature(module, current, module_id), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    module_version = _raw_snapshot(module, current)
    module_version["module_version"] = "9.9.9"
    rejected = _rejected_1h(_rekeyed_feature(module, current, module_version), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    feature_contract = _raw_snapshot(module, current)
    feature_contract["feature_contract"] = "wrong-contract"
    rejected = _rejected_1h(
        module._feature(feature_contract, current.close_time, current.symbol),
        current,
    )
    assert rejected.features[0].key == "setup_adapter.rejection"

    symbol = _raw_snapshot(module, current)
    symbol["symbol"] = "ETHUSDT"
    rejected = _rejected_1h(_rekeyed_feature(module, current, symbol), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    causality = _raw_snapshot(module, current)
    future = (current.close_time + timedelta(minutes=5)).isoformat()
    causality["signal_available_at"] = future
    causality["first_eligible_5m_open"] = future
    rejected = _rejected_1h(_rekeyed_feature(module, current, causality), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    setup_id = _raw_snapshot(module, current)
    setup_id["setup_id"] = "wrong"
    rejected = _rejected_1h(_rekeyed_feature(module, current, setup_id), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    side = _raw_snapshot(module, current)
    side["side"] = "buy"
    rejected = _rejected_1h(module._feature(side, current.close_time, current.symbol), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    failed_condition = _raw_snapshot(module, current)
    conditions = dict(failed_condition["conditions"])
    conditions[next(iter(conditions))] = False
    failed_condition["conditions"] = conditions
    rejected = _rejected_1h(
        module._feature(failed_condition, current.close_time, current.symbol),
        current,
    )
    assert rejected.features[0].key == "setup_adapter.rejection"

    geometry = _raw_snapshot(module, current)
    geometry["entry_price"] = "99"
    rejected = _rejected_1h(_rekeyed_feature(module, current, geometry), current)
    assert rejected.features[0].key == "setup_adapter.rejection"

    semantic_digest = _raw_snapshot(module, current)
    semantic_digest["semantic_input_digest"] = "0" * 64
    rejected = _rejected_1h(
        module._feature(semantic_digest, current.close_time, current.symbol),
        current,
    )
    assert rejected.features[0].key == "setup_adapter.rejection"

    execution_type = _raw_snapshot(module, current)
    execution_type["ttl_bars_5m"] = True
    rejected = _rejected_1h(
        module._feature(execution_type, current.close_time, current.symbol),
        current,
    )
    assert rejected.features[0].key == "setup_adapter.rejection"


def test_frozen_profiles_accept_composite_emitter_and_preserve_child_identity() -> None:
    current = bar()
    cases = (
        (
            Frozen1hShortModule(),
            FROZEN_1H_FEATURE_KEY,
            "fvg_rt_frozen_1h_v1",
            "frozen_1h_short",
        ),
        (
            Frozen4hShortModule(),
            FROZEN_4H_FEATURE_KEY,
            "fvg_rt_frozen_4h_v1",
            "frozen_4h_short",
        ),
    )

    for module, feature_key, profile, child_module in cases:
        source = replace(_feature(module, current), source_module="fvg_rt_composite")
        adapted = _adapter(
            feature_key=feature_key,
            profile=profile,
            source_module="fvg_rt_composite",
        ).on_bar(view(current, (source,)))

        assert len(adapted.features) == 1
        metadata = adapted.features[0].value["metadata"]
        assert metadata["module_id"] == child_module
        assert metadata["source_module"] == "fvg_rt_composite"
        assert metadata["source"]["module_id"] == child_module
