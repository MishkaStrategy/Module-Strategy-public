from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from strategy_factory.errors import CompatibilityError
from strategy_factory.ms5a_budget_probe import (
    EXPECTED_RUNTIME_SURFACE_FINGERPRINT,
    FAMILIES,
    WINDOWS,
    FrozenCase,
    _native_specification,
    _prefix_readiness,
    _select_frozen_plans,
    _timeframe_seconds,
    _validate_first_search_job,
    _validate_runtime_surface_fingerprint,
    _warmup_projection,
    authority_manifest,
    validate_authority,
)


def _plan(family: str, number: int) -> dict[str, Any]:
    return {
        "strategy_family": family,
        "factory_plan": {
            "plan_id": f"sf-plan-{number}",
            "resource_limits": {"native_experiment_id": f"exp-{number}"},
            "research_specification": {"research_id": f"research-{number}"},
            "opaque_parameters": {"private": number},
        },
    }


def _selected_modules(family: str) -> tuple[str, ...]:
    family_module, adapter = {
        "breakout_return": ("breakout_return@1.0.0", "setup_adapter.breakout_return_v1@0.1.0"),
        "fvg_rt": ("fvg_rt@1.0.0", "setup_adapter.fvg_rt_composite_v1@0.1.0"),
        "liquidity_sweep": ("liquidity_sweep@0.1.2", "setup_adapter.liquidity_sweep_v1@0.1.0"),
    }[family]
    return (
        family_module,
        adapter,
        "entry_model@0.2.0",
        "protection_target.protection@1.1.1",
        "protection_target.target@1.1.1",
        "risk_request_composer@0.1.0",
        "risk_sizing@1.2.0",
    )


def test_selects_first_frozen_plan_per_family_without_requiring_submission() -> None:
    payload = {
        "plans": [
            _plan("fvg_rt", 1),
            _plan("fvg_rt", 2),
            _plan("breakout_return", 3),
            _plan("liquidity_sweep", 4),
        ]
    }
    cases = _select_frozen_plans(payload, {"research-sf-plan-1": {}})

    assert tuple(case.family for case in cases) == FAMILIES
    assert cases[1].experiment_id == "exp-1"
    assert cases[1].submitted is True
    assert cases[2].submitted is False
    assert "opaque_parameters" not in cases[0].public()


@pytest.mark.parametrize(
    "plans",
    [
        [_plan("breakout_return", 1), _plan("fvg_rt", 2)],
        [_plan("breakout_return", 1), _plan("fvg_rt", 2), _plan("fvg_rt", 3)],
    ],
)
def test_plan_selector_fails_closed_when_any_family_is_missing(plans: list[dict[str, Any]]) -> None:
    with pytest.raises(CompatibilityError):
        _select_frozen_plans({"plans": plans}, {})


def test_canonical_decimal_restore_preserves_native_list_shape() -> None:
    specification = _native_specification(
        {
            "research_id": "research-1",
            "title": "bounded",
            "description": "synthetic test",
            "strategy_id": "strategy-1",
            "strategy_version": "1",
            "module_versions": {},
            "base_configuration": {},
            "search_method": "grid",
            "search_space": {
                "dimensions": [
                    {"name": "fraction", "kind": "decimal", "values": [{"$decimal": "0.125"}]}
                ]
            },
            "datasets": [
                {
                    "dataset_id": "dataset-1",
                    "source": "registered",
                    "market_type": "spot",
                    "instrument": "TEST",
                    "timeframe": "5m",
                    "period": {"start": "2024-01-01T00:00:00Z", "end": "2024-02-01T00:00:00Z"},
                    "row_count": 100,
                    "timezone": "UTC",
                    "file_format": "parquet",
                    "file_size": 1,
                    "sha256": "a" * 64,
                    "preprocessing_version": "1",
                    "aggregation_version": "1",
                    "metadata_version": "1",
                }
            ],
            "instruments": ["TEST"],
            "market_type": "spot",
            "timeframes": ["5m"],
            "date_range": {"start": "2024-01-01T00:00:00Z", "end": "2024-02-01T00:00:00Z"},
            "split_policy": {
                "mode": "fixed",
                "train": {"start": "2024-01-01T00:00:00Z", "end": "2024-01-20T00:00:00Z"},
                "validation": {"start": "2024-01-20T00:00:00Z", "end": "2024-01-25T00:00:00Z"},
                "test": {"start": "2024-01-25T00:00:00Z", "end": "2024-02-01T00:00:00Z"},
                "train_fraction": None,
                "validation_fraction": None,
                "test_fraction": None,
            },
        }
    )
    assert len(specification.search_space.dimensions) == 1


def test_warmup_projection_distinguishes_startup_and_ready_prefixes() -> None:
    assert WINDOWS == (1_000, 2_000, 4_000)
    assert [
        item["manifest_history_complete"]
        for item in _prefix_readiness({"base": 12}, skip_until_ready=False)
    ] == [
        True,
        True,
        True,
    ]
    assert [
        item["manifest_history_complete"]
        for item in _prefix_readiness({"base": 5}, skip_until_ready=False)
    ] == [
        True,
        True,
        True,
    ]
    fvg = _prefix_readiness({"15m": 200, "1h": 200, "4h": 200, "1d": 200}, skip_until_ready=False)
    assert [item["manifest_history_complete"] for item in fvg] == [False] * 3
    assert _timeframe_seconds("1d") == 86_400


def test_selected_module_warmup_contracts_are_projected_from_frozen_parameters() -> None:
    breakout = FrozenCase("breakout_return", "a" * 64, {}, "exp-1", True)
    liquidity = FrozenCase("liquidity_sweep", "b" * 64, {}, "exp-2", False)
    fvg = FrozenCase("fvg_rt", "c" * 64, {}, "exp-3", True)

    breakout_warmup = _warmup_projection(
        breakout,
        {"modules": _selected_modules("breakout_return"), "breakout_return.lookback_bars": 12},
    )
    assert breakout_warmup["static_branch_history_minimum"] == {"base": 12}
    assert breakout_warmup["manifest_history_bars_by_timeframe"] == {}
    liquidity_warmup = _warmup_projection(
        liquidity,
        {
            "modules": _selected_modules("liquidity_sweep"),
            "liquidity_sweep.pivot_left_bars": 2,
            "liquidity_sweep.pivot_right_bars": 2,
        },
    )
    assert liquidity_warmup["static_branch_history_minimum"] == {"base": 5}
    assert liquidity_warmup["manifest_history_bars_by_timeframe"] == {}
    fvg_requirements = _warmup_projection(
        fvg,
        {"modules": _selected_modules("fvg_rt")},
    )
    assert fvg_requirements["skip_until_ready"] is False
    manifest_requirements = fvg_requirements["manifest_history_bars_by_timeframe"]
    assert manifest_requirements == {"15m": 200, "1h": 200, "4h": 200, "1d": 200}
    coverage = _prefix_readiness(manifest_requirements, skip_until_ready=False)
    assert all(not item["manifest_history_complete"] for item in coverage)
    assert all(item["runtime_readiness"] == "unverified" for item in coverage)
    assert all(item["skip_until_ready"] is False for item in coverage)


def test_unsupported_selected_context_module_fails_closed() -> None:
    case = FrozenCase("breakout_return", "a" * 64, {}, "exp-1", True)
    with pytest.raises(CompatibilityError):
        _warmup_projection(
            case,
            {
                "modules": (
                    *_selected_modules("breakout_return"),
                    "market_regime@1.0.0",
                    "entry_model@0.2.0",
                    "protection_target.protection@1.1.1",
                    "protection_target.target@1.1.1",
                    "risk_request_composer@0.1.0",
                    "risk_sizing@1.2.0",
                ),
                "breakout_return.lookback_bars": 12,
            },
        )


def test_first_frozen_job_rejects_non_initial_search_stage() -> None:
    period = SimpleNamespace(
        start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2024, 1, 2, tzinfo=UTC)
    )
    _validate_first_search_job(SimpleNamespace(stage_role="optimization_000", period=period))
    with pytest.raises(CompatibilityError, match="optimization_000"):
        _validate_first_search_job(SimpleNamespace(stage_role="train", period=period))


def test_runtime_surface_fingerprint_fails_closed_and_spec_hash_is_stable() -> None:
    assert _validate_runtime_surface_fingerprint(EXPECTED_RUNTIME_SURFACE_FINGERPRINT)
    for missing_or_wrong in (None, "f" * 64):
        with pytest.raises(CompatibilityError):
            _validate_runtime_surface_fingerprint(missing_or_wrong)

    specification = {"research_id": "frozen"}
    case = FrozenCase("breakout_return", "a" * 64, specification, "exp-1", False)
    specification["research_id"] = "mutated"
    with pytest.raises(CompatibilityError):
        case.public()


def test_owner_manifest_is_exactly_bound_to_hashes_cases_and_limits() -> None:
    cases = tuple(
        FrozenCase(family, f"{index:064x}", {"research_id": family}, f"exp-{index}", False)
        for index, family in enumerate(FAMILIES, start=1)
    )
    bindings = {
        "source_commit_sha": "a" * 40,
        "request_fingerprint": "b" * 64,
        "dataset_fingerprint": "c" * 64,
        "dataset_sha256": "d" * 64,
        "runtime_surface_fingerprint": "e" * 64,
    }
    manifest = authority_manifest(bindings, cases, "owner-grant-1")
    validate_authority(manifest, bindings, cases)

    manifest["workers"] = 2
    with pytest.raises(CompatibilityError):
        validate_authority(manifest, bindings, cases)
