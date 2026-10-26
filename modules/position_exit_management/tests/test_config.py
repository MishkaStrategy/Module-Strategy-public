from __future__ import annotations

from decimal import Decimal

import pytest
from modular_core import ModuleConfigurationError, ModuleRegistry

from modular_strategy_position_exit_management import (
    MANIFEST,
    PartialExitRule,
    PositionExitConfig,
    PositionExitManagementModule,
    TrailingMode,
    TriggerPriceSource,
)


def test_defaults_are_valid_and_canonical():
    config = PositionExitConfig()
    assert config.trailing_mode is TrailingMode.NONE
    assert config.trigger_source is TriggerPriceSource.CLOSE
    assert len(config.fingerprint) == 64
    assert config.fingerprint == PositionExitConfig().fingerprint


def test_partial_rules_are_sorted_and_preserve_decimal_precision():
    config = PositionExitConfig.from_parameters(
        partial_exits=[
            {"trigger_r": "2.00", "fraction": "0.25"},
            {"trigger_r": "1.00", "fraction": "0.125"},
        ]
    )
    assert config.partial_exits == (
        PartialExitRule(Decimal("1.00"), Decimal("0.125")),
        PartialExitRule(Decimal("2.00"), Decimal("0.25")),
    )
    assert config.canonical_mapping()["partial_exits"] == (
        {"trigger_r": "1", "fraction": "0.125"},
        {"trigger_r": "2", "fraction": "0.25"},
    )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_holding_minutes": 0}, ValueError),
        ({"max_holding_minutes": True}, TypeError),
        ({"breakeven_trigger_r": -1}, ValueError),
        ({"breakeven_trigger_r": float("nan")}, ValueError),
        ({"trailing_distance_r": 0}, ValueError),
        ({"trailing_distance_pct": 0}, ValueError),
        ({"trailing_distance_pct": 1}, ValueError),
        ({"market_buffer_bps": 10_000}, ValueError),
        ({"cancel_retry_bars": 0}, ValueError),
        ({"max_tracked_cancels": 0}, ValueError),
        ({"max_cancel_requests_per_bar": 0}, ValueError),
        (
            {"max_tracked_cancels": 2, "max_cancel_requests_per_bar": 3},
            ValueError,
        ),
        ({"managed_order_tag_prefix": " "}, ValueError),
        ({"managed_order_tag_prefix": " prefix"}, ValueError),
        ({"emit_status_feature": 1}, TypeError),
        ({"partial_exits": "invalid"}, TypeError),
    ],
)
def test_invalid_top_level_config_is_rejected(kwargs, error):
    with pytest.raises(error):
        PositionExitManagementModule(**kwargs)


@pytest.mark.parametrize(
    "partial_exits",
    [
        [{"trigger_r": 1}],
        [{"fraction": 0.5}],
        [{"trigger_r": 1, "fraction": 0}],
        [{"trigger_r": -1, "fraction": 0.5}],
        [{"trigger_r": 1, "fraction": 1.1}],
        [{"trigger_r": 1, "fraction": 0.5, "unknown": 1}],
        [{"trigger_r": 1, "fraction": 0.6}, {"trigger_r": 2, "fraction": 0.5}],
        [{"trigger_r": 1, "fraction": 0.5}, {"trigger_r": 1, "fraction": 0.5}],
    ],
)
def test_invalid_partial_rules_are_rejected(partial_exits):
    with pytest.raises((TypeError, ValueError)):
        PositionExitManagementModule(partial_exits=partial_exits)


def test_manifest_matches_public_module():
    module = PositionExitManagementModule(
        max_holding_minutes=60,
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    assert module.name == MANIFEST.name
    assert MANIFEST.version == "0.2.1"
    assert MANIFEST.produced_features == ("position_exit_management.status",)
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    assert plan.modules == (module,)
    assert plan.required_timeframes == ()


def test_parameters_are_immutable_and_manifest_compatible():
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    with pytest.raises(TypeError):
        module.parameters["trailing_mode"] = "percent"
    with pytest.raises(TypeError):
        module.parameters["partial_exits"][0]["fraction"] = 1.0


def test_unknown_loader_parameter_is_rejected_before_run():
    registry = ModuleRegistry()
    with pytest.raises(ModuleConfigurationError):
        registry.load_import_path(
            "modular_strategy_position_exit_management:PositionExitManagementModule",
            {"unknown": 1},
        )


def test_config_fingerprint_changes_for_semantic_change_only():
    base = PositionExitConfig.from_parameters(breakeven_trigger_r="1.0")
    equivalent = PositionExitConfig.from_parameters(breakeven_trigger_r=1)
    changed = PositionExitConfig.from_parameters(breakeven_trigger_r="1.0001")
    assert base.fingerprint == equivalent.fingerprint
    assert base.fingerprint != changed.fingerprint


def test_high_precision_decimal_configuration_has_distinct_core_identity(candle_factory):
    from datetime import UTC, datetime

    from modular_core.run_manifest import build_run_manifest

    first = PositionExitManagementModule(breakeven_trigger_r="1.0000000000000000001")
    second = PositionExitManagementModule(breakeven_trigger_r="1.0000000000000000002")
    equivalent = PositionExitManagementModule(breakeven_trigger_r=Decimal("1.00000000000000000010"))

    assert first.parameters != second.parameters
    assert first.parameters == equivalent.parameters

    def module_hash(module):
        manifest = build_run_manifest(
            kernel_version="0.4.0",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            bars=(candle_factory(0),),
            config={},
            manifests=(module.manifest,),
            module_parameters=(module.parameters,),
        )
        return manifest.module_hash

    assert module_hash(first) != module_hash(second)
    assert module_hash(first) == module_hash(equivalent)
    ModuleRegistry().validate_and_plan((first,), base_timeframe="5m")


def test_registry_loader_accepts_numeric_and_exact_decimal_parameters():
    registry = ModuleRegistry()
    numeric = registry.load_import_path(
        "modular_strategy_position_exit_management:PositionExitManagementModule",
        {"breakeven_trigger_r": 1.25},
    )
    exact = registry.load_import_path(
        "modular_strategy_position_exit_management:PositionExitManagementModule",
        {"breakeven_trigger_r": "1.2500000000000000001"},
    )
    assert numeric.config.breakeven_trigger_r == Decimal("1.25")
    assert exact.config.breakeven_trigger_r == Decimal("1.2500000000000000001")
