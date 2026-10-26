from __future__ import annotations

from modular_core import (
    BacktestEngine,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    SetupStatus,
    Side,
    StrategyModule,
)

from strategy_modules.breakout_return import BreakoutReturnModule


class RangeProducer(StrategyModule):
    name = "range_producer"
    manifest = ModuleManifest(
        name=name,
        produced_features=("range.current",),
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(
            features=(
                Feature(
                    key="range.current",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value={"high": 100.0, "low": 90.0, "range_id": "session-1"},
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    feature_id=f"session-1:{view.index}",
                ),
            )
        )


class ConfirmationConsumer(StrategyModule):
    name = "confirmation_consumer"
    manifest = ModuleManifest(
        name=name,
        required_features=("breakout_return.confirmed",),
    )

    def __init__(self) -> None:
        self.seen: list[dict[str, object]] = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.features is None:
            return ModuleDecision()
        value = view.features.get("breakout_return.confirmed", symbol=view.current.symbol)
        if value is not None:
            self.seen.append(dict(value))
        return ModuleDecision()


def test_engine_lifecycle_and_same_bar_feature_pipeline(make_bar):
    consumer = ConfirmationConsumer()
    detector = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_key="range.current",
    )
    bars = [
        make_bar(0, 95, 99, 91, 96),
        make_bar(1, 101, 104, 100.5, 103),
        make_bar(2, 99.5, 101, 98.5, 99),
        make_bar(3, 98, 99, 96, 97),
    ]
    result = BacktestEngine([consumer, detector, RangeProducer()]).run(bars)

    assert result.modules == ("range_producer", "breakout_return", "confirmation_consumer")
    assert [row["setup"].status for row in result.journal.setups] == [
        SetupStatus.ARMED,
        SetupStatus.CONFIRMED,
    ]
    assert len(result.journal.signals) == 1
    assert result.journal.signals[0]["signal"].side is Side.SELL
    assert len(consumer.seen) == 1
    assert consumer.seen[0]["return_depth"] == 0.1
    assert result.journal.orders == []
    assert result.journal.errors == []


def test_engine_rolling_mode_is_deterministic(make_bar):
    bars = [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
        make_bar(3, 101, 104, 100.5, 103),
        make_bar(4, 99.5, 101, 98.5, 99),
    ]
    first = BacktestEngine(BreakoutReturnModule(lookback_bars=3)).run(bars)
    second = BacktestEngine(BreakoutReturnModule(lookback_bars=3)).run(bars)
    first_signal = first.journal.signals[0]["signal"]
    second_signal = second.journal.signals[0]["signal"]
    assert first_signal.metadata == second_signal.metadata
    assert first.run_manifest.run_id == second.run_manifest.run_id


def test_engine_checkpoint_contains_json_compatible_module_state(make_bar):
    module = BreakoutReturnModule(lookback_bars=3)
    bars = [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
        make_bar(3, 101, 104, 100.5, 103),
    ]
    result = BacktestEngine(module).run(bars)
    state = result.checkpoint.module_states[0]
    assert state.module == "breakout_return"
    assert state.schema_version == "1"
    assert state.payload["active"]["setup_id"].startswith("br-BTCUSDT-5m-above-")


def test_module_registry_yaml_import_path(tmp_path, make_bar):
    config = tmp_path / "modules.yaml"
    config.write_text(
        """modules:
  - class: strategy_modules.breakout_return:BreakoutReturnModule
    parameters:
      lookback_bars: 3
      breakout_confirmation: close
""",
        encoding="utf-8",
    )
    bars = [
        make_bar(0, 95, 100, 90, 96),
        make_bar(1, 96, 99, 91, 95),
        make_bar(2, 95, 98, 92, 94),
        make_bar(3, 99, 104, 98, 103),
    ]
    result = BacktestEngine.from_module_config(config).run(bars)
    assert result.modules == ("breakout_return",)
    assert result.journal.setups[0]["setup"].status is SetupStatus.ARMED
