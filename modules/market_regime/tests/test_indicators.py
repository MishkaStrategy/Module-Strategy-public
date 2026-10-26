from __future__ import annotations

from decimal import Decimal, getcontext

import pytest

from market_regime import MarketRegimeConfig, Regime, TrendDirection, VolatilityLevel
from market_regime.indicators import (
    build_constants,
    calculate_confidence,
    classify_regime,
    classify_volatility,
)


@pytest.fixture
def config() -> MarketRegimeConfig:
    return MarketRegimeConfig()


@pytest.mark.parametrize(
    ("spread", "slope", "efficiency", "direction", "regime"),
    [
        ("0.001", "0.00015", "0.35", TrendDirection.BULLISH, Regime.TREND_UP),
        ("-0.001", "-0.00015", "0.35", TrendDirection.BEARISH, Regime.TREND_DOWN),
        ("0", "0", "0.22", TrendDirection.NEUTRAL, Regime.RANGE),
        ("0.001", "0", "0.9", TrendDirection.NEUTRAL, Regime.TRANSITION),
        ("0", "0.00015", "0.1", TrendDirection.NEUTRAL, Regime.TRANSITION),
    ],
)
def test_regime_boundaries(
    config: MarketRegimeConfig,
    spread: str,
    slope: str,
    efficiency: str,
    direction: TrendDirection,
    regime: Regime,
) -> None:
    assert classify_regime(Decimal(spread), Decimal(slope), Decimal(efficiency), config) == (
        direction,
        regime,
    )


def test_directional_symmetry(config: MarketRegimeConfig) -> None:
    up = classify_regime(Decimal("0.01"), Decimal("0.001"), Decimal("0.9"), config)
    down = classify_regime(Decimal("-0.01"), Decimal("-0.001"), Decimal("0.9"), config)
    assert up == (TrendDirection.BULLISH, Regime.TREND_UP)
    assert down == (TrendDirection.BEARISH, Regime.TREND_DOWN)
    assert calculate_confidence(
        Regime.TREND_UP, Decimal("0.01"), Decimal("0.001"), Decimal("0.9"), config
    ) == calculate_confidence(
        Regime.TREND_DOWN, Decimal("-0.01"), Decimal("-0.001"), Decimal("0.9"), config
    )


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        ("0.75", VolatilityLevel.LOW),
        ("0.7500001", VolatilityLevel.NORMAL),
        ("1.349999", VolatilityLevel.NORMAL),
        ("1.35", VolatilityLevel.HIGH),
    ],
)
def test_volatility_boundaries(
    config: MarketRegimeConfig, ratio: str, expected: VolatilityLevel
) -> None:
    assert classify_volatility(Decimal(ratio), config) is expected


@pytest.mark.parametrize(
    "regime", [Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGE, Regime.TRANSITION]
)
def test_confidence_bounded(config: MarketRegimeConfig, regime: Regime) -> None:
    value = calculate_confidence(regime, Decimal("100"), Decimal("100"), Decimal("100"), config)
    assert Decimal("0") <= value <= Decimal("1")


def test_decimal_context_isolation(config: MarketRegimeConfig) -> None:
    original = getcontext().prec
    try:
        getcontext().prec = 6
        low = build_constants(config)
        getcontext().prec = 80
        high = build_constants(config)
        assert low == high
    finally:
        getcontext().prec = original
