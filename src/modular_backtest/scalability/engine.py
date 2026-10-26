from __future__ import annotations

import gc
import json
import random
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from modular_core.adapters import DataFeed, SimulationClock
from modular_core.audit import calculate_metrics, to_primitive
from modular_core.checkpoints import build_checkpoint
from modular_core.data import timeframe_to_seconds
from modular_core.engine import KERNEL_VERSION, BacktestEngine, BacktestResult
from modular_core.features import FeatureStore
from modular_core.manifest import parameters_for
from modular_core.models import Candle, EquityPoint, ReasonCode
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule
from modular_core.readiness import ReadinessView, calculate_readiness
from modular_core.series import ReadOnlySeries
from modular_core.setups import SetupRegistry
from modular_core.timeframes import TimeframeStore

from .config import PerformanceConfig, ResourceLimitExceeded, RuntimeCounters
from .events import StreamingEventStore
from .journal import ScalableJournal
from .manifest import (
    build_scalable_run_manifest_from_hash,
    incremental_candle_data_hash,
)
from .memory import MemoryGuard


class PreparedCandleStream(Protocol):
    data_hash: str
    first_candle: Candle
    last_candle: Candle

    @property
    def total_bars(self) -> int: ...

    def iter_candles(self) -> Iterable[Candle]: ...


@dataclass(frozen=True, slots=True)
class _RunSource:
    candles: Iterable[Candle]
    total_bars: int
    data_hash: str
    first_candle: Candle
    last_candle: Candle


class ProgressReporter:
    def __init__(self, total_bars: int, interval: int) -> None:
        self.total_bars = total_bars
        self.interval = interval
        self.started = time.monotonic()

    def maybe_report(self, journal: ScalableJournal, bars_processed: int) -> RuntimeCounters | None:
        if bars_processed != self.total_bars and bars_processed % self.interval != 0:
            return None
        elapsed = max(time.monotonic() - self.started, 1e-9)
        speed = bars_processed / elapsed
        remaining = max(self.total_bars - bars_processed, 0)
        eta = remaining / speed if speed > 0 else None
        counters = journal.counters(bars_processed)
        payload: dict[str, object] = {
            **counters.to_dict(),
            "total_bars": self.total_bars,
            "percent": bars_processed / self.total_bars * 100,
            "elapsed_seconds": elapsed,
            "bars_per_second": speed,
            "eta_seconds": eta,
        }
        print(json.dumps({"backtest_progress": payload}, sort_keys=True), file=sys.stderr)
        return counters


def _validated_sequence(
    candles: Iterable[Candle] | DataFeed,
    engine: BacktestEngine,
) -> Sequence[Candle]:
    if isinstance(candles, Sequence) and not isinstance(candles, (str, bytes)):
        bars = candles
    else:
        bars = tuple(candles)
    if not bars:
        raise ValueError("no candles supplied")
    expected_seconds = engine.config.data.expected_interval_seconds or timeframe_to_seconds(
        bars[0].timeframe
    )
    expected = timedelta(seconds=expected_seconds) if expected_seconds else None
    previous: Candle | None = None
    for candle in bars:
        _validate_stream_bar(
            candle,
            first=bars[0],
            previous=previous,
            expected=expected,
            reject_gaps=engine.config.data.gap_policy == "reject",
        )
        previous = candle
    return bars


def _validate_stream_bar(
    candle: Candle,
    *,
    first: Candle,
    previous: Candle | None,
    expected: timedelta | None,
    reject_gaps: bool,
) -> None:
    if not isinstance(candle, Candle):
        raise TypeError("candles must contain Candle")
    if candle.symbol != first.symbol:
        raise ValueError("mixed symbols are not supported")
    if candle.timeframe != first.timeframe:
        raise ValueError("mixed base timeframes are not supported")
    if previous is None:
        return
    if candle.timestamp <= previous.timestamp:
        raise ValueError("candles must be strictly chronological")
    if previous.close_time > candle.timestamp:
        raise ValueError("candle intervals cannot overlap")
    if reject_gaps and expected is not None and candle.timestamp - previous.timestamp != expected:
        raise ValueError("data gap in candle sequence")


def _decision_has_substantial_action(decision: ModuleDecision) -> bool:
    return bool(
        decision.setups
        or decision.signals
        or decision.features
        or decision.feature_deletes
        or decision.orders
        or decision.cancellations
        or decision.position_updates
        or decision.closes
    )


def _trim_threshold(max_history: int | None) -> int | None:
    if max_history is None:
        return None
    return max_history + max(64, min(4_096, max(1, max_history // 4)))


@dataclass(slots=True)
class ScalableRuntimeState:
    """Serializable bounded runtime state for a direct checkpoint restart."""

    run_manifest: Any
    journal: ScalableJournal
    broker: Any
    feature_store: FeatureStore
    setup_registry: SetupRegistry
    clock: SimulationClock
    store: TimeframeStore
    base_history: list[Candle]
    order_event_cursor: int
    trade_cursor: int
    active_module_names: tuple[str, ...]
    quarantined_modules: set[str]
    previous_bar: Candle | None
    last_bar: Candle | None
    bars_processed: int
    random_state: tuple[Any, ...]


class ScalableRunSession:
    """One bounded simulation session that can be suspended and restored directly."""

    def __init__(self, engine: "ScalableBacktestEngine", source: _RunSource) -> None:
        self.engine = engine
        self.source = source
        first = source.first_candle
        self.plan = engine.module_registry.validate_and_plan(
            engine.modules,
            base_timeframe=first.timeframe,
        )
        self.effective_timeframes = engine._resolve_timeframes(self.plan, first.timeframe)
        self.run_manifest = build_scalable_run_manifest_from_hash(
            kernel_version=KERNEL_VERSION,
            created_at=first.timestamp,
            data_hash=source.data_hash,
            symbol=first.symbol,
            base_timeframe=first.timeframe,
            bars=source.total_bars,
            config=engine.config,
            manifests=self.plan.manifests,
            module_parameters=tuple(parameters_for(module) for module in self.plan.modules),
        )
        instrument = engine._resolve_instrument(first)
        event_dir = Path(tempfile.mkdtemp(prefix="modular-backtest-events-"))
        event_store = StreamingEventStore(
            event_dir / "events.jsonl",
            retained_window=engine.performance.retained_event_window,
        )
        self.journal = ScalableJournal(
            run_id=self.run_manifest.run_id,
            event_store=event_store,
            performance=engine.performance,
        )
        self.broker = engine.execution_factory(engine.config, self.journal, instrument)
        self.feature_store = FeatureStore()
        self.setup_registry = SetupRegistry()
        self.clock = SimulationClock()
        expected_seconds = engine.config.data.expected_interval_seconds or timeframe_to_seconds(
            first.timeframe
        )
        self.expected_interval = (
            timedelta(seconds=expected_seconds) if expected_seconds is not None else None
        )
        self.store = TimeframeStore(
            self.effective_timeframes,
            expected_base_interval=self.expected_interval,
        )
        self.max_base_history = engine.config.data.max_base_history
        self.base_trim_threshold = _trim_threshold(self.max_base_history)
        self.base_history: list[Candle] = []
        self.order_event_cursor = 0
        self.trade_cursor = 0
        self.active_modules: list[StrategyModule] = []
        self.manifest_by_name = {manifest.name: manifest for manifest in self.plan.manifests}
        self.quarantined_modules: set[str] = set()
        self.dependents_by_module = engine._module_dependents(self.plan)
        self.guard = MemoryGuard(engine.performance.max_memory_mb)
        self.reporter = ProgressReporter(
            source.total_bars,
            engine.performance.progress_interval_bars,
        )
        self.guard_interval = min(engine.performance.progress_interval_bars, 1_024)
        self.previous_bar: Candle | None = None
        self.last_bar: Candle | None = None
        self.bars_processed = 0
        self._modules_finished = False
        self._suspended = False
        self._gc_was_enabled = gc.isenabled()
        self._start()

    @classmethod
    def restore(
        cls,
        engine: "ScalableBacktestEngine",
        source: _RunSource,
        state: ScalableRuntimeState,
        *,
        module_states: Sequence[Any],
    ) -> "ScalableRunSession":
        from modular_core.checkpoints import restore_module_states

        session = cls.__new__(cls)
        session.engine = engine
        session.source = source
        first = source.first_candle
        session.plan = engine.module_registry.validate_and_plan(
            engine.modules,
            base_timeframe=first.timeframe,
        )
        session.effective_timeframes = engine._resolve_timeframes(
            session.plan,
            first.timeframe,
        )
        candidate_manifest = build_scalable_run_manifest_from_hash(
            kernel_version=KERNEL_VERSION,
            created_at=first.timestamp,
            data_hash=source.data_hash,
            symbol=first.symbol,
            base_timeframe=first.timeframe,
            bars=source.total_bars,
            config=engine.config,
            manifests=session.plan.manifests,
            module_parameters=tuple(parameters_for(module) for module in session.plan.modules),
        )
        if candidate_manifest != state.run_manifest:
            state.journal.streaming_store.cleanup()
            raise ValueError("run manifest changed while restoring runtime snapshot")
        session.run_manifest = state.run_manifest
        session.journal = state.journal
        session.broker = state.broker
        if session.broker.journal is not session.journal:
            session.journal.streaming_store.cleanup()
            raise RuntimeError("restored broker lost its journal identity")
        session.feature_store = state.feature_store
        session.setup_registry = state.setup_registry
        session.clock = state.clock
        session.store = state.store
        session.max_base_history = engine.config.data.max_base_history
        session.base_trim_threshold = _trim_threshold(session.max_base_history)
        session.base_history = state.base_history
        session.order_event_cursor = state.order_event_cursor
        session.trade_cursor = state.trade_cursor
        session.manifest_by_name = {manifest.name: manifest for manifest in session.plan.manifests}
        session.dependents_by_module = engine._module_dependents(session.plan)
        by_name = {module.name: module for module in session.plan.modules}
        missing = sorted(set(state.active_module_names) - set(by_name))
        if missing:
            session.journal.streaming_store.cleanup()
            raise ValueError(f"runtime snapshot active module mismatch: {missing}")
        session.active_modules = [by_name[name] for name in state.active_module_names]
        restore_module_states(session.active_modules, module_states)
        session.quarantined_modules = set(state.quarantined_modules)
        expected_seconds = engine.config.data.expected_interval_seconds or timeframe_to_seconds(
            first.timeframe
        )
        session.expected_interval = (
            timedelta(seconds=expected_seconds) if expected_seconds is not None else None
        )
        session.guard = MemoryGuard(engine.performance.max_memory_mb)
        session.reporter = ProgressReporter(
            source.total_bars,
            engine.performance.progress_interval_bars,
        )
        session.guard_interval = min(engine.performance.progress_interval_bars, 1_024)
        session.previous_bar = state.previous_bar
        session.last_bar = state.last_bar
        session.bars_processed = state.bars_processed
        session._modules_finished = False
        session._suspended = False
        session._gc_was_enabled = gc.isenabled()
        random.setstate(state.random_state)
        if engine.performance.disable_cyclic_gc_in_hot_loop and session._gc_was_enabled:
            gc.disable()
        return session

    def _start(self) -> None:
        first = self.source.first_candle
        try:
            self.journal.emit_event(
                timestamp=first.timestamp,
                event_type="run.started",
                source="kernel",
                payload={"run_manifest": to_primitive(self.run_manifest)},
                symbol=first.symbol,
            )
            for module in self.plan.modules:
                try:
                    module.on_start()
                except Exception as exc:
                    self.journal.log_error(
                        first.timestamp,
                        f"module_start:{module.name}",
                        exc,
                    )
                    if self.engine.config.strict_module_errors:
                        raise
                    self.journal.log_skip(
                        first.timestamp,
                        module.name,
                        f"module_start_error:{type(exc).__name__}",
                        symbol=first.symbol,
                    )
                    continue
                self.active_modules.append(module)
        except BaseException as exc:
            self.abort(exc)
            raise
        if self.engine.performance.disable_cyclic_gc_in_hot_loop and self._gc_was_enabled:
            gc.disable()

    def process_bar(self, index: int, bar: Candle) -> None:
        if self._suspended:
            raise RuntimeError("runtime session is suspended")
        if index != self.bars_processed:
            raise ValueError(
                f"runtime candle index mismatch: expected {self.bars_processed}, got {index}"
            )
        if index >= self.source.total_bars:
            raise ValueError("prepared source yielded more candles than declared")
        first = self.source.first_candle
        engine = self.engine
        journal = self.journal
        _validate_stream_bar(
            bar,
            first=first,
            previous=self.previous_bar,
            expected=self.expected_interval,
            reject_gaps=engine.config.data.gap_policy == "reject",
        )
        journal.set_bar_context(index)
        self.clock.advance(bar.close_time)
        self.broker.process_bar(bar, index)
        self.order_event_cursor, self.trade_cursor = engine._dispatch_events(
            self.broker,
            self.order_event_cursor,
            self.trade_cursor,
            journal,
            self.active_modules,
            self.setup_registry,
            bar.symbol,
            features=self.feature_store,
            manifests=self.manifest_by_name,
            dependents=self.dependents_by_module,
            quarantined=self.quarantined_modules,
        )

        base_gap = (
            self.previous_bar is not None
            and self.expected_interval is not None
            and bar.timestamp - self.previous_bar.timestamp != self.expected_interval
        )
        store_gap = self.store.push(bar)
        data_gap = base_gap or store_gap
        if data_gap:
            journal.close_all_skip_ranges(bar.timestamp)
            removed = self.feature_store.invalidate_on_gap(symbol=bar.symbol)
            for feature in removed:
                journal.log_feature(bar.timestamp, feature, "gap_invalidated")
            journal.log_diagnostics(
                bar.timestamp,
                "kernel",
                {
                    "event": "data_gap_detected",
                    "base_index": index,
                    "timeframes_reset": bool(self.effective_timeframes),
                    "features_invalidated": len(removed),
                },
            )

        for feature in self.feature_store.expire(bar.close_time):
            journal.log_feature(bar.timestamp, feature, "expired")

        self.base_history.append(bar)
        if (
            self.base_trim_threshold is not None
            and len(self.base_history) > self.base_trim_threshold
        ):
            if self.max_base_history is None:
                raise RuntimeError("base history limit disappeared")
            self.base_history = self.base_history[-self.max_base_history :]
        base_start = (
            0
            if self.max_base_history is None
            else max(0, len(self.base_history) - self.max_base_history)
        )
        timeframe_snapshot = self.store.snapshot_view()
        base_snapshot = ReadOnlySeries(self.base_history, start=base_start)
        readiness_cache: dict[
            tuple[tuple[tuple[str, int], ...], tuple[str, ...]], ReadinessView
        ] = {}
        view_cache: dict[int, MarketView] = {}
        for module in self.active_modules:
            if module.name in self.quarantined_modules:
                continue
            manifest = self.manifest_by_name[module.name]
            readiness_key = (
                tuple(manifest.warmup_bars.items()),
                manifest.required_timeframes,
            )
            readiness = readiness_cache.get(readiness_key)
            if readiness is None:
                readiness = calculate_readiness(
                    base_timeframe=bar.timeframe,
                    base_bars=base_snapshot,
                    timeframe_bars=timeframe_snapshot,
                    warmup_bars=manifest.warmup_bars,
                    required_timeframes=manifest.required_timeframes,
                )
                readiness_cache[readiness_key] = readiness
            if manifest.skip_until_ready and not readiness.is_ready:
                journal.log_skip(
                    bar.timestamp,
                    module.name,
                    f"{ReasonCode.MODULE_NOT_READY.value}:"
                    + ",".join(readiness.missing_requirements),
                    symbol=bar.symbol,
                )
                continue

            view = view_cache.get(id(readiness))
            if view is None:
                view = engine._build_view(
                    base_bars=base_snapshot,
                    timeframe_bars=timeframe_snapshot,
                    index=index,
                    bar=bar,
                    broker=self.broker,
                    data_gap=data_gap,
                    feature_store=self.feature_store,
                    readiness=readiness,
                )
                view_cache[id(readiness)] = view
            try:
                decision = module.on_bar(view)
                if not isinstance(decision, ModuleDecision):
                    raise TypeError("on_bar must return ModuleDecision")
            except Exception as exc:
                journal.log_error(bar.timestamp, f"module:{module.name}", exc)
                if engine.config.strict_module_errors:
                    raise
                journal.log_skip(
                    bar.timestamp,
                    module.name,
                    f"module_runtime_error:{type(exc).__name__}",
                    symbol=bar.symbol,
                )
                engine._quarantine_modules(
                    initial_module=module.name,
                    reason=f"on_bar:{type(exc).__name__}",
                    timestamp=bar.timestamp,
                    symbol=bar.symbol,
                    journal=journal,
                    features=self.feature_store,
                    manifests=self.manifest_by_name,
                    dependents=self.dependents_by_module,
                    quarantined=self.quarantined_modules,
                )
                view_cache.clear()
                continue

            if decision.is_empty:
                journal.close_skip_range(module.name, bar.timestamp)
                continue
            if not decision.skips or _decision_has_substantial_action(decision):
                journal.close_skip_range(module.name, bar.timestamp)
            engine._apply_decision(
                decision=decision,
                module=module,
                manifest=manifest,
                bar=bar,
                index=index,
                broker=self.broker,
                journal=journal,
                features=self.feature_store,
                setups=self.setup_registry,
            )
            if decision.changes_view_state:
                view_cache.clear()
            self.order_event_cursor, self.trade_cursor = engine._dispatch_events(
                self.broker,
                self.order_event_cursor,
                self.trade_cursor,
                journal,
                self.active_modules,
                self.setup_registry,
                bar.symbol,
                features=self.feature_store,
                manifests=self.manifest_by_name,
                dependents=self.dependents_by_module,
                quarantined=self.quarantined_modules,
            )

        engine._append_equity(journal, self.broker, bar)
        self.bars_processed = index + 1
        if (
            self.bars_processed % self.guard_interval == 0
            or self.bars_processed == self.source.total_bars
        ):
            journal.observe_retention(
                base_candles=len(self.base_history),
                timeframe_candles=self.store.retention_counts(),
            )
            self.guard.check(journal.counters(self.bars_processed))
        self.reporter.maybe_report(journal, self.bars_processed)
        self.previous_bar = bar
        self.last_bar = bar

    def runtime_state(self) -> ScalableRuntimeState:
        if self.last_bar is None or self.bars_processed < 1:
            raise ValueError("cannot snapshot before the first processed candle")
        return ScalableRuntimeState(
            run_manifest=self.run_manifest,
            journal=self.journal,
            broker=self.broker,
            feature_store=self.feature_store,
            setup_registry=self.setup_registry,
            clock=self.clock,
            store=self.store,
            base_history=self.base_history,
            order_event_cursor=self.order_event_cursor,
            trade_cursor=self.trade_cursor,
            active_module_names=tuple(module.name for module in self.active_modules),
            quarantined_modules=set(self.quarantined_modules),
            previous_bar=self.previous_bar,
            last_bar=self.last_bar,
            bars_processed=self.bars_processed,
            random_state=random.getstate(),
        )

    def kernel_checkpoint(self) -> Any:
        if self.last_bar is None or self.bars_processed < 1:
            raise ValueError("cannot checkpoint before the first processed candle")
        return build_checkpoint(
            run_id=self.run_manifest.run_id,
            kernel_version=KERNEL_VERSION,
            created_at=self.last_bar.close_time,
            last_bar_index=self.bars_processed - 1,
            last_bar_time=self.last_bar.close_time,
            data_hash=self.run_manifest.data_hash,
            config_hash=self.run_manifest.config_hash,
            module_hash=self.run_manifest.module_hash,
            modules=self.active_modules,
        )

    def suspend(self) -> None:
        if self._suspended:
            return
        if (
            self.engine.performance.disable_cyclic_gc_in_hot_loop
            and self._gc_was_enabled
            and not gc.isenabled()
        ):
            gc.enable()
        self.journal.streaming_store.close()
        self._suspended = True

    def _finish_modules(self, primary_error: BaseException | None) -> list[Exception]:
        if self._modules_finished:
            return []
        self._modules_finished = True
        first = self.source.first_candle
        finish_timestamp = self.last_bar.timestamp if self.last_bar is not None else first.timestamp
        finish_errors: list[Exception] = []
        for module in reversed(self.active_modules):
            try:
                module.on_finish()
            except Exception as exc:
                if primary_error is None:
                    self.journal.log_error(
                        finish_timestamp,
                        f"module_finish:{module.name}",
                        exc,
                    )
                    self.journal.log_skip(
                        finish_timestamp,
                        module.name,
                        f"module_finish_error:{type(exc).__name__}",
                        symbol=first.symbol,
                    )
                finish_errors.append(exc)
        return finish_errors

    def abort(self, error: BaseException) -> None:
        if (
            self.engine.performance.disable_cyclic_gc_in_hot_loop
            and self._gc_was_enabled
            and not gc.isenabled()
        ):
            gc.enable()
        if isinstance(error, ResourceLimitExceeded):
            error.attach_counters(self.journal.counters(self.bars_processed))
        self._finish_modules(error)
        self.journal.streaming_store.cleanup()

    def finish(self) -> BacktestResult:
        if self._suspended:
            raise RuntimeError("cannot finish a suspended runtime session")
        if self.bars_processed != self.source.total_bars or self.last_bar is None:
            raise ValueError("prepared source candle count differs from validated metadata")
        last_bar = self.last_bar
        if (
            last_bar.timestamp != self.source.last_candle.timestamp
            or last_bar.close_time != self.source.last_candle.close_time
        ):
            raise ValueError("prepared source end boundary changed")
        primary_error: BaseException | None = None
        try:
            self.broker.cancel_all(last_bar.timestamp, ReasonCode.END_OF_DATA.value)
            if (
                self.engine.config.close_open_position_at_end
                and self.broker.portfolio.position is not None
            ):
                self.broker.close_position(
                    last_bar.timestamp,
                    last_bar.close,
                    ReasonCode.END_OF_DATA.value,
                    index=self.source.total_bars - 1,
                    liquidity="taker",
                    reference_price=last_bar.close,
                )
                previous_exposure = self.journal.equity[-1].position_open
                self.journal.equity[-1] = EquityPoint(
                    timestamp=last_bar.timestamp,
                    cash=self.broker.portfolio.cash,
                    unrealized_pnl=0.0,
                    equity=self.broker.portfolio.cash,
                    position_open=previous_exposure,
                )
            self.engine._dispatch_events(
                self.broker,
                self.order_event_cursor,
                self.trade_cursor,
                self.journal,
                self.active_modules,
                self.setup_registry,
                last_bar.symbol,
                features=self.feature_store,
                manifests=self.manifest_by_name,
                dependents=self.dependents_by_module,
                quarantined=self.quarantined_modules,
            )
            self.journal.setup_records = list(self.setup_registry.all())
            self.journal.close_all_skip_ranges(last_bar.timestamp)
            self.journal.emit_event(
                timestamp=last_bar.timestamp,
                event_type="run.finished",
                source="kernel",
                payload={"bars_processed": self.source.total_bars},
                symbol=last_bar.symbol,
            )
        except BaseException as exc:
            primary_error = exc
            if isinstance(exc, ResourceLimitExceeded):
                exc.attach_counters(self.journal.counters(self.bars_processed))
            raise
        finally:
            if (
                self.engine.performance.disable_cyclic_gc_in_hot_loop
                and self._gc_was_enabled
                and not gc.isenabled()
            ):
                gc.enable()
            finish_errors = self._finish_modules(primary_error)
            if primary_error is None:
                self.journal.finalize_observability()
            else:
                self.journal.streaming_store.cleanup()
            if finish_errors and self.engine.config.strict_module_errors and primary_error is None:
                self.journal.streaming_store.cleanup()
                raise ExceptionGroup("module finish failures", finish_errors)

        checkpoint = self.kernel_checkpoint()
        return BacktestResult(
            metrics=calculate_metrics(self.journal, self.engine.config.initial_cash),
            journal=self.journal,
            bars_processed=self.source.total_bars,
            modules=tuple(module.name for module in self.active_modules),
            run_manifest=self.run_manifest,
            module_manifests=tuple(
                self.manifest_by_name[module.name] for module in self.active_modules
            ),
            checkpoint=checkpoint,
        )


class ScalableBacktestEngine(BacktestEngine):
    """Core-compatible engine path with bounded data and observability state."""

    def __init__(self, *args: Any, performance: PerformanceConfig, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.performance = performance

    def run(self, candles: Iterable[Candle] | DataFeed) -> BacktestResult:
        bars = _validated_sequence(candles, self)
        source = _RunSource(
            candles=iter(bars),
            total_bars=len(bars),
            data_hash=incremental_candle_data_hash(bars),
            first_candle=bars[0],
            last_candle=bars[-1],
        )
        return self._run_source(source)

    def run_prepared(self, source: PreparedCandleStream) -> BacktestResult:
        return self._run_source(self._prepared_source(source))

    def create_prepared_session(self, source: PreparedCandleStream) -> ScalableRunSession:
        return ScalableRunSession(self, self._prepared_source(source))

    @staticmethod
    def _prepared_source(
        source: PreparedCandleStream,
        *,
        include_candles: bool = True,
    ) -> _RunSource:
        if not isinstance(source.total_bars, int) or isinstance(source.total_bars, bool):
            raise TypeError("prepared source total_bars must be int")
        if source.total_bars < 1:
            raise ValueError("prepared source total_bars must be positive")
        if not isinstance(source.first_candle, Candle) or not isinstance(
            source.last_candle,
            Candle,
        ):
            raise TypeError("prepared source boundaries must be Candle")
        return _RunSource(
            candles=source.iter_candles() if include_candles else (),
            total_bars=source.total_bars,
            data_hash=source.data_hash,
            first_candle=source.first_candle,
            last_candle=source.last_candle,
        )

    def _run_source(self, source: _RunSource) -> BacktestResult:
        session = ScalableRunSession(self, source)
        try:
            for index, bar in enumerate(source.candles):
                session.process_bar(index, bar)
            return session.finish()
        except BaseException as exc:
            if not session._modules_finished and not session._suspended:
                session.abort(exc)
            raise
