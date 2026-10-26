from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import modular_core

from modular_strategy_fibonacci import (
    ACTIVE_PROJECTION_KEY,
    COMPLETED_WAVE_KEY,
    FibonacciModule,
    SequentialFibonacciEngine,
    WorkflowPhase,
)


def _seeded_module() -> FibonacciModule:
    module = FibonacciModule(
        timeframes=("15m",),
        dataset_hash="core-integration-fixture",
        publish_short_setup=False,
    )
    engine = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="core-integration-fixture",
        config=module.config,
    )
    engine.seed_wave(
        direction="bullish",
        start_time=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        start_price=Decimal("100"),
        end_time=datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_price=Decimal("150"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )
    module._engines[("BTCUSDT", "15m")] = engine
    return module


def _bars() -> tuple[modular_core.Candle, ...]:
    start = datetime(2025, 1, 1, 1, 15, tzinfo=timezone.utc)
    rows = (
        (145.0, 149.0, 125.0, 130.0),  # correction confirms the full endpoint
        (130.0, 161.0, 128.0, 159.0),  # reaches -0.18 / -0.27 target area
        (159.0, 160.0, 150.0, 154.0),
    )
    return tuple(
        modular_core.Candle(
            timestamp=start + timedelta(minutes=15 * index),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="15m",
        )
        for index, (open_, high, low, close) in enumerate(rows)
    )


def test_real_core_exposes_required_module_api() -> None:
    assert modular_core.__version__ == "0.4.2"
    for name in (
        "BacktestEngine",
        "Candle",
        "Feature",
        "FeatureDelete",
        "MarketView",
        "ModuleDecision",
        "ModuleManifest",
        "ModuleRegistry",
        "StrategyModule",
    ):
        assert hasattr(modular_core, name), name


def test_real_core_registry_accepts_fibonacci_manifest() -> None:
    registry = modular_core.ModuleRegistry()
    module = FibonacciModule(timeframes=("15m",), publish_short_setup=False)
    plan = registry.validate_and_plan((module,), base_timeframe="15m")
    assert plan.modules == (module,)
    assert plan.produced_by[ACTIVE_PROJECTION_KEY] == "fibonacci"
    assert plan.produced_by[COMPLETED_WAVE_KEY] == "fibonacci"


def test_real_backtest_engine_publishes_fibonacci_features_without_orders() -> None:
    module = _seeded_module()
    result = modular_core.BacktestEngine(module).run(_bars())

    assert result.modules == ("fibonacci",)
    assert result.bars_processed == len(_bars())
    assert not result.journal.errors

    published = [row["feature"] for row in result.journal.features if row["action"] == "published"]
    keys = {feature.key for feature in published}
    assert ACTIVE_PROJECTION_KEY in keys
    assert COMPLETED_WAVE_KEY in keys
    assert not result.journal.trades


def test_checkpoint_state_roundtrip_preserves_full_endpoint_revision() -> None:
    module = _seeded_module()
    result = modular_core.BacktestEngine(module).run(_bars()[:1])
    state = result.checkpoint.module_states[0].payload

    restored = FibonacciModule(
        timeframes=("15m",),
        dataset_hash="core-integration-fixture",
        publish_short_setup=False,
    )
    restored.restore_state(state)
    restored_state = restored.snapshot_state()

    def thaw(value):
        if isinstance(value, dict) or hasattr(value, "items"):
            return {str(key): thaw(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [thaw(item) for item in value]
        return value

    assert thaw(restored_state["engines"]) == thaw(state["engines"])
