from __future__ import annotations

from decimal import Decimal

import pytest

from modular_strategy_position_scaling.config import PositionScalingConfig
from modular_strategy_position_scaling.enums import ScalingMode


def test_safe_defaults_disable_scaling() -> None:
    config = PositionScalingConfig()
    assert config.enabled is False
    assert config.allow_averaging_down is False
    assert config.allow_martingale is False
    assert config.rules == ()


def test_unknown_config_field_rejected() -> None:
    with pytest.raises(ValueError, match="unknown position scaling parameters"):
        PositionScalingConfig.from_mapping({"unknown": 1})


def test_strict_boolean_rejected() -> None:
    with pytest.raises(TypeError):
        PositionScalingConfig.from_mapping({"enabled": 1})


def test_rule_for_disabled_mode_rejected() -> None:
    with pytest.raises(ValueError, match="disabled"):
        PositionScalingConfig.from_mapping(
            {
                "enabled": True,
                "rules": [
                    {
                        "stage": 1,
                        "mode": "averaging_down",
                        "action": "add",
                        "trigger_kind": "always",
                        "trigger_value": 0,
                        "quantity_kind": "fixed",
                        "quantity_value": 1,
                    }
                ],
            }
        )


def test_duplicate_stages_rejected() -> None:
    rule = {
        "stage": 1,
        "mode": "scale_out",
        "action": "reduce",
        "trigger_kind": "always",
        "trigger_value": 0,
        "quantity_kind": "current_fraction",
        "quantity_value": "0.1",
    }
    with pytest.raises(ValueError, match="stages"):
        PositionScalingConfig.from_mapping({"enabled": True, "rules": [rule, rule]})


def test_increasing_weighted_dca_rejected_as_martingale() -> None:
    rules = []
    for stage, weight in ((1, "0.25"), (2, "0.5")):
        rules.append(
            {
                "stage": stage,
                "mode": "weighted_dca",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": str(stage / 100),
                "quantity_kind": "weight",
                "quantity_value": weight,
            }
        )
    with pytest.raises(ValueError, match="martingale-like"):
        PositionScalingConfig.from_mapping(
            {
                "enabled": True,
                "allow_weighted_dca": True,
                "rules": rules,
            }
        )


def test_martingale_requires_explicit_opt_in() -> None:
    rules = [
        {
            "stage": 1,
            "mode": "weighted_dca",
            "action": "add",
            "trigger_kind": "adverse_percent",
            "trigger_value": "0.01",
            "quantity_kind": "weight",
            "quantity_value": "0.25",
        },
        {
            "stage": 2,
            "mode": "weighted_dca",
            "action": "add",
            "trigger_kind": "adverse_percent",
            "trigger_value": "0.02",
            "quantity_kind": "weight",
            "quantity_value": "0.5",
        },
    ]
    config = PositionScalingConfig.from_mapping(
        {
            "enabled": True,
            "allow_weighted_dca": True,
            "allow_martingale": True,
            "rules": rules,
        }
    )
    assert config.allow_martingale


def test_config_fingerprint_canonicalizes_decimal_values() -> None:
    first = PositionScalingConfig(max_total_leverage=Decimal("3.0"))
    second = PositionScalingConfig(max_total_leverage=Decimal("3.00"))
    assert first.fingerprint == second.fingerprint


def test_rules_sorted_by_stage() -> None:
    config = PositionScalingConfig.from_mapping(
        {
            "enabled": True,
            "allow_scale_out": True,
            "rules": [
                {
                    "stage": 2,
                    "mode": "scale_out",
                    "action": "reduce",
                    "trigger_kind": "r_multiple",
                    "trigger_value": "2",
                    "quantity_kind": "current_fraction",
                    "quantity_value": "0.5",
                },
                {
                    "stage": 1,
                    "mode": "scale_out",
                    "action": "reduce",
                    "trigger_kind": "r_multiple",
                    "trigger_value": "1",
                    "quantity_kind": "current_fraction",
                    "quantity_value": "0.25",
                },
            ],
        }
    )
    assert [rule.stage for rule in config.rules] == [1, 2]
    assert config.rules[0].mode is ScalingMode.SCALE_OUT
