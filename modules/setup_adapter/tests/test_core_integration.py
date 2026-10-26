from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from modular_core import BacktestEngine, Candle, ModuleRegistry
from modular_modules.entry_model import EntryModelModule, EntryPlan
from modular_session_range import SessionRangeModule
from strategy_modules.breakout_return import BreakoutReturnModule

from modular_setup_adapter import CanonicalSetupAdapterModule


def synthetic_bars(*, include_return: bool) -> list[Candle]:
    start = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)
    bars = [
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(48)
    ]
    bars.append(
        Candle(
            timestamp=start + timedelta(minutes=5 * 48),
            open=102.0,
            high=103.0,
            low=101.5,
            close=102.0,
            volume=2.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
    )
    if include_return:
        bars.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * 49),
                open=100.9,
                high=101.1,
                low=100.7,
                close=100.9,
                volume=2.0,
                symbol="BTCUSDT",
                timeframe="5m",
            )
        )
    return bars


def modules():
    return (
        EntryModelModule(
            default_entry_kind="limit_retracement",
            retracement_fraction="0.382",
            ttl_bars=36,
            allowed_entry_kinds=("limit_retracement",),
        ),
        CanonicalSetupAdapterModule(),
        BreakoutReturnModule(
            reference_mode="feature",
            reference_feature_key="session_range.completed",
            direction="above",
            breakout_confirmation="body",
            return_confirmation="body",
            min_return_depth=0.02,
            max_return_depth=0.20,
            max_return_bars=6,
        ),
        SessionRangeModule(
            strict_data_validation=True,
            sessions=[
                {
                    "name": "ny_first_4h",
                    "timezone": "America/New_York",
                    "start": "00:00",
                    "end": "04:00",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "minimum_bars": 48,
                    "minimum_range": "0",
                    "maximum_range": None,
                    "emit_active": True,
                    "dst_fold": 0,
                }
            ],
        ),
    )


def published(result, key: str):
    return [
        row["feature"]
        for row in result.journal.features
        if row["action"] == "published" and row["feature"].key == key
    ]


def test_dependency_plan_orders_full_same_bar_chain() -> None:
    plan = ModuleRegistry().validate_and_plan(modules(), base_timeframe="5m")
    names = [module.name for module in plan.modules]
    assert names == ["session_range", "breakout_return", "setup_adapter", "entry_model"]
    assert plan.produced_by["session_range.completed"] == "session_range"
    assert plan.produced_by["breakout_return.confirmed"] == "breakout_return"
    assert plan.produced_by["setup.confirmed"] == "setup_adapter"
    assert plan.produced_by["entry_model.plan"] == "entry_model"


def test_real_pipeline_has_no_plan_before_return() -> None:
    result = BacktestEngine(modules()).run(synthetic_bars(include_return=False))
    assert published(result, "breakout_return.confirmed") == []
    assert published(result, "setup.confirmed") == []
    assert published(result, "entry_model.plan") == []
    assert result.journal.errors == []


def test_real_pipeline_emits_one_deterministic_entry_plan_same_bar() -> None:
    result = BacktestEngine(modules()).run(synthetic_bars(include_return=True))
    source = published(result, "breakout_return.confirmed")
    canonical = published(result, "setup.confirmed")
    plans = published(result, "entry_model.plan")
    assert len(source) == len(canonical) == len(plans) == 1
    assert source[0].created_at == canonical[0].created_at == plans[0].created_at
    plan = EntryPlan.from_feature_value(plans[0].value)
    assert plan.side.value == "sell"
    assert plan.entry_kind.value == "limit_retracement"
    assert plan.price == Decimal("101.7022")
    assert plan.limit_price == Decimal("101.7022")
    assert plan.post_only is True
    assert result.modules == ("session_range", "breakout_return", "setup_adapter", "entry_model")
    assert result.journal.errors == []


def test_future_data_does_not_change_emitted_plan() -> None:
    exact = BacktestEngine(modules()).run(synthetic_bars(include_return=True))
    extended_bars = synthetic_bars(include_return=True)
    last = extended_bars[-1]
    for index in range(1, 5):
        extended_bars.append(
            Candle(
                timestamp=last.timestamp + timedelta(minutes=5 * index),
                open=100.8,
                high=101.0,
                low=100.5,
                close=100.7,
                volume=1.0,
                symbol="BTCUSDT",
                timeframe="5m",
            )
        )
    extended = BacktestEngine(modules()).run(extended_bars)
    exact_plan = EntryPlan.from_feature_value(published(exact, "entry_model.plan")[0].value)
    extended_plan = EntryPlan.from_feature_value(published(extended, "entry_model.plan")[0].value)
    assert exact_plan == extended_plan
