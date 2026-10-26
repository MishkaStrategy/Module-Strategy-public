from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    EngineConfig,
    InstrumentSpec,
    MarketView,
    ModuleDecision,
    StrategyModule,
    TimeframeSpec,
)
from modular_core.audit import Journal
from modular_core.events import InMemoryEventStore
from modular_core.execution import BrokerSimulator


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR = Candle(NOW, 100, 101, 99, 100, 1, "BTCUSDT", "5m")


class _Noop(StrategyModule):
    name = "noop"

    def __init__(self) -> None:
        self.started = False

    def on_start(self) -> None:
        self.started = True

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


@pytest.mark.parametrize("value", [False, 0, {}, object()])
def test_engine_rejects_invalid_config_without_defaulting(value: object) -> None:
    with pytest.raises(TypeError, match="config must be EngineConfig or None"):
        BacktestEngine(_Noop(), value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["5m", 1, object()])
def test_engine_rejects_invalid_timeframe_collection(value: object) -> None:
    with pytest.raises(TypeError, match="timeframes must be a sequence"):
        BacktestEngine(_Noop(), timeframes=value)  # type: ignore[arg-type]


def test_engine_rejects_invalid_timeframe_elements() -> None:
    with pytest.raises(TypeError, match="timeframes must contain TimeframeSpec"):
        BacktestEngine(_Noop(), timeframes=("15m",))  # type: ignore[arg-type]


def test_engine_accepts_typed_timeframe_specs() -> None:
    engine = BacktestEngine(
        _Noop(),
        timeframes=(TimeframeSpec(3, "15m"),),
    )
    assert engine.timeframes[0].output_timeframe == "15m"


@pytest.mark.parametrize("value", [False, 0, {}, object()])
def test_engine_rejects_invalid_instrument(value: object) -> None:
    with pytest.raises(TypeError, match="instrument must be InstrumentSpec or None"):
        BacktestEngine(_Noop(), instrument=value)  # type: ignore[arg-type]


def test_engine_accepts_typed_instrument() -> None:
    instrument = InstrumentSpec("BTCUSDT")
    assert BacktestEngine(_Noop(), instrument=instrument).instrument is instrument


@pytest.mark.parametrize("value", [False, 0, {}, object()])
def test_engine_rejects_invalid_module_registry(value: object) -> None:
    with pytest.raises(TypeError, match="module_registry must be ModuleRegistry or None"):
        BacktestEngine(_Noop(), module_registry=value)  # type: ignore[arg-type]


def test_from_module_config_rejects_registry_before_loading_config(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.yml"
    with pytest.raises(TypeError, match="module_registry must be ModuleRegistry or None"):
        BacktestEngine.from_module_config(
            missing,
            module_registry=False,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field", ["event_store_factory", "execution_factory"])
def test_engine_requires_callable_factories(field: str) -> None:
    with pytest.raises(TypeError, match=f"{field} must be callable"):
        BacktestEngine(_Noop(), **{field: 0})  # type: ignore[arg-type]


class _FalseyExecutionFactory:
    def __init__(self) -> None:
        self.called = False

    def __bool__(self) -> bool:
        return False

    def __call__(
        self,
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
    ) -> BrokerSimulator:
        self.called = True
        return BrokerSimulator(config, journal, instrument)


def test_falsey_callable_execution_factory_is_not_replaced() -> None:
    factory = _FalseyExecutionFactory()
    result = BacktestEngine(_Noop(), execution_factory=factory).run((BAR,))

    assert result.bars_processed == 1
    assert factory.called


class _InvalidEventStoreFactory:
    def __call__(self) -> object:
        return object()


def test_event_store_factory_result_is_validated_before_module_start() -> None:
    module = _Noop()
    engine = BacktestEngine(
        module,
        event_store_factory=_InvalidEventStoreFactory(),  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError, match="event_store_factory must return EventStore"):
        engine.run((BAR,))
    assert not module.started


class _InvalidExecutionFactory:
    def __call__(
        self,
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
    ) -> object:
        return object()


def test_execution_factory_result_is_validated_before_module_start() -> None:
    module = _Noop()
    engine = BacktestEngine(
        module,
        execution_factory=_InvalidExecutionFactory(),  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError, match="execution_factory must return BrokerSimulator"):
        engine.run((BAR,))
    assert not module.started


class _ExplodingStoreFactory:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> InMemoryEventStore:
        self.called = True
        raise AssertionError("event store factory must not run")


def test_instrument_mismatch_fails_before_event_store_factory() -> None:
    factory = _ExplodingStoreFactory()
    engine = BacktestEngine(
        _Noop(),
        instrument=InstrumentSpec("ETHUSDT"),
        event_store_factory=factory,
    )

    with pytest.raises(ValueError, match="does not match data BTCUSDT"):
        engine.run((BAR,))
    assert not factory.called


@pytest.mark.parametrize("value", [1, True, "not-candles", object()])
def test_run_uses_canonical_candle_iterable_boundary(value: object) -> None:
    with pytest.raises(TypeError, match="candles must be an iterable of Candle"):
        BacktestEngine(_Noop()).run(value)  # type: ignore[arg-type]


def test_valid_event_store_factory_still_runs() -> None:
    result = BacktestEngine(
        _Noop(),
        event_store_factory=InMemoryEventStore,
    ).run((BAR,))
    assert result.bars_processed == 1
