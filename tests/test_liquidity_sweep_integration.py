from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable


from modular_core import Candle, MarketView, SetupStatus
from strategy_modules.liquidity_sweep import LiquiditySweepModule

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def candle(
    i: int,
    o: float,
    h: float,
    low: float,
    c: float,
    *,
    timeframe: str = "5m",
    symbol: str = "BTCUSDT",
) -> Candle:
    step = 5 if timeframe == "5m" else 15
    return Candle(START + timedelta(minutes=i * step), o, h, low, c, 1.0, symbol, timeframe)


def view(
    history: list[Candle],
    *,
    gap: bool = False,
    tf_history: dict[str, list[Candle]] | None = None,
    features: object | None = None,
) -> MarketView:
    return MarketView(
        base_bars=tuple(history),
        timeframe_bars={key: tuple(value) for key, value in (tf_history or {}).items()},
        index=len(history) - 1,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        data_gap=gap,
        features=features,
    )


def feed(module: LiquiditySweepModule, bars: Iterable[Candle], *, gap_at: set[int] | None = None):
    history: list[Candle] = []
    decisions = []
    for index, item in enumerate(bars):
        history.append(item)
        decisions.append(module.on_bar(view(history, gap=index in (gap_at or set()))))
    return decisions


def high_level_bars() -> list[Candle]:
    return [
        candle(0, 9.0, 10.0, 8.0, 9.0),
        candle(1, 10.0, 12.0, 9.0, 11.0),
        candle(2, 10.5, 11.0, 9.5, 10.0),
    ]


def low_level_bars() -> list[Candle]:
    return [
        candle(0, 10.0, 11.0, 9.0, 10.0),
        candle(1, 9.0, 10.0, 7.0, 8.0),
        candle(2, 8.5, 10.0, 8.0, 9.0),
    ]


def make_module(**kwargs) -> LiquiditySweepModule:
    parameters = {
        "pivot_left_bars": 1,
        "pivot_right_bars": 1,
        "min_wick_to_body_ratio": 0.5,
        "min_wick_fraction": 0.2,
    }
    parameters.update(kwargs)
    return LiquiditySweepModule(**parameters)


def created_level(decision):
    return next(feature for feature in decision.features if feature.key == "liquidity_sweep.level")


def sweep_event(decision):
    return next(feature for feature in decision.features if feature.key == "liquidity_sweep.event")


def test_registry_loads_and_plans_module_without_core_changes():
    from modular_core import ModuleRegistry

    registry = ModuleRegistry()
    module = registry.load_import_path(
        "strategy_modules.liquidity_sweep:LiquiditySweepModule",
        {"timeframe": "15m", "pivot_left_bars": 1, "pivot_right_bars": 1},
    )
    plan = registry.validate_and_plan((module,), base_timeframe="5m")
    assert plan.modules == (module,)
    assert plan.manifests[0].name == "liquidity_sweep"
    assert "15m" in plan.required_timeframes


def test_backtest_engine_full_lifecycle_publishes_without_orders():
    from modular_core import BacktestEngine

    bars = high_level_bars() + [candle(3, 11.6, 12.1, 10.8, 11.2)]
    result = BacktestEngine(make_module(emit_signals=True)).run(bars)
    assert result.bars_processed == 4
    assert result.modules == ("liquidity_sweep",)
    assert not result.journal.errors
    assert any(row["feature"].key == "liquidity_sweep.level" for row in result.journal.features)
    assert any(row["feature"].key == "liquidity_sweep.event" for row in result.journal.features)
    assert any(row["setup"].status is SetupStatus.CONFIRMED for row in result.journal.setups)
    assert result.journal.signals
    assert result.journal.orders == []
