from __future__ import annotations

import copyreg
import gc
import hashlib
import json
import os
import pickle
import random
from collections import Counter, deque
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, cast

from modular_core.adapters import SimulationClock
from modular_core.audit import Journal, calculate_metrics, to_primitive
from modular_core.checkpoints import KernelCheckpoint, build_checkpoint
from modular_core.data import timeframe_to_seconds, validate_candles
from modular_core.engine import KERNEL_VERSION, BacktestEngine, BacktestResult
from modular_core.events import EventEnvelope
from modular_core.features import FeatureStore
from modular_core.manifest import DependencyPlan, ModuleManifest, parameters_for
from modular_core.models import Candle, EquityPoint, ReasonCode
from modular_core.module_api import ModuleDecision, StrategyModule
from modular_core.readiness import calculate_readiness
from modular_core.run_manifest import RunManifest, build_run_manifest
from modular_core.series import ReadOnlySeries
from modular_core.setups import SetupRegistry
from modular_core.timeframes import TimeframeStore

from .scalability.engine import (
    PreparedCandleStream,
    ScalableBacktestEngine,
    ScalableRunSession,
    ScalableRuntimeState,
)
from .scalability.events import StreamingEventStore


class ReplayRecoveryError(ValueError):
    """Raised when replay cannot reconstruct the exact checkpoint boundary."""


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    kernel: KernelCheckpoint
    bars_processed: int
    runtime_sha256: str
    event_sha256: str
    equity_sha256: str
    seed: int
    commit_sha: str | None
    recovery_mode: str = "deterministic_prefix_replay"
    snapshot_schema_version: str | None = None
    snapshot_bytes: int | None = None
    event_stream_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kernel": to_primitive(self.kernel),
            "bars_processed": self.bars_processed,
            "runtime_sha256": self.runtime_sha256,
            "event_sha256": self.event_sha256,
            "equity_sha256": self.equity_sha256,
            "seed": self.seed,
            "commit_sha": self.commit_sha,
            "recovery_mode": self.recovery_mode,
            "snapshot_schema_version": self.snapshot_schema_version,
            "snapshot_bytes": self.snapshot_bytes,
            "event_stream_bytes": self.event_stream_bytes,
        }


@dataclass(frozen=True, slots=True)
class ReplayRun:
    result: BacktestResult
    checkpoint: ReplayCheckpoint | None
    restart_verified: bool
    continuous_sha256: str | None = None
    restarted_sha256: str | None = None
    recovery_mode: str = "disabled"
    simulation_passes: int = 1
    source_traversals: int = 1
    source_rows_replayed: int = 0

    def evidence(self) -> dict[str, object]:
        return {
            "restart_verified": self.restart_verified,
            "recovery_mode": self.recovery_mode,
            "simulation_passes": self.simulation_passes,
            "source_traversals": self.source_traversals,
            "source_rows_replayed": self.source_rows_replayed,
            "continuous_sha256": self.continuous_sha256,
            "restarted_sha256": self.restarted_sha256,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint is not None else None,
        }


EngineFactory = Callable[[], BacktestEngine]


def _digest(value: object) -> str:
    payload = json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


RUNTIME_SNAPSHOT_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    schema_version: str
    kernel_version: str
    payload_path: Path
    payload_sha256: str
    payload_bytes: int
    event_stream_path: Path
    event_stream_sha256: str
    event_stream_bytes: int
    integrity_sha256: str

    def verify(self) -> None:
        if self.schema_version != RUNTIME_SNAPSHOT_SCHEMA_VERSION:
            raise ReplayRecoveryError(f"unsupported runtime snapshot schema: {self.schema_version}")
        if self.kernel_version != KERNEL_VERSION:
            raise ReplayRecoveryError(f"runtime snapshot kernel mismatch: {self.kernel_version}")
        if not self.event_stream_path.is_file():
            raise ReplayRecoveryError("runtime snapshot event stream is missing")
        if not self.payload_path.is_file():
            raise ReplayRecoveryError("runtime snapshot payload is missing")
        if self.payload_path.stat().st_size != self.payload_bytes:
            raise ReplayRecoveryError("runtime snapshot payload size changed")
        actual_payload = _file_sha256(self.payload_path)
        if actual_payload != self.payload_sha256:
            raise ReplayRecoveryError("runtime snapshot payload integrity verification failed")
        actual_size = self.event_stream_path.stat().st_size
        if actual_size != self.event_stream_bytes:
            raise ReplayRecoveryError("runtime snapshot event stream size changed")
        actual_event = _file_sha256(self.event_stream_path)
        if actual_event != self.event_stream_sha256:
            raise ReplayRecoveryError("runtime snapshot event stream integrity verification failed")
        actual_integrity = _snapshot_integrity(
            schema_version=self.schema_version,
            kernel_version=self.kernel_version,
            payload_sha256=self.payload_sha256,
            event_stream_sha256=self.event_stream_sha256,
            event_stream_bytes=self.event_stream_bytes,
        )
        if actual_integrity != self.integrity_sha256:
            raise ReplayRecoveryError("runtime snapshot envelope integrity verification failed")


ScalableEngineFactory = Callable[[], ScalableBacktestEngine]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_integrity(
    *,
    schema_version: str,
    kernel_version: str,
    payload_sha256: str,
    event_stream_sha256: str,
    event_stream_bytes: int,
) -> str:
    payload = json.dumps(
        {
            "schema_version": schema_version,
            "kernel_version": kernel_version,
            "payload_sha256": payload_sha256,
            "event_stream_sha256": event_stream_sha256,
            "event_stream_bytes": event_stream_bytes,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _restore_mapping_proxy(
    value: dict[object, object],
) -> MappingProxyType[object, object]:
    return MappingProxyType(value)


def _reduce_mapping_proxy(
    value: MappingProxyType[object, object],
) -> tuple[object, tuple[object, ...]]:
    return _restore_mapping_proxy, (dict(value),)


_RESTORE_EVENT_STREAM_PATH: ContextVar[Path | None] = ContextVar(
    "runtime_snapshot_event_stream_path", default=None
)


def _restore_streaming_event_store(
    retained_window: int,
    retained: tuple[EventEnvelope, ...],
    count: int,
    last_sequence: int,
    run_id: str | None,
    counts_by_type: dict[str, int],
    bytes_written: int,
) -> StreamingEventStore:
    path = _RESTORE_EVENT_STREAM_PATH.get()
    if path is None:
        raise pickle.UnpicklingError("event stream restore context is missing")
    if not path.is_file() or path.stat().st_size != bytes_written:
        raise pickle.UnpicklingError("event stream changed before runtime restore")
    store = StreamingEventStore.__new__(StreamingEventStore)
    store.path = path
    store._descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    store._retained = deque(retained, maxlen=retained_window)
    store._count = count
    store._last_sequence = last_sequence
    store._run_id = run_id
    store._counts_by_type = Counter(counts_by_type)
    store._bytes_written = bytes_written
    store._closed = False
    store._lock = RLock()
    return store


def _reduce_streaming_event_store(
    store: StreamingEventStore,
) -> tuple[object, tuple[object, ...]]:
    store.flush()
    retained_window = store._retained.maxlen
    if retained_window is None:
        raise RuntimeError("streaming event store retained window is not bounded")
    return (
        _restore_streaming_event_store,
        (
            retained_window,
            tuple(store._retained),
            store._count,
            store._last_sequence,
            store._run_id,
            dict(store._counts_by_type),
            store._bytes_written,
        ),
    )


class _RuntimePickler(pickle.Pickler):
    dispatch_table: dict[type[object], Any] = cast(
        dict[type[object], Any],
        copyreg.dispatch_table.copy(),
    )
    dispatch_table[MappingProxyType] = _reduce_mapping_proxy
    dispatch_table[StreamingEventStore] = _reduce_streaming_event_store


def _serialize_runtime(state: ScalableRuntimeState, path: Path) -> None:
    with path.open("wb") as handle:
        _RuntimePickler(handle, protocol=pickle.HIGHEST_PROTOCOL).dump(state)
        handle.flush()
        os.fsync(handle.fileno())


def _build_runtime_snapshot(session: ScalableRunSession) -> RuntimeSnapshot:
    store = session.journal.streaming_store
    store.flush()
    payload_path = store.path.with_name("runtime-snapshot-v1.pkl")
    _serialize_runtime(session.runtime_state(), payload_path)
    payload_bytes = payload_path.stat().st_size
    payload_sha256 = _file_sha256(payload_path)
    event_stream_bytes = store.path.stat().st_size
    event_stream_sha256 = _file_sha256(store.path)
    integrity_sha256 = _snapshot_integrity(
        schema_version=RUNTIME_SNAPSHOT_SCHEMA_VERSION,
        kernel_version=KERNEL_VERSION,
        payload_sha256=payload_sha256,
        event_stream_sha256=event_stream_sha256,
        event_stream_bytes=event_stream_bytes,
    )
    return RuntimeSnapshot(
        schema_version=RUNTIME_SNAPSHOT_SCHEMA_VERSION,
        kernel_version=KERNEL_VERSION,
        payload_path=payload_path,
        payload_sha256=payload_sha256,
        payload_bytes=payload_bytes,
        event_stream_path=store.path,
        event_stream_sha256=event_stream_sha256,
        event_stream_bytes=event_stream_bytes,
        integrity_sha256=integrity_sha256,
    )


def _load_runtime_snapshot(snapshot: RuntimeSnapshot) -> ScalableRuntimeState:
    snapshot.verify()
    token = _RESTORE_EVENT_STREAM_PATH.set(snapshot.event_stream_path)
    try:
        with snapshot.payload_path.open("rb") as handle:
            state = pickle.load(handle)
    except Exception as exc:
        raise ReplayRecoveryError("runtime snapshot deserialization failed") from exc
    finally:
        _RESTORE_EVENT_STREAM_PATH.reset(token)
    if not isinstance(state, ScalableRuntimeState):
        raise ReplayRecoveryError("runtime snapshot payload has an invalid root type")
    return state


def _equity_digest(values: Sequence[object]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(
            json.dumps(
                to_primitive(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def run_with_runtime_snapshot_restart(
    engine_factory: ScalableEngineFactory,
    source: PreparedCandleStream,
    *,
    restart_at_index: int,
    seed: int = 0,
    commit_sha: str | None = None,
) -> ReplayRun:
    """Run one trading pass with a verified direct runtime snapshot restart.

    The source is reopened and the validated prefix is skipped without invoking the
    broker or strategy modules. No candle collection is materialized and no prefix
    is simulated twice.
    """

    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be int")
    if not isinstance(restart_at_index, int) or isinstance(restart_at_index, bool):
        raise TypeError("restart_at_index must be int")
    if restart_at_index < 0 or restart_at_index >= source.total_bars - 1:
        raise ValueError("restart_at_index must leave at least one candle to continue")

    previous_random_state = random.getstate()
    first_session: ScalableRunSession | None = None
    restored_session: ScalableRunSession | None = None
    first_iterator: object | None = None
    snapshot: RuntimeSnapshot | None = None
    try:
        random.seed(seed)
        first_engine = engine_factory()
        if not isinstance(first_engine, ScalableBacktestEngine):
            raise TypeError("runtime snapshot factory must return ScalableBacktestEngine")
        first_source = first_engine._prepared_source(source)
        first_session = ScalableRunSession(first_engine, first_source)
        first_iterator = iter(first_source.candles)
        for index, bar in enumerate(first_iterator):
            first_session.process_bar(index, bar)
            if index == restart_at_index:
                break
        if first_session.bars_processed != restart_at_index + 1:
            raise ReplayRecoveryError("source ended before the requested checkpoint boundary")

        kernel = first_session.kernel_checkpoint()
        snapshot = _build_runtime_snapshot(first_session)
        checkpoint = ReplayCheckpoint(
            kernel=kernel,
            bars_processed=first_session.bars_processed,
            runtime_sha256=snapshot.payload_sha256,
            event_sha256=snapshot.event_stream_sha256,
            equity_sha256=_equity_digest(first_session.journal.equity),
            seed=seed,
            commit_sha=commit_sha,
            recovery_mode="runtime_snapshot_v1",
            snapshot_schema_version=snapshot.schema_version,
            snapshot_bytes=snapshot.payload_bytes,
            event_stream_bytes=snapshot.event_stream_bytes,
        )
        first_session.suspend()
        close = getattr(first_iterator, "close", None)
        if callable(close):
            close()
        first_iterator = None
        first_session = None
        del first_engine
        gc.collect()

        restored_engine = engine_factory()
        if not isinstance(restored_engine, ScalableBacktestEngine):
            raise TypeError("runtime snapshot factory must return ScalableBacktestEngine")
        state = _load_runtime_snapshot(snapshot)
        try:
            snapshot.payload_path.unlink()
        except FileNotFoundError:
            pass
        restored_source = restored_engine._prepared_source(
            source,
            include_candles=False,
        )
        restored_session = ScalableRunSession.restore(
            restored_engine,
            restored_source,
            state,
            module_states=checkpoint.kernel.module_states,
        )
        del state

        resume = getattr(source, "iter_candles_from", None)
        source_rows_replayed = 0
        if callable(resume):
            continuation = resume(checkpoint.bars_processed)
            minimum_first_timestamp = checkpoint.kernel.last_bar_time
            reject_gaps = restored_engine.config.data.gap_policy == "reject"
            for index, bar in enumerate(
                continuation,
                start=checkpoint.bars_processed,
            ):
                if index == checkpoint.bars_processed and (
                    bar.timestamp < minimum_first_timestamp
                    or (reject_gaps and bar.timestamp != minimum_first_timestamp)
                ):
                    raise ReplayRecoveryError(
                        "runtime snapshot source continuation boundary changed"
                    )
                restored_session.process_bar(index, bar)
        else:
            skipped_last: Candle | None = None
            for index, bar in enumerate(restored_source.candles):
                if index < checkpoint.bars_processed:
                    skipped_last = bar
                    source_rows_replayed += 1
                    continue
                if skipped_last is None:
                    raise ReplayRecoveryError("runtime snapshot prefix was not available")
                if index == checkpoint.bars_processed and (
                    skipped_last.close_time != checkpoint.kernel.last_bar_time
                ):
                    raise ReplayRecoveryError("runtime snapshot source boundary changed")
                restored_session.process_bar(index, bar)
        result = restored_session.finish()
        final_sha256 = _result_digest(result)
        return ReplayRun(
            result=result,
            checkpoint=checkpoint,
            restart_verified=True,
            continuous_sha256=None,
            restarted_sha256=final_sha256,
            recovery_mode="runtime_snapshot_v1",
            simulation_passes=1,
            source_traversals=2,
            source_rows_replayed=source_rows_replayed,
        )
    except BaseException as exc:
        if restored_session is not None and not restored_session._modules_finished:
            restored_session.abort(exc)
        elif first_session is not None and not first_session._modules_finished:
            first_session.abort(exc)
        elif snapshot is not None:
            for path in (snapshot.payload_path, snapshot.event_stream_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        raise
    finally:
        close = getattr(first_iterator, "close", None)
        if callable(close):
            close()
        random.setstate(previous_random_state)


class _ReplaySession:
    """Application-owned resumable session for Core 0.4.0.

    Core 0.4.0 exposes a complete deterministic event loop but not an incremental
    public session API. This adapter keeps orchestration in one place and delegates
    all strategy, execution, portfolio, timeframe and audit behavior to Core.
    Restart recovery deliberately reconstructs the boundary by replaying the
    validated prefix, then proves exact runtime equivalence before continuation.
    """

    def __init__(
        self,
        engine: BacktestEngine,
        bars: Sequence[Candle],
        *,
        run_manifest: RunManifest | None = None,
    ) -> None:
        if not bars:
            raise ValueError("replay session requires at least one candle")
        self.engine = engine
        self.bars = tuple(bars)
        self.plan: DependencyPlan = engine.module_registry.validate_and_plan(
            engine.modules,
            base_timeframe=self.bars[0].timeframe,
        )
        self.effective_timeframes = engine._resolve_timeframes(
            self.plan,
            self.bars[0].timeframe,
        )
        candidate_manifest = build_run_manifest(
            kernel_version=KERNEL_VERSION,
            created_at=self.bars[0].timestamp,
            bars=self.bars,
            config=engine.config,
            manifests=self.plan.manifests,
            module_parameters=tuple(parameters_for(module) for module in self.plan.modules),
        )
        if run_manifest is not None and candidate_manifest != run_manifest:
            raise ReplayRecoveryError("run manifest changed while restoring checkpoint")
        self.run_manifest = run_manifest or candidate_manifest
        self.journal = Journal(
            run_id=self.run_manifest.run_id,
            event_store=engine.event_store_factory(),
        )
        instrument = engine._resolve_instrument(self.bars[0])
        self.broker = engine.execution_factory(engine.config, self.journal, instrument)
        self.feature_store = FeatureStore()
        self.setup_registry = SetupRegistry()
        self.clock = SimulationClock()
        self.visible: list[Candle] = []
        expected_seconds = engine.config.data.expected_interval_seconds or timeframe_to_seconds(
            self.bars[0].timeframe
        )
        self.expected_seconds = expected_seconds
        self.store = TimeframeStore(
            self.effective_timeframes,
            expected_base_interval=(
                timedelta(seconds=expected_seconds) if expected_seconds is not None else None
            ),
        )
        self.order_event_cursor = 0
        self.trade_cursor = 0
        self.active_modules: list[StrategyModule] = []
        self.manifest_by_name: dict[str, ModuleManifest] = {
            manifest.name: manifest for manifest in self.plan.manifests
        }
        self.dependents_by_module = engine._module_dependents(self.plan)
        self.quarantined_modules: set[str] = set()
        self.next_index = 0
        self.finished = False
        self._modules_finished = False
        self._start()

    def _start(self) -> None:
        try:
            self.journal.emit_event(
                timestamp=self.bars[0].timestamp,
                event_type="run.started",
                source="kernel",
                payload={"run_manifest": to_primitive(self.run_manifest)},
                symbol=self.bars[0].symbol,
            )
            for module in self.plan.modules:
                try:
                    module.on_start()
                except Exception as exc:
                    self.journal.log_error(
                        self.bars[0].timestamp,
                        f"module_start:{module.name}",
                        exc,
                    )
                    if self.engine.config.strict_module_errors:
                        raise
                    continue
                self.active_modules.append(module)
        except BaseException:
            self._finish_modules()
            raise

    def _finish_modules(self) -> None:
        if self._modules_finished:
            return
        self._modules_finished = True
        for module in reversed(self.active_modules):
            try:
                module.on_finish()
            except Exception as exc:
                self.journal.log_error(
                    self.bars[-1].timestamp,
                    f"module_finish:{module.name}",
                    exc,
                )

    def abort(self) -> None:
        self._finish_modules()

    def process_until(self, last_index: int) -> None:
        if self.finished:
            raise RuntimeError("replay session is already finished")
        if last_index < self.next_index - 1 or last_index >= len(self.bars):
            raise ValueError("last_index is outside the remaining candle range")
        while self.next_index <= last_index:
            self._process_bar(self.next_index)
            self.next_index += 1

    def process_all(self) -> None:
        self.process_until(len(self.bars) - 1)

    def _process_bar(self, index: int) -> None:
        bar = self.bars[index]
        self.clock.advance(bar.close_time)
        self.broker.process_bar(bar, index)
        self.order_event_cursor, self.trade_cursor = self.engine._dispatch_events(
            self.broker,
            self.order_event_cursor,
            self.trade_cursor,
            self.journal,
            self.active_modules,
            self.setup_registry,
            bar.symbol,
            features=self.feature_store,
            manifests=self.manifest_by_name,
            dependents=self.dependents_by_module,
            quarantined=self.quarantined_modules,
        )

        base_gap = False
        if self.visible and self.expected_seconds is not None:
            base_gap = bar.timestamp - self.visible[-1].timestamp != timedelta(
                seconds=self.expected_seconds
            )
        self.visible.append(bar)
        store_gap = self.store.push(bar)
        data_gap = base_gap or store_gap
        if data_gap:
            removed = self.feature_store.invalidate_on_gap(symbol=bar.symbol)
            for feature in removed:
                self.journal.log_feature(bar.timestamp, feature, "gap_invalidated")
            self.journal.log_diagnostics(
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
            self.journal.log_feature(bar.timestamp, feature, "expired")

        timeframe_snapshot = MappingProxyType(self.store.snapshot())
        base_start = 0
        if self.engine.config.data.max_base_history is not None:
            base_start = max(
                0,
                len(self.visible) - self.engine.config.data.max_base_history,
            )
        base_snapshot = ReadOnlySeries(
            self.visible,
            len(self.visible),
            start=base_start,
        )
        for module in self.active_modules:
            if module.name in self.quarantined_modules:
                continue
            manifest = self.manifest_by_name[module.name]
            readiness = calculate_readiness(
                base_timeframe=bar.timeframe,
                base_bars=base_snapshot,
                timeframe_bars=timeframe_snapshot,
                warmup_bars=manifest.warmup_bars,
                required_timeframes=manifest.required_timeframes,
            )
            if manifest.skip_until_ready and not readiness.is_ready:
                self.journal.log_skip(
                    bar.timestamp,
                    module.name,
                    f"{ReasonCode.MODULE_NOT_READY.value}:"
                    + ",".join(readiness.missing_requirements),
                    symbol=bar.symbol,
                )
                continue

            view = self.engine._build_view(
                base_bars=base_snapshot,
                timeframe_bars=timeframe_snapshot,
                index=index,
                bar=bar,
                broker=self.broker,
                data_gap=data_gap,
                feature_store=self.feature_store,
                readiness=readiness,
            )
            try:
                decision = module.on_bar(view)
                if not isinstance(decision, ModuleDecision):
                    raise TypeError("on_bar must return ModuleDecision")
            except Exception as exc:
                self.journal.log_error(bar.timestamp, f"module:{module.name}", exc)
                if self.engine.config.strict_module_errors:
                    raise
                self.engine._quarantine_modules(
                    initial_module=module.name,
                    reason=f"on_bar:{type(exc).__name__}",
                    timestamp=bar.timestamp,
                    symbol=bar.symbol,
                    journal=self.journal,
                    features=self.feature_store,
                    manifests=self.manifest_by_name,
                    dependents=self.dependents_by_module,
                    quarantined=self.quarantined_modules,
                )
                continue

            self.engine._apply_decision(
                decision=decision,
                module=module,
                manifest=manifest,
                bar=bar,
                index=index,
                broker=self.broker,
                journal=self.journal,
                features=self.feature_store,
                setups=self.setup_registry,
            )
            self.order_event_cursor, self.trade_cursor = self.engine._dispatch_events(
                self.broker,
                self.order_event_cursor,
                self.trade_cursor,
                self.journal,
                self.active_modules,
                self.setup_registry,
                bar.symbol,
                features=self.feature_store,
                manifests=self.manifest_by_name,
                dependents=self.dependents_by_module,
                quarantined=self.quarantined_modules,
            )

        self.engine._append_equity(self.journal, self.broker, bar)

    def checkpoint(
        self,
        *,
        seed: int,
        commit_sha: str | None = None,
    ) -> ReplayCheckpoint:
        if self.next_index <= 0:
            raise ValueError("cannot checkpoint before the first processed candle")
        last_index = self.next_index - 1
        last = self.bars[last_index]
        kernel = build_checkpoint(
            run_id=self.run_manifest.run_id,
            kernel_version=KERNEL_VERSION,
            created_at=last.close_time,
            last_bar_index=last_index,
            last_bar_time=last.close_time,
            data_hash=self.run_manifest.data_hash,
            config_hash=self.run_manifest.config_hash,
            module_hash=self.run_manifest.module_hash,
            modules=self.active_modules,
        )
        runtime_payload = self._runtime_payload(kernel)
        return ReplayCheckpoint(
            kernel=kernel,
            bars_processed=self.next_index,
            runtime_sha256=_digest(runtime_payload),
            event_sha256=_digest(self.journal.events),
            equity_sha256=_digest(self.journal.equity),
            seed=seed,
            commit_sha=commit_sha,
        )

    def verify_checkpoint(self, expected: ReplayCheckpoint) -> None:
        actual = self.checkpoint(seed=expected.seed, commit_sha=expected.commit_sha)
        if actual != expected:
            raise ReplayRecoveryError(
                "replayed runtime does not match the persisted checkpoint boundary"
            )

    def _runtime_payload(self, kernel: KernelCheckpoint) -> dict[str, object]:
        aggregators: dict[str, object] = {}
        for name, aggregator in self.store._aggregators.items():
            aggregators[name] = {
                "buffer": tuple(aggregator._buffer),
                "last_timestamp": aggregator._last_timestamp,
                "active_bucket": aggregator._active_bucket,
                "gaps_detected": aggregator.gaps_detected,
            }
        return {
            "kernel": kernel,
            "clock": self.clock.now,
            "visible": tuple(self.visible),
            "timeframes": {name: tuple(series) for name, series in self.store.snapshot().items()},
            "aggregators": aggregators,
            "portfolio": self.broker.portfolio,
            "orders": tuple(self.broker.orders),
            "pending_order_ids": tuple(order.id for order in self.broker.pending_orders),
            "used_client_order_ids": tuple(sorted(self.broker._used_client_order_ids)),
            "broker_sequences": {
                "order": self.broker._order_sequence,
                "position": self.broker._position_sequence,
                "fill": self.broker._fill_sequence,
            },
            "pending_close": self.broker.pending_close,
            "features": self.feature_store.all(),
            "setups": self.setup_registry.all(),
            "quarantined_modules": tuple(sorted(self.quarantined_modules)),
            "journal": {
                "events": self.journal.events,
                "orders": tuple(self.journal.orders),
                "fills": tuple(self.journal.fills),
                "trades": tuple(self.journal.trades),
                "equity": tuple(self.journal.equity),
                "setups": tuple(self.journal.setups),
                "setup_records": tuple(self.journal.setup_records),
                "features": tuple(self.journal.features),
                "signals": tuple(self.journal.signals),
                "skips": tuple(self.journal.skips),
                "diagnostics": tuple(self.journal.diagnostics),
                "errors": tuple(self.journal.errors),
                "event_sequence": self.journal._event_sequence,
                "last_event_by_entity": dict(self.journal._last_event_by_entity),
            },
            "cursors": {
                "next_index": self.next_index,
                "order_event": self.order_event_cursor,
                "trade": self.trade_cursor,
            },
        }

    def finish(self) -> BacktestResult:
        if self.finished:
            raise RuntimeError("replay session is already finished")
        if self.next_index != len(self.bars):
            raise RuntimeError("cannot finish before all candles are processed")
        self.finished = True
        last = self.bars[-1]
        try:
            self.broker.cancel_all(last.timestamp, ReasonCode.END_OF_DATA.value)
            if (
                self.engine.config.close_open_position_at_end
                and self.broker.portfolio.position is not None
            ):
                self.broker.close_position(
                    last.timestamp,
                    last.close,
                    ReasonCode.END_OF_DATA.value,
                    index=len(self.bars) - 1,
                    liquidity="taker",
                    reference_price=last.close,
                )
                previous_exposure = self.journal.equity[-1].position_open
                self.journal.equity[-1] = EquityPoint(
                    timestamp=last.timestamp,
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
                last.symbol,
                features=self.feature_store,
                manifests=self.manifest_by_name,
                dependents=self.dependents_by_module,
                quarantined=self.quarantined_modules,
            )
            self.journal.setup_records = list(self.setup_registry.all())
            self.journal.emit_event(
                timestamp=last.timestamp,
                event_type="run.finished",
                source="kernel",
                payload={"bars_processed": len(self.bars)},
                symbol=last.symbol,
            )
        finally:
            self._finish_modules()

        checkpoint = build_checkpoint(
            run_id=self.run_manifest.run_id,
            kernel_version=KERNEL_VERSION,
            created_at=last.close_time,
            last_bar_index=len(self.bars) - 1,
            last_bar_time=last.close_time,
            data_hash=self.run_manifest.data_hash,
            config_hash=self.run_manifest.config_hash,
            module_hash=self.run_manifest.module_hash,
            modules=self.active_modules,
        )
        return BacktestResult(
            metrics=calculate_metrics(self.journal, self.engine.config.initial_cash),
            journal=self.journal,
            bars_processed=len(self.bars),
            modules=tuple(module.name for module in self.active_modules),
            run_manifest=self.run_manifest,
            module_manifests=tuple(
                self.manifest_by_name[module.name] for module in self.active_modules
            ),
            checkpoint=checkpoint,
        )


def _update_result_digest_value(
    digest: hashlib._Hash,
    name: str,
    value: object,
) -> None:
    payload = json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    name_bytes = name.encode("utf-8")
    digest.update(len(name_bytes).to_bytes(4, "big"))
    digest.update(name_bytes)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _update_result_digest_sequence(
    digest: hashlib._Hash,
    name: str,
    values: Sequence[object],
) -> None:
    name_bytes = name.encode("utf-8")
    digest.update(len(name_bytes).to_bytes(4, "big"))
    digest.update(name_bytes)
    digest.update(len(values).to_bytes(8, "big"))
    for value in values:
        payload = json.dumps(
            to_primitive(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)


def _result_digest(result: BacktestResult) -> str:
    """Hash exact result evidence without materializing complete projections."""

    digest = hashlib.sha256()
    for name, value in (
        ("metrics", result.metrics),
        ("bars_processed", result.bars_processed),
        ("modules", result.modules),
        ("run_manifest", result.run_manifest),
        ("module_manifests", result.module_manifests),
        ("checkpoint", result.checkpoint),
    ):
        _update_result_digest_value(digest, name, value)

    streaming_store = getattr(result.journal, "streaming_store", None)
    if isinstance(streaming_store, StreamingEventStore):
        streaming_store.flush()
        _update_result_digest_value(
            digest,
            "event_stream",
            {
                "count": streaming_store.count,
                "bytes": streaming_store.bytes_written,
                "sha256": _file_sha256(streaming_store.path),
            },
        )
    else:
        _update_result_digest_sequence(digest, "events", result.journal.events)

    for name in (
        "orders",
        "fills",
        "trades",
        "equity",
        "setups",
        "setup_records",
        "features",
        "signals",
        "skips",
        "diagnostics",
        "errors",
    ):
        values = cast(Sequence[object], getattr(result.journal, name))
        _update_result_digest_sequence(digest, name, values)
    return digest.hexdigest()


def run_with_replay_restart(
    engine_factory: EngineFactory,
    candles: Sequence[Candle],
    *,
    restart_at_index: int | None = None,
    seed: int = 0,
    commit_sha: str | None = None,
) -> ReplayRun:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be int")

    previous_random_state = random.getstate()
    first_session: _ReplaySession | None = None
    restored_session: _ReplaySession | None = None
    try:
        random.seed(seed)
        first_engine = engine_factory()
        bars = tuple(validate_candles(iter(candles), first_engine.config.data))
        if restart_at_index is None:
            first_session = _ReplaySession(first_engine, bars)
            first_session.process_all()
            return ReplayRun(first_session.finish(), None, False)

        if restart_at_index < 0 or restart_at_index >= len(bars) - 1:
            raise ValueError("restart_at_index must leave at least one candle to continue")

        first_session = _ReplaySession(first_engine, bars)
        first_session.process_until(restart_at_index)
        checkpoint = first_session.checkpoint(seed=seed, commit_sha=commit_sha)
        first_session.process_all()
        continuous_result = first_session.finish()
        run_manifest = first_session.run_manifest
        continuous_sha256 = _result_digest(continuous_result)

        # The continuous result can contain a full journal, equity curve, and event
        # projections. Release that graph before constructing the restarted session
        # so checkpoint verification does not retain two complete simulations at
        # once. A deliberate phase-boundary collection is safe here because the hot
        # loop has finished and no object from the first runtime is part of recovery.
        first_session.abort()
        first_session = None
        del continuous_result
        del first_engine
        gc.collect()

        random.seed(seed)
        restored_engine = engine_factory()
        restored_session = _ReplaySession(
            restored_engine,
            bars,
            run_manifest=run_manifest,
        )
        restored_session.process_until(restart_at_index)
        restored_session.verify_checkpoint(checkpoint)
        restored_session.process_all()
        restarted_result = restored_session.finish()

        restarted_sha256 = _result_digest(restarted_result)
        if continuous_sha256 != restarted_sha256:
            raise ReplayRecoveryError(
                "continuous and restarted backtests produced different deterministic results"
            )
        return ReplayRun(
            result=restarted_result,
            checkpoint=checkpoint,
            restart_verified=True,
            continuous_sha256=continuous_sha256,
            restarted_sha256=restarted_sha256,
        )
    finally:
        if restored_session is not None:
            restored_session.abort()
        if first_session is not None:
            first_session.abort()
        random.setstate(previous_random_state)
