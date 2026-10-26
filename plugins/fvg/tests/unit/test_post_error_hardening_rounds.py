from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta, timezone
from decimal import Decimal
from importlib.metadata import entry_points
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml
from modular_core import Candle, FeatureDelete, FeatureStore, ModuleRegistry

from conftest import bearish_triplet, bullish_triplet, candle, monotonic_bars, view_for
from modular_strategy_fvg import (
    ACTIVE_ZONE_KEY,
    CREATED_EVENT_KEY,
    ZONE_STATE_KEY,
    FvgConfig,
    FvgDataConflictError,
    FvgDirection,
    FvgModule,
    FvgReasonCode,
    FvgStateError,
    detect_zone,
)


def _digest(module: FvgModule) -> str:
    payload = json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def test_round_01_bullish_strict_boundary_and_decimal_geometry() -> None:
    a, b, c = bullish_triplet()
    equal = Candle(c.timestamp, c.open, c.high, a.high, c.close, c.volume, c.symbol, c.timeframe)
    assert detect_zone(a, b, equal, FvgConfig(timeframes=("15m",))) is None
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    assert zone.direction is FvgDirection.BULLISH
    assert (zone.lower, zone.upper, zone.size) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("5"),
    )


def test_round_02_bearish_symmetry_and_equality_exclusion() -> None:
    a, b, c = bearish_triplet()
    equal = Candle(c.timestamp, c.open, a.low, c.low, c.close, c.volume, c.symbol, c.timeframe)
    assert detect_zone(a, b, equal, FvgConfig(timeframes=("15m",))) is None
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    assert zone.direction is FvgDirection.BEARISH
    assert (zone.lower, zone.upper, zone.midpoint) == (
        Decimal("95"),
        Decimal("100"),
        Decimal("97.5"),
    )


def test_round_03_future_bar_rejection_is_atomic() -> None:
    module = FvgModule(timeframes=("15m",))
    before = module.snapshot_state()
    with pytest.raises(ValueError, match="future/incomplete"):
        module.on_bar(view_for(bullish_triplet(), base_close_offset_minutes=-5))
    assert module.snapshot_state() == before


def test_round_04_timezone_offset_replay_has_same_feature_id() -> None:
    bars = list(bullish_triplet())
    first = FvgModule(timeframes=("15m",)).on_bar(view_for(bars))
    offset = timezone(timedelta(hours=7))
    shifted = [
        Candle(
            bar.timestamp.astimezone(offset),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.symbol,
            bar.timeframe,
        )
        for bar in bars
    ]
    second = FvgModule(timeframes=("15m",)).on_bar(view_for(shifted))
    assert [feature.feature_id for feature in first.features] == [feature.feature_id for feature in second.features]


def test_round_05_manifest_input_contract_matches_resolved_config() -> None:
    module = FvgModule(
        timeframes=("15m", "4h"),
        max_active_zones_per_timeframe=7,
        max_state_bytes=4096,
    )
    assert module.manifest.required_timeframes == ("15m", "4h")
    assert module.manifest.warmup_bars == {"15m": 3, "4h": 3}
    assert module.parameters["max_active_zones_per_timeframe"] == 7
    assert module.parameters["max_state_bytes"] == 4096


def test_round_06_missing_optional_timeframe_is_a_deterministic_skip() -> None:
    module = FvgModule(timeframes=("15m", "4h"))
    first = module.on_bar(view_for(bullish_triplet()))
    snapshot = module.snapshot_state()
    second = module.on_bar(view_for(bullish_triplet()))
    assert "fvg.no_data:4h" in first.skips
    assert "fvg.no_data:4h" in second.skips
    assert second.diagnostics["duplicates"] == 1
    assert module.snapshot_state() == snapshot


def test_round_07_feature_payload_and_metadata_are_immutable() -> None:
    decision = FvgModule(timeframes=("15m",)).on_bar(view_for(bullish_triplet()))
    active = next(feature for feature in decision.features if feature.key == ACTIVE_ZONE_KEY)
    assert isinstance(active.value, MappingProxyType)
    with pytest.raises(TypeError):
        active.value["lower"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        active.metadata["lifecycle"] = "changed"  # type: ignore[index]


def test_round_08_output_ids_and_order_are_deterministic() -> None:
    first = FvgModule(timeframes=("15m",)).on_bar(view_for(bullish_triplet()))
    second = FvgModule(timeframes=("15m",)).on_bar(view_for(bullish_triplet()))
    assert [(item.key, item.feature_id) for item in first.features] == [
        (item.key, item.feature_id) for item in second.features
    ]
    assert [item.key for item in first.features] == [
        ACTIVE_ZONE_KEY,
        ZONE_STATE_KEY,
        CREATED_EVENT_KEY,
    ]


def test_round_09_decimal_configuration_is_canonical() -> None:
    left = FvgModule(
        timeframes=("15m",),
        min_gap_absolute="0.00",
        min_gap_fraction="0.0100",
    )
    right = FvgModule(
        timeframes=("15m",),
        min_gap_absolute=Decimal("0"),
        min_gap_fraction=Decimal("0.01"),
    )
    assert left.parameters == right.parameters
    assert left.snapshot_state()["config_fingerprint"] == right.snapshot_state()["config_fingerprint"]


def test_round_10_unknown_constructor_parameter_fails_closed() -> None:
    with pytest.raises(TypeError, match="unexpected keyword"):
        FvgModule(
            timeframes=("15m",),
            unknown_parameter=True,  # type: ignore[call-arg]
        )


def test_round_11_capacity_eviction_is_oldest_first_and_bounded() -> None:
    module = FvgModule(
        timeframes=("15m",),
        max_active_zones_per_timeframe=2,
    )
    decision = module.on_bar(view_for(monotonic_bars(8)))
    assert len(module.active_zones) == 2
    evictions = [
        item for item in decision.feature_deletes if item.reason == FvgReasonCode.CAPACITY_EVICTED_DELETE.value
    ]
    assert evictions
    assert tuple(zone.zone_id for zone in module.active_zones) == tuple(
        sorted(zone.zone_id for zone in module.active_zones)
    )


def test_round_12_creation_candle_cannot_invalidate_new_zone() -> None:
    module = FvgModule(
        timeframes=("15m",),
        invalidation_level="first_touch",
        invalidation_basis="wick",
    )
    decision = module.on_bar(view_for(bullish_triplet()))
    assert not decision.feature_deletes
    assert len(module.active_zones) == 1


def test_round_13_schema1_migration_is_fail_closed_until_new_history() -> None:
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bullish_triplet()))
    current = source.snapshot_state()
    legacy = {
        "state_schema_version": "1",
        "module_version": "0.1.0",
        "config_fingerprint": source._legacy_config_fingerprint,  # noqa: SLF001
        "last_processed": [
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "close_time": row["close_time"],
            }
            for row in current["last_processed"]
        ],
        "zones": current["zones"],
    }
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(legacy)
    state = restored.snapshot_state()
    assert state["state_schema_version"] == "4"
    assert state["module_version"] == "0.3.0"
    assert state["last_processed"][0]["barrier_close_time"] == current["last_processed"][0]["close_time"]
    assert len(state["zone_lifecycle"]) == 1


def test_round_14_partial_history_state_rejection_is_atomic() -> None:
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bullish_triplet()))
    before = module.snapshot_state()
    bad = copy.deepcopy(before)
    bad["last_processed"][0]["history_fingerprints"][0].pop("bar_fingerprint")
    with pytest.raises(FvgStateError):
        module.restore_state(bad)
    assert module.snapshot_state() == before


def test_round_15_exact_duplicate_after_restore_is_idempotent() -> None:
    bars = bullish_triplet()
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bars))
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(source.snapshot_state())
    republished = restored.on_bar(view_for(bars))
    assert sum(feature.key == ACTIVE_ZONE_KEY for feature in republished.features) == 1
    assert sum(feature.key == ZONE_STATE_KEY for feature in republished.features) == 1
    duplicate = restored.on_bar(view_for(bars))
    assert not duplicate.features
    assert duplicate.diagnostics["duplicates"] == 1


def test_round_16_conflicting_duplicate_after_restore_is_atomic() -> None:
    bars = list(bullish_triplet())
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bars))
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(source.snapshot_state())
    restored.on_bar(view_for(bars))
    before = restored.snapshot_state()
    current = bars[-1]
    bars[-1] = Candle(
        current.timestamp,
        current.open,
        current.high,
        current.low,
        current.close,
        current.volume + 10,
        current.symbol,
        current.timeframe,
    )
    with pytest.raises(FvgDataConflictError):
        restored.on_bar(view_for(bars))
    assert restored.snapshot_state() == before


def test_round_17_registry_loads_real_module_and_manifest_version() -> None:
    module = ModuleRegistry().load_import_path(
        "modular_strategy_fvg:FvgModule",
        {"timeframes": ["15m"]},
    )
    assert isinstance(module, FvgModule)
    assert module.manifest.version == "0.3.0"
    assert module.manifest.state_schema_version == "4"
    assert ZONE_STATE_KEY in module.manifest.produced_features


def test_round_18_entry_point_resolves_to_public_module() -> None:
    selected = {item.name: item for item in entry_points().select(group="modular_strategy.modules")}
    assert selected["fvg"].value == "modular_strategy_fvg:FvgModule"
    assert selected["fvg"].load() is FvgModule


def test_round_19_feature_store_round_trip_preserves_ownership() -> None:
    decision = FvgModule(timeframes=("15m",)).on_bar(view_for(bullish_triplet()))
    active = next(feature for feature in decision.features if feature.key == ACTIVE_ZONE_KEY)
    store = FeatureStore()
    stored = store.publish(active)
    assert stored.source_module == "fvg"
    deleted = store.delete(
        FeatureDelete(
            key=active.key,
            source_module=active.source_module,
            feature_id=active.feature_id,
            symbol=active.symbol,
            timeframe=active.timeframe,
            reason="round_19",
        )
    )
    assert deleted == (stored,)


def test_round_20_instances_are_symbol_and_state_isolated() -> None:
    btc = FvgModule(timeframes=("15m",))
    eth = FvgModule(timeframes=("15m",))
    btc.on_bar(view_for(bullish_triplet(symbol="BTCUSDT"), symbol="BTCUSDT"))
    eth.on_bar(view_for(bullish_triplet(symbol="ETHUSDT"), symbol="ETHUSDT"))
    assert btc.bound_symbol == "BTCUSDT"
    assert eth.bound_symbol == "ETHUSDT"
    assert _digest(btc) != _digest(eth)


def test_round_21_tail_cursor_returns_only_new_indexes() -> None:
    bars = monotonic_bars(20)
    previous = bars[17].close_time
    assert list(FvgModule._new_indexes(bars, previous)) == [18, 19]  # noqa: SLF001
    assert list(FvgModule._new_indexes(bars, bars[-1].close_time)) == []  # noqa: SLF001


def test_round_22_empty_state_is_bounded_and_canonical_json() -> None:
    module = FvgModule(timeframes=("15m",), max_state_bytes=1024)
    state = module.snapshot_state()
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
    assert len(encoded.encode()) <= 1024
    assert json.loads(encoded) == state


def test_round_23_smoke_workload_keeps_cursor_state_bounded() -> None:
    module = FvgModule(
        timeframes=("15m",),
        max_active_zones_per_timeframe=8,
    )
    bars = monotonic_bars(100)
    for index in range(3, len(bars) + 1):
        module.on_bar(view_for(bars[max(0, index - 3) : index]))
    state = module.snapshot_state()
    assert len(state["last_processed"]) == 1
    assert len(state["last_processed"][0]["history_fingerprints"]) <= 2
    assert len(module.active_zones) <= 8


def test_round_24_midpoint_restart_digest_matches_uninterrupted() -> None:
    bars = monotonic_bars(30)
    uninterrupted = FvgModule(
        timeframes=("15m",),
        max_active_zones_per_timeframe=6,
    )
    restarted = FvgModule(
        timeframes=("15m",),
        max_active_zones_per_timeframe=6,
    )
    for index in range(3, 31):
        window = bars[max(0, index - 3) : index]
        uninterrupted.on_bar(view_for(window))
        if index <= 15:
            restarted.on_bar(view_for(window))
    continued = FvgModule(
        timeframes=("15m",),
        max_active_zones_per_timeframe=6,
    )
    continued.restore_state(restarted.snapshot_state())
    for index in range(16, 31):
        continued.on_bar(view_for(bars[max(0, index - 3) : index]))
    assert _digest(uninterrupted) == _digest(continued)


def test_round_25_diagnostics_are_structured_and_stable() -> None:
    decision = FvgModule(timeframes=("15m",)).on_bar(view_for(bullish_triplet()))
    expected = {
        "base_index",
        "symbol",
        "data_gap",
        "processed_bars",
        "created",
        "invalidated",
        "expired",
        "duplicates",
        "evaluations_accepted",
        "evaluations_rejected",
        "pre_candidates",
        "active_total",
        "active_by_timeframe",
        "state_estimated_bytes",
    }
    assert expected <= set(decision.diagnostics)
    assert all(
        isinstance(decision.diagnostics[key], int)
        for key in (
            "processed_bars",
            "created",
            "invalidated",
            "duplicates",
            "active_total",
            "state_estimated_bytes",
        )
    )


def test_round_26_reason_codes_match_emitted_delete_reasons() -> None:
    bars = list(bullish_triplet())
    module = FvgModule(timeframes=("15m",), invalidation_level="full_fill")
    module.on_bar(view_for(bars))
    trigger = candle(3, open_=99, high=101, low=90, close=99)
    decision = module.on_bar(view_for([*bars, trigger]))
    assert decision.feature_deletes[0].reason == FvgReasonCode.MITIGATED_DELETE.value
    assert {item.value for item in FvgReasonCode} >= {
        "fvg.empty_base_bars",
        "fvg.data_gap_reset",
        "fvg_mitigated",
        "fvg_expired",
        "fvg_pre_candidate_resolved",
    }


def test_round_27_packaging_declares_types_and_strict_quality_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'modular_strategy_fvg = ["py.typed"]' in pyproject
    assert "strict = true" in pyproject
    assert "fail_under = 95" in pyproject
    assert (root / "src/modular_strategy_fvg/py.typed").is_file()


def test_round_28_self_hosted_workflow_has_resource_and_safety_controls() -> None:
    repository = Path(__file__).resolve().parents[4]
    workflow = repository / ".github/workflows/fvg-release-validation.yml"
    payload = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    jobs = payload["jobs"]
    formatter = jobs["materialize-format"]
    release = jobs["exact-head"]
    assert formatter["runs-on"] == ["self-hosted", "fast"]
    assert release["runs-on"] == ["self-hosted", "fast"]
    assert formatter["timeout-minutes"] == 30
    assert release["timeout-minutes"] == 120
    assert payload["concurrency"]["cancel-in-progress"] is True
    assert "pull_request.number" in payload["concurrency"]["group"]
    assert release["strategy"]["fail-fast"] is False
    assert release["strategy"]["matrix"]["python-version"] == ["3.12", "3.13"]
    formatter_names = {step["name"] for step in formatter["steps"]}
    release_names = {step["name"] for step in release["steps"]}
    assert {
        "Determine formatting eligibility",
        "Materialize deterministic Ruff output",
    } <= formatter_names
    assert {
        "Checkout exact FVG head",
        "Checkout pinned Core source",
        "Verify pinned Core",
        "Run complete FVG release gate",
        "Upload exact-head evidence",
        "Publish release gate summary",
        "Enforce release gate",
    } <= release_names


def test_round_29_full_lifecycle_replay_is_deterministic() -> None:
    bars = list(bullish_triplet())
    trigger = candle(3, open_=99, high=101, low=90, close=99)
    stream = [bars[:1], bars[:2], bars[:3], [*bars, trigger]]
    digests = []
    for _ in range(2):
        module = FvgModule(timeframes=("15m",), invalidation_level="full_fill")
        emitted = []
        for visible in stream:
            decision = module.on_bar(view_for(visible))
            emitted.extend((feature.key, feature.feature_id) for feature in decision.features)
            emitted.extend((delete.key, delete.feature_id, delete.reason) for delete in decision.feature_deletes)
        digests.append(hashlib.sha256(repr((emitted, module.snapshot_state())).encode()).hexdigest())
    assert digests[0] == digests[1]


def test_round_30_release_documents_match_runtime_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    contract = root / "docs/COMPLETENESS_REVIEW.md"
    assert "v0.3.0" in readme
    assert "State schema v4" in readme
    assert "fvg.zone_state" in readme
    assert "0.3.0" in changelog
    assert contract.is_file()
