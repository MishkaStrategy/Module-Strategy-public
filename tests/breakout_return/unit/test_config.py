import math

import pytest

from strategy_modules.breakout_return import BreakoutReturnConfig, BreakoutReturnModule


def test_defaults_are_safe_and_manifest_is_standalone():
    module = BreakoutReturnModule()
    manifest = module.manifest
    assert manifest.required_features == ()
    assert set(manifest.produced_features) == {
        "breakout_return.active_setup",
        "breakout_return.confirmed",
    }
    assert module.parameters["lookback_bars"] == 20


def test_feature_mode_declares_exact_dependency():
    module = BreakoutReturnModule(reference_mode="feature", reference_feature_key="session.range")
    assert module.manifest.required_features == ("session.range",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback_bars": 1},
        {"max_return_bars": 0},
        {"reference_feature_key": "unnamespaced"},
        {"breakout_buffer_bps": -1},
        {"breakout_buffer_bps": 5001},
        {"min_return_depth": -0.1},
        {"min_return_depth": 0.3, "max_return_depth": 0.2},
        {"max_return_depth": 1.1},
        {"max_return_depth": math.nan},
        {"reference_mode": "unknown"},
        {"direction": "long"},
        {"breakout_confirmation": "wick"},
        {"return_confirmation": "wick"},
        {"range_change_policy": "merge"},
    ],
)
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError, match="invalid breakout_return configuration"):
        BreakoutReturnModule(**kwargs)


def test_zero_thresholds_are_valid():
    config = BreakoutReturnConfig(
        breakout_buffer_bps=0,
        min_return_depth=0,
        max_return_depth=0,
    )
    assert config.min_return_depth == 0
