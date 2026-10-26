from __future__ import annotations

import gc
import random
import weakref
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from modular_backtest.recovery import (
    ReplayRecoveryError,
    _result_digest,
    run_with_replay_restart,
    run_with_runtime_snapshot_restart,
)
from modular_backtest.scalability import PerformanceConfig, ScalableBacktestEngine
from modular_backtest.scalability.manifest import incremental_candle_data_hash
from modular_core.config import DataConfig, EngineConfig
from modular_core.engine import BacktestEngine
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle, OrderRequest, OrderType, Side
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule
from modular_core.timeframes import TimeframeSpec


class StatefulEntryModule(StrategyModule):
    name = "stateful_entry"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
        state_schema_version="1",
    )

    def __init__(self, *, initial_counter: int = 0) -> None:
        self.counter = initial_counter
        self.sent = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.counter += 1
        if self.sent:
            return ModuleDecision()
        self.sent = True
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    stop_loss=80.0,
                    take_profit=130.0,
                    client_order_id="entry-1",
                ),
            )
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return {"counter": self.counter, "sent": self.sent}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.counter = int(state["counter"])
        self.sent = bool(state["sent"])


def _bars(count: int = 8) -> tuple[Candle, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple(
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=100.0 + index,
            high=102.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=10.0 + index,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(count)
    )


def _factory(*, initial_counter: int = 0):
    def create() -> BacktestEngine:
        return BacktestEngine(
            StatefulEntryModule(initial_counter=initial_counter),
            EngineConfig(
                close_open_position_at_end=True,
                data=DataConfig(expected_interval_seconds=300, gap_policy="reject"),
            ),
            timeframes=(
                TimeframeSpec(
                    3,
                    "15m",
                    gap_safe=True,
                    align_to_clock=True,
                    alignment_timezone="UTC",
                ),
            ),
        )

    return create


@pytest.mark.checkpoint
def test_prefix_replay_restart_matches_continuous_result() -> None:
    replay = run_with_replay_restart(
        _factory(),
        _bars(),
        restart_at_index=3,
        seed=17,
        commit_sha="a" * 40,
    )

    assert replay.restart_verified is True
    assert replay.continuous_sha256 == replay.restarted_sha256
    assert replay.checkpoint is not None
    assert replay.checkpoint.bars_processed == 4
    assert replay.checkpoint.kernel.last_bar_index == 3
    assert replay.checkpoint.seed == 17
    assert replay.checkpoint.commit_sha == "a" * 40
    assert replay.result.bars_processed == 8
    assert len(replay.result.journal.trades) == 1
    assert replay.result.journal.trades[0].exit_reason == "end_of_data"


def test_continuous_mode_is_deterministic_without_system_time() -> None:
    left = run_with_replay_restart(_factory(), _bars())
    right = run_with_replay_restart(_factory(), _bars())

    assert left.restart_verified is False
    assert left.result.run_manifest == right.result.run_manifest
    assert left.result.journal.events == right.result.journal.events
    assert left.result.journal.equity == right.result.journal.equity


@pytest.mark.parametrize("restart_at_index", [-1, 7, 8])
def test_restart_index_must_leave_a_continuation_bar(restart_at_index: int) -> None:
    with pytest.raises(ValueError, match="leave at least one candle"):
        run_with_replay_restart(
            _factory(),
            _bars(),
            restart_at_index=restart_at_index,
        )


def test_continuous_runtime_is_released_before_restart_factory() -> None:
    first_module_ref: weakref.ReferenceType[StatefulEntryModule] | None = None
    calls = 0

    def create() -> BacktestEngine:
        nonlocal calls, first_module_ref
        calls += 1
        if calls == 2:
            gc.collect()
            assert first_module_ref is not None
            assert first_module_ref() is None
        module = StatefulEntryModule()
        if calls == 1:
            first_module_ref = weakref.ref(module)
        return BacktestEngine(
            module,
            EngineConfig(
                close_open_position_at_end=True,
                data=DataConfig(expected_interval_seconds=300, gap_policy="reject"),
            ),
        )

    replay = run_with_replay_restart(create, _bars(), restart_at_index=3)

    assert replay.restart_verified is True
    assert calls == 2


def test_replay_fails_closed_when_fresh_runtime_does_not_match() -> None:
    calls = 0

    def inconsistent_factory() -> BacktestEngine:
        nonlocal calls
        calls += 1
        initial_counter = 0 if calls == 1 else 10
        return _factory(initial_counter=initial_counter)()

    with pytest.raises(ReplayRecoveryError, match="checkpoint boundary"):
        run_with_replay_restart(
            inconsistent_factory,
            _bars(),
            restart_at_index=3,
        )


class LifecycleModule(StrategyModule):
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_on_start: bool = False,
        fail_on_bar: bool = False,
    ) -> None:
        self.name = name
        self.manifest = ModuleManifest(
            name=name,
            version="0.1.0",
            api_version="1.0",
            skip_until_ready=False,
        )
        self.events = events
        self.fail_on_start = fail_on_start
        self.fail_on_bar = fail_on_bar

    def on_start(self) -> None:
        self.events.append(f"{self.name}:start")
        if self.fail_on_start:
            raise RuntimeError("start failure")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.events.append(f"{self.name}:bar")
        if self.fail_on_bar:
            raise RuntimeError("bar failure")
        return ModuleDecision()

    def on_finish(self) -> None:
        self.events.append(f"{self.name}:finish")


class RandomStateModule(StrategyModule):
    name = "random_state"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
        state_schema_version="1",
    )

    def __init__(self) -> None:
        self.constructor_draw = random.random()
        self.draws: list[float] = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.draws.append(random.random())
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {
            "constructor_draw": self.constructor_draw,
            "draws": tuple(self.draws),
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.constructor_draw = float(state["constructor_draw"])
        draws = state["draws"]
        if not isinstance(draws, tuple | list):
            raise TypeError("draws must be a sequence")
        self.draws = [float(value) for value in draws]


def _strict_engine(modules: tuple[StrategyModule, ...]) -> BacktestEngine:
    return BacktestEngine(
        modules,
        EngineConfig(
            strict_module_errors=True,
            data=DataConfig(expected_interval_seconds=300, gap_policy="reject"),
        ),
    )


def test_started_modules_are_finished_when_a_later_module_fails_on_start() -> None:
    events: list[str] = []

    def create() -> BacktestEngine:
        return _strict_engine(
            (
                LifecycleModule("first", events),
                LifecycleModule("second", events, fail_on_start=True),
            )
        )

    with pytest.raises(RuntimeError, match="start failure"):
        run_with_replay_restart(create, _bars(2))

    assert events == ["first:start", "second:start", "first:finish"]


def test_started_modules_are_finished_when_bar_processing_fails() -> None:
    events: list[str] = []

    def create() -> BacktestEngine:
        return _strict_engine((LifecycleModule("failing", events, fail_on_bar=True),))

    with pytest.raises(RuntimeError, match="bar failure"):
        run_with_replay_restart(create, _bars(2))

    assert events == ["failing:start", "failing:bar", "failing:finish"]


def test_random_seed_is_replayed_and_caller_state_is_restored() -> None:
    def create() -> BacktestEngine:
        return _strict_engine((RandomStateModule(),))

    random.seed(991)
    caller_state = random.getstate()
    left = run_with_replay_restart(create, _bars(), restart_at_index=3, seed=17)
    assert random.getstate() == caller_state

    right = run_with_replay_restart(create, _bars(), restart_at_index=3, seed=17)
    different = run_with_replay_restart(create, _bars(), restart_at_index=3, seed=18)

    assert left.restart_verified is True
    assert left.result.checkpoint.module_states == right.result.checkpoint.module_states
    assert left.result.checkpoint.module_states != different.result.checkpoint.module_states


class _PreparedBars:
    def __init__(self, bars: tuple[Candle, ...]) -> None:
        self._bars = bars
        self.total_bars = len(bars)
        self.first_candle = bars[0]
        self.last_candle = bars[-1]
        self.data_hash = incremental_candle_data_hash(bars)
        self.traversals = 0

    def iter_candles(self):
        self.traversals += 1
        return iter(self._bars)

    def iter_candles_from(self, start_index: int):
        self.traversals += 1
        return iter(self._bars[start_index:])


def _scalable_factory(
    *,
    lifecycle: list[str] | None = None,
    bar_calls: list[int] | None = None,
):
    def create() -> ScalableBacktestEngine:
        module = StatefulEntryModule()
        original_start = module.on_start
        original_bar = module.on_bar
        original_finish = module.on_finish

        def on_start() -> None:
            if lifecycle is not None:
                lifecycle.append("start")
            original_start()

        def on_bar(view: MarketView) -> ModuleDecision:
            if bar_calls is not None:
                bar_calls.append(view.index)
            return original_bar(view)

        def on_finish() -> None:
            if lifecycle is not None:
                lifecycle.append("finish")
            original_finish()

        module.on_start = on_start  # type: ignore[method-assign]
        module.on_bar = on_bar  # type: ignore[method-assign]
        module.on_finish = on_finish  # type: ignore[method-assign]
        performance = PerformanceConfig(
            mode="canonical_audit",
            max_memory_mb=1024,
            progress_interval_bars=10_000,
            max_base_history=64,
            max_timeframe_history=64,
        )
        return ScalableBacktestEngine(
            module,
            EngineConfig(
                close_open_position_at_end=True,
                data=DataConfig(
                    expected_interval_seconds=300,
                    gap_policy="reject",
                    max_base_history=64,
                ),
            ),
            timeframes=(
                TimeframeSpec(
                    3,
                    "15m",
                    gap_safe=True,
                    align_to_clock=True,
                    alignment_timezone="UTC",
                    max_history=64,
                ),
            ),
            performance=performance,
        )

    return create


def test_runtime_snapshot_restart_matches_single_pass_without_prefix_resimulation() -> None:
    bars = _bars(20)
    direct_source = _PreparedBars(bars)
    restart_source = _PreparedBars(bars)
    direct = _scalable_factory()().run_prepared(direct_source)
    calls: list[int] = []
    lifecycle: list[str] = []

    replay = run_with_runtime_snapshot_restart(
        _scalable_factory(lifecycle=lifecycle, bar_calls=calls),
        restart_source,
        restart_at_index=7,
        seed=17,
    )

    assert replay.restart_verified is True
    assert replay.recovery_mode == "runtime_snapshot_v1"
    assert replay.simulation_passes == 1
    assert replay.source_traversals == 2
    assert restart_source.traversals == 2
    assert replay.source_rows_replayed == 0
    assert calls == list(range(20))
    assert lifecycle == ["start", "finish"]
    assert replay.checkpoint is not None
    assert replay.checkpoint.bars_processed == 8
    assert replay.checkpoint.snapshot_schema_version == "1"
    assert replay.checkpoint.snapshot_bytes is not None
    assert replay.checkpoint.snapshot_bytes > 0
    snapshot_payload = replay.result.journal.streaming_store.path.with_name(
        "runtime-snapshot-v1.pkl"
    )
    assert not snapshot_payload.exists()
    assert _result_digest(replay.result) == _result_digest(direct)


def test_runtime_snapshot_releases_first_module_before_fresh_factory() -> None:
    bars = _bars(12)
    source = _PreparedBars(bars)
    first_ref: weakref.ReferenceType[StatefulEntryModule] | None = None
    calls = 0

    def create() -> ScalableBacktestEngine:
        nonlocal calls, first_ref
        calls += 1
        if calls == 2:
            gc.collect()
            assert first_ref is not None
            assert first_ref() is None
        engine = _scalable_factory()()
        if calls == 1:
            first_ref = weakref.ref(engine.module)
        return engine

    replay = run_with_runtime_snapshot_restart(
        create,
        source,
        restart_at_index=4,
    )

    assert replay.restart_verified is True
    assert calls == 2


class _ShiftedContinuation(_PreparedBars):
    def iter_candles_from(self, start_index: int):
        self.traversals += 1
        return iter(self._bars[start_index + 1 :])


def test_runtime_snapshot_rejects_changed_continuation_boundary() -> None:
    source = _ShiftedContinuation(_bars(12))

    with pytest.raises(ReplayRecoveryError, match="continuation boundary changed"):
        run_with_runtime_snapshot_restart(
            _scalable_factory(),
            source,
            restart_at_index=4,
        )


def test_runtime_snapshot_fails_closed_on_payload_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modular_backtest.recovery as recovery_module

    original = recovery_module._build_runtime_snapshot
    captured = None

    def corrupt(session):
        nonlocal captured
        snapshot = original(session)
        payload = bytearray(snapshot.payload_path.read_bytes())
        payload[len(payload) // 2] ^= 0x01
        snapshot.payload_path.write_bytes(payload)
        captured = snapshot
        return snapshot

    monkeypatch.setattr(recovery_module, "_build_runtime_snapshot", corrupt)

    with pytest.raises(ReplayRecoveryError, match="payload integrity"):
        run_with_runtime_snapshot_restart(
            _scalable_factory(),
            _PreparedBars(_bars(12)),
            restart_at_index=4,
        )

    assert captured is not None
    assert not captured.payload_path.exists()
    assert not captured.event_stream_path.exists()
