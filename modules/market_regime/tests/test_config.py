from __future__ import annotations

from decimal import Decimal

import pytest

from market_regime import MarketRegimeConfig, MarketRegimeConfigurationError
from market_regime.config import canonical_decimal, canonical_timeframe, timeframe_seconds


@pytest.mark.parametrize(
    ("raw", "normalized", "seconds"),
    [
        ("1H", "1h", 3600),
        (" 15M ", "15m", 900),
        ("4h", "4h", 14400),
        ("1d", "1d", 86400),
        ("1w", "1w", 604800),
    ],
)
def test_timeframe_normalization(raw: str, normalized: str, seconds: int) -> None:
    assert canonical_timeframe(raw) == normalized
    assert timeframe_seconds(raw) == seconds


@pytest.mark.parametrize("value", ["", "0h", "h", "1hour", "-1h", "1.5h"])
def test_invalid_timeframe(value: str) -> None:
    with pytest.raises(MarketRegimeConfigurationError):
        MarketRegimeConfig(timeframe=value)


def test_timeframe_type_is_strict() -> None:
    with pytest.raises(MarketRegimeConfigurationError, match="string"):
        MarketRegimeConfig(timeframe=1)  # type: ignore[arg-type]


def test_timeframe_overflow_rejected() -> None:
    with pytest.raises(MarketRegimeConfigurationError, match="maximum duration"):
        MarketRegimeConfig(timeframe="999999999999999999999999999w")


def test_oversized_timeframe_integer_uses_typed_configuration_error() -> None:
    with pytest.raises(
        MarketRegimeConfigurationError,
        match="supported integer size",
    ):
        MarketRegimeConfig(timeframe=f"{'9' * 5000}w")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fast_ema_period": 1},
        {"slow_ema_period": 2},
        {"atr_period": 1},
        {"volatility_baseline_period": 4},
        {"efficiency_period": 1},
        {"slope_lookback": 0},
        {"regime_confirmation_bars": 0},
        {"volatility_confirmation_bars": 101},
        {"publish_diagnostics": 1},
    ],
)
def test_invalid_scalar_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(MarketRegimeConfigurationError):
        MarketRegimeConfig(**kwargs)  # type: ignore[arg-type]


def test_fast_must_be_less_than_slow() -> None:
    with pytest.raises(MarketRegimeConfigurationError, match="fast_ema_period"):
        MarketRegimeConfig(fast_ema_period=20, slow_ema_period=20)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trend_spread_threshold": "NaN"},
        {"trend_slope_threshold": "Infinity"},
        {"low_volatility_ratio": "0"},
        {"low_volatility_ratio": "1"},
        {"high_volatility_ratio": "1"},
        {"high_volatility_ratio": "1000.1"},
        {"range_efficiency_threshold": "0.5", "trend_efficiency_threshold": "0.4"},
    ],
)
def test_invalid_decimal_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(MarketRegimeConfigurationError):
        MarketRegimeConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.0010"), "0.001"),
        (Decimal("-0"), "0"),
        (Decimal("10.000"), "10"),
        (Decimal("0.0000001"), "0.0000001"),
    ],
)
def test_canonical_decimal(value: Decimal, expected: str) -> None:
    assert canonical_decimal(value) == expected


def test_equivalent_decimal_configs_have_same_signature() -> None:
    first = MarketRegimeConfig(trend_spread_threshold="0.001")
    second = MarketRegimeConfig(trend_spread_threshold="0.0010")
    assert first.signature == second.signature


def test_diagnostics_does_not_change_semantic_signature() -> None:
    verbose = MarketRegimeConfig(publish_diagnostics=True)
    quiet = MarketRegimeConfig(publish_diagnostics=False)
    assert verbose.signature == quiet.signature
    assert verbose.legacy_signature != quiet.legacy_signature
    assert verbose.as_parameters()["publish_diagnostics"] is True


def test_compatible_signatures_cover_both_legacy_diagnostics_variants() -> None:
    verbose = MarketRegimeConfig(publish_diagnostics=True)
    quiet = MarketRegimeConfig(publish_diagnostics=False)
    assert verbose.compatible_signatures == quiet.compatible_signatures
    assert verbose.signature in verbose.compatible_signatures
    assert verbose.legacy_signature in verbose.compatible_signatures
    assert quiet.legacy_signature in quiet.compatible_signatures


def test_parameters_are_read_only() -> None:
    config = MarketRegimeConfig()
    with pytest.raises(TypeError):
        config.as_parameters()["timeframe"] = "4h"  # type: ignore[index]


def test_exact_warmup_formula() -> None:
    config = MarketRegimeConfig(
        slow_ema_period=100,
        fast_ema_period=2,
        atr_period=3,
        volatility_baseline_period=5,
        efficiency_period=3,
        slope_lookback=2,
        regime_confirmation_bars=3,
        volatility_confirmation_bars=2,
    )
    assert config.indicator_ready_observations == 100
    assert config.minimum_observations == 102
