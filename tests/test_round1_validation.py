from datetime import UTC, datetime, timedelta, timezone

import pytest

from modular_core import (
    Candle,
    EngineConfig,
    EquityPoint,
    ExecutionConfig,
    Feature,
    FeeConfig,
    Fill,
    PositionSide,
    RiskConfig,
    Side,
    Trade,
)


def test_candle_normalizes_aware_timestamp_to_utc() -> None:
    local = timezone(timedelta(hours=7))
    candle = Candle(datetime(2026, 1, 1, 7, tzinfo=local), 1, 2, 1, 2, 0, "BTCUSDT", "5m")
    assert candle.timestamp == datetime(2026, 1, 1, tzinfo=UTC)
    assert candle.timestamp.tzinfo is UTC


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_candle_rejects_non_finite_numbers(field: str) -> None:
    values = {"open": 1.0, "high": 2.0, "low": 1.0, "close": 2.0, "volume": 1.0}
    values[field] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        Candle(datetime.now(UTC), symbol="BTCUSDT", timeframe="5m", **values)


def test_fill_trade_and_equity_reject_non_finite_values() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="finite"):
        Fill("o", now, Side.BUY, 1, float("inf"), 0, 0, "entry", "maker")
    with pytest.raises(ValueError, match="finite"):
        Trade(PositionSide.LONG, 1, now, now, 1, 2, 1, 0, float("nan"), "target")
    with pytest.raises(ValueError, match="finite"):
        EquityPoint(now, 1, 0, float("inf"))


def test_config_rejects_nan_and_inf() -> None:
    with pytest.raises(ValueError, match="finite"):
        FeeConfig(maker_rate=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        ExecutionConfig(slippage_bps=float("inf"))
    with pytest.raises(ValueError, match="finite"):
        RiskConfig(max_notional_multiple=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        EngineConfig(initial_cash=float("inf"))


def test_feature_normalizes_timestamps_to_utc() -> None:
    local = timezone(timedelta(hours=-5))
    feature = Feature("x.y", "x", datetime(2026, 1, 1, tzinfo=local), 1)
    assert feature.created_at.tzinfo is UTC
