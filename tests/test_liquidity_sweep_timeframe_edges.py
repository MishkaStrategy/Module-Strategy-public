from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_core import Candle, MarketView
from strategy_modules.liquidity_sweep import LiquiditySweepConfig, LiquiditySweepModule
from strategy_modules.liquidity_sweep.models import BarSnapshot

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_config_rejects_unrepresentable_timeframe_duration() -> None:
    with pytest.raises(ValueError, match="out of supported range"):
        LiquiditySweepConfig(timeframe=f"{10**20}d")


def test_core_candle_timeframe_overflow_has_stable_module_error() -> None:
    item = Candle(
        NOW,
        10,
        11,
        9,
        10,
        symbol="BTCUSDT",
        timeframe=f"{10**20}d",
    )
    view = MarketView(
        base_bars=(item,),
        timeframe_bars={},
        index=0,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
    )
    with pytest.raises(ValueError, match="out of supported range"):
        LiquiditySweepModule().on_bar(view)


def test_near_datetime_max_close_overflow_has_stable_module_error() -> None:
    item = Candle(
        datetime.max.replace(tzinfo=UTC) - timedelta(hours=12),
        10,
        11,
        9,
        10,
        symbol="BTCUSDT",
        timeframe="1d",
    )
    view = MarketView(
        base_bars=(item,),
        timeframe_bars={},
        index=0,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
    )
    with pytest.raises(ValueError, match="out of supported range"):
        LiquiditySweepModule().on_bar(view)


def test_bar_snapshot_rejects_unrepresentable_duration() -> None:
    with pytest.raises(ValueError, match="out of supported range"):
        BarSnapshot(
            timestamp=NOW,
            close_time=NOW + timedelta(days=1),
            symbol="BTCUSDT",
            timeframe=f"{10**20}d",
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        )
