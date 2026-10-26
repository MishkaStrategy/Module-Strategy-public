from __future__ import annotations

import gc
import hashlib
import json
import random
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from modular_core.audit import Journal, to_primitive
from modular_core.checkpoints import KernelCheckpoint, build_checkpoint
from modular_core.engine import KERNEL_VERSION, BacktestEngine, BacktestResult
from modular_core.execution import BrokerSimulator
from modular_core.features import FeatureStore
from modular_core.models import Candle
from modular_core.module_api import MarketView
from modular_core.readiness import ReadinessView

from .registered_data import (
    RegisteredDataError,
    RegisteredSourcePosition,
    StreamingRegisteredCandleIterator,
    StreamingRegisteredCandleSource,
    open_streaming_registered_candles,
)
from .scalability import ScalableBacktestEngine, ScalableJournal
from .scalability.engine import PreparedCandleStream
from .streaming_data import PreparedCandleSource


class RegisteredStreamingRecoveryError(ValueError):
    """Raised when bounded registered replay cannot verify a checkpoint boundary."""


@dataclass(frozen=True, slots=True)
class RegisteredStreamingReplayCheckpoint:
    """Canonical evidence for a committed bar and the next physical source row."""

    kernel: KernelCheckpoint
    bars_processed: int
    source_position: RegisteredSourcePosition
    runtime_sha256: str
    event_sha256: str
    equity_sha256: str
    seed: int
    commit_sha: str | None
    schema_version: str = "1"
    recovery_mode: str = "bounded_deterministic_prefix_replay"

    def to_dict(self) -> dict[str, object]:
        return {
            "kernel": to_primitive(self.kernel),
            "bars_processed": self.bars_processed,
            "source_position": self.source_position.to_dict(),
            "runtime_sha256": self.runtime_sha256,
            "event_sha256": self.event_sha256,
            "equity_sha256": self.equity_sha256,
            "seed": self.seed,
            "commit_sha": self.commit_sha,
            "schema_version": self.schema_version,
            "recovery_mode": self.recovery_mode,
        }


@dataclass(frozen=True, slots=True)
class RegisteredStreamingReplayRun:
    result: BacktestResult
    checkpoint: RegisteredStreamingReplayCheckpoint
    restart_verified: bool
    continuous_sha256: str
    restarted_sha256: str
    continuous_source_open_count: int
    restarted_source_open_count: int

    @property
    def recovery_mode(self) -> str:
        return self.checkpoint.recovery_mode

    @property
    def simulation_passes(self) -> int:
        return 2

    @property
    def source_traversals(self) -> int:
        return self.continuous_source_open_count + self.restarted_source_open_count

    @property
    def source_rows_replayed(self) -> int:
        return self.checkpoint.bars_processed

    def evidence(self) -> dict[str, object]:
        return {
            "restart_verified": self.restart_verified,
            "recovery_mode": self.recovery_mode,
            "simulation_passes": self.simulation_passes,
            "source_traversals": self.source_traversals,
            "source_rows_replayed": self.source_rows_replayed,
            "continuous_sha256": self.continuous_sha256,
            "restarted_sha256": self.restarted_sha256,
            "continuous_source_open_count": self.continuous_source_open_count,
            "restarted_source_open_count": self.restarted_source_open_count,
            "checkpoint": self.checkpoint.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _RuntimeBoundary:
    kernel: KernelCheckpoint
    bars_processed: int
    source_position: RegisteredSourcePosition
    runtime_sha256: str
    event_sha256: str
    equity_sha256: str


EngineFactory = Callable[[], ScalableBacktestEngine]
BoundaryObserver = Callable[[_RuntimeBoundary], None]
PositionProvider = Callable[[], RegisteredSourcePosition]


def _digest(value: object) -> str:
    payload = json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run_manifest_payload(journal: ScalableJournal) -> Mapping[str, object]:
    journal.streaming_store.flush()
    try:
        with journal.streaming_store.path.open("r", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError) as exc:
        raise RegisteredStreamingRecoveryError(
            "cannot read the canonical run.started event at checkpoint boundary"
        ) from exc
    if not isinstance(first, dict) or first.get("event_type") != "run.started":
        raise RegisteredStreamingRecoveryError(
            "event stream does not begin with the canonical run.started event"
        )
    payload = first.get("payload")
    if not isinstance(payload, dict):
        raise RegisteredStreamingRecoveryError("run.started payload is invalid")
    manifest = payload.get("run_manifest")
    if not isinstance(manifest, dict):
        raise RegisteredStreamingRecoveryError("run.started event has no run manifest")
    return cast(Mapping[str, object], manifest)


def _required_manifest_text(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise RegisteredStreamingRecoveryError(f"run manifest field {key!r} is invalid")
    return value


class _ObservedScalableBacktestEngine(ScalableBacktestEngine):
    """Scalable engine instrumentation without changing the PR #287 hot loop."""

    _recovery_target: int
    _recovery_observer: BoundaryObserver
    _position_provider: PositionProvider
    _committed_bars: int
    _last_base_bars: Sequence[Candle] | None
    _last_timeframe_bars: Mapping[str, Sequence[Candle]] | None
    _last_feature_store: FeatureStore | None

    def configure_recovery_observer(
        self,
        *,
        target_bars: int,
        observer: BoundaryObserver,
        position_provider: PositionProvider,
    ) -> None:
        self._recovery_target = target_bars
        self._recovery_observer = observer
        self._position_provider = position_provider
        self._committed_bars = 0
        self._last_base_bars = None
        self._last_timeframe_bars = None
        self._last_feature_store = None

    def run_prepared(self, source: PreparedCandleStream) -> BacktestResult:
        token = _ACTIVE_RECOVERY_ENGINE.set(self)
        try:
            return super().run_prepared(source)
        finally:
            _ACTIVE_RECOVERY_ENGINE.reset(token)

    def _build_view(
        self,
        *,
        base_bars: Sequence[Candle],
        timeframe_bars: Mapping[str, Sequence[Candle]],
        index: int,
        bar: Candle,
        broker: BrokerSimulator,
        data_gap: bool,
        feature_store: FeatureStore,
        readiness: ReadinessView | None,
    ) -> MarketView:
        self._last_base_bars = base_bars
        self._last_timeframe_bars = timeframe_bars
        self._last_feature_store = feature_store
        return super()._build_view(
            base_bars=base_bars,
            timeframe_bars=timeframe_bars,
            index=index,
            bar=bar,
            broker=broker,
            data_gap=data_gap,
            feature_store=feature_store,
            readiness=readiness,
        )

    @staticmethod
    def _append_equity(
        journal: Journal,
        broker: BrokerSimulator,
        bar: Candle,
    ) -> None:
        BacktestEngine._append_equity(journal, broker, bar)
        active = _ACTIVE_RECOVERY_ENGINE.get()
        if active is not None:
            active._after_committed_bar(journal, broker, bar)

    def _after_committed_bar(
        self,
        journal: Journal,
        broker: BrokerSimulator,
        bar: Candle,
    ) -> None:
        self._committed_bars += 1
        if self._committed_bars != self._recovery_target:
            return
        if not isinstance(journal, ScalableJournal):
            raise RegisteredStreamingRecoveryError(
                "registered streaming recovery requires ScalableJournal"
            )
        self._recovery_observer(self._capture_boundary(journal, broker, bar))

    def _capture_boundary(
        self,
        journal: ScalableJournal,
        broker: BrokerSimulator,
        bar: Candle,
    ) -> _RuntimeBoundary:
        manifest = _run_manifest_payload(journal)
        kernel = build_checkpoint(
            run_id=_required_manifest_text(manifest, "run_id"),
            kernel_version=KERNEL_VERSION,
            created_at=bar.close_time,
            last_bar_index=self._committed_bars - 1,
            last_bar_time=bar.close_time,
            data_hash=_required_manifest_text(manifest, "data_hash"),
            config_hash=_required_manifest_text(manifest, "config_hash"),
            module_hash=_required_manifest_text(manifest, "module_hash"),
            modules=self.modules,
        )
        source_position = self._position_provider()
        journal.streaming_store.flush()
        event_sha256 = _file_sha256(journal.streaming_store.path)
        equity_sha256 = _digest(tuple(journal.equity))
        runtime_payload = {
            "kernel": kernel,
            "bars_processed": self._committed_bars,
            "source_position": source_position,
            "base_bars": tuple(self._last_base_bars or ()),
            "timeframe_bars": {
                name: tuple(series) for name, series in (self._last_timeframe_bars or {}).items()
            },
            "features": (
                self._last_feature_store.all() if self._last_feature_store is not None else ()
            ),
            "portfolio": broker.portfolio,
            "orders": tuple(broker.orders),
            "pending_order_ids": tuple(order.id for order in broker.pending_orders),
            "used_client_order_ids": tuple(sorted(broker._used_client_order_ids)),
            "broker_sequences": {
                "order": broker._order_sequence,
                "position": broker._position_sequence,
                "fill": broker._fill_sequence,
            },
            "pending_close": broker.pending_close,
            "journal": {
                "event_sha256": event_sha256,
                "event_count": journal.event_count,
                "event_sequence": journal._event_sequence,
                "last_event_by_entity": dict(journal._last_event_by_entity),
                "active_skip_ranges": dict(journal._active_skip_ranges),
                "orders": tuple(journal.orders),
                "fills": tuple(journal.fills),
                "trades": tuple(journal.trades),
                "equity": tuple(journal.equity),
                "retained_equity": tuple(journal.retained_equity),
                "setups": tuple(journal.setups),
                "setup_records": tuple(journal.setup_records),
                "features": tuple(journal.features),
                "signals": tuple(journal.signals),
                "skips": tuple(journal.skips),
                "diagnostics": tuple(journal.diagnostics),
                "errors": tuple(journal.errors),
            },
        }
        return _RuntimeBoundary(
            kernel=kernel,
            bars_processed=self._committed_bars,
            source_position=source_position,
            runtime_sha256=_digest(runtime_payload),
            event_sha256=event_sha256,
            equity_sha256=equity_sha256,
        )


_ACTIVE_RECOVERY_ENGINE: ContextVar[_ObservedScalableBacktestEngine | None] = ContextVar(
    "registered_streaming_recovery_engine",
    default=None,
)


def _instrument_engine(
    engine: ScalableBacktestEngine,
    *,
    target_bars: int,
    observer: BoundaryObserver,
    position_provider: PositionProvider,
) -> _ObservedScalableBacktestEngine:
    if not isinstance(engine, ScalableBacktestEngine):
        raise TypeError("engine_factory must return ScalableBacktestEngine")
    observed = _ObservedScalableBacktestEngine.__new__(_ObservedScalableBacktestEngine)
    observed.__dict__.update(engine.__dict__)
    observed.configure_recovery_observer(
        target_bars=target_bars,
        observer=observer,
        position_provider=position_provider,
    )
    return observed


def _validate_source_identity(
    prepared: PreparedCandleSource,
    source: StreamingRegisteredCandleSource,
) -> None:
    validation = prepared.validation
    if (
        validation.dataset_id != source.dataset_id
        or validation.dataset_version != source.dataset_version
        or validation.dataset_hash != source.dataset_hash
        or validation.partition_count != source.partition_count
    ):
        raise RegisteredStreamingRecoveryError("registered dataset identity changed before replay")
    if source.symbol != prepared.first_candle.symbol:
        raise RegisteredStreamingRecoveryError("registered replay symbol changed")
    if source.timeframe != prepared.first_candle.timeframe:
        raise RegisteredStreamingRecoveryError("registered replay timeframe changed")


class _RegisteredReplayIterator(Iterator[Candle]):
    def __init__(
        self,
        prepared: PreparedCandleSource,
        checkpoint: RegisteredStreamingReplayCheckpoint | None,
    ) -> None:
        batch_size = prepared.stream_batch_size
        if batch_size is None:
            raise RegisteredStreamingRecoveryError(
                "streaming replay source has no resolved batch size"
            )
        try:
            self.source = open_streaming_registered_candles(
                prepared.config,
                batch_size=batch_size,
            )
        except RegisteredDataError as exc:
            raise RegisteredStreamingRecoveryError(str(exc)) from exc
        _validate_source_identity(prepared, self.source)
        self._checkpoint = checkpoint
        self._current: StreamingRegisteredCandleIterator = self.source.open()
        self._count = 0
        self._switched = checkpoint is None
        self._closed = False

    def __iter__(self) -> _RegisteredReplayIterator:
        return self

    def __next__(self) -> Candle:
        if self._closed:
            raise StopIteration
        checkpoint = self._checkpoint
        if checkpoint is not None and not self._switched:
            if self._count == checkpoint.bars_processed:
                observed = self._current.checkpoint_position()
                if observed != checkpoint.source_position:
                    raise RegisteredStreamingRecoveryError(
                        "replayed prefix reached a different physical source position"
                    )
                self._current.close()
                self._current = self.source.open(checkpoint.source_position)
                self._switched = True
        try:
            candle = next(self._current)
        except StopIteration:
            self.close()
            if checkpoint is not None and not self._switched:
                raise RegisteredStreamingRecoveryError(
                    "registered source ended before the checkpoint boundary"
                )
            raise
        self._count += 1
        return candle

    def checkpoint_position(self) -> RegisteredSourcePosition:
        return self._current.checkpoint_position()

    @property
    def source_open_count(self) -> int:
        return self.source.source_open_count

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._current.close()


@dataclass(slots=True)
class _ReplayPreparedSource:
    prepared: PreparedCandleSource
    checkpoint: RegisteredStreamingReplayCheckpoint | None = None
    data_hash: str = field(init=False)
    first_candle: Candle = field(init=False)
    last_candle: Candle = field(init=False)
    total_bars: int = field(init=False)
    _iterator: _RegisteredReplayIterator | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.data_hash = self.prepared.data_hash
        self.first_candle = self.prepared.first_candle
        self.last_candle = self.prepared.last_candle
        self.total_bars = self.prepared.total_bars

    def iter_candles(self) -> Iterator[Candle]:
        if self._iterator is not None:
            raise RegisteredStreamingRecoveryError(
                "registered replay source may be opened only once per simulation"
            )
        self._iterator = _RegisteredReplayIterator(self.prepared, self.checkpoint)
        return self._iterator

    def checkpoint_position(self) -> RegisteredSourcePosition:
        if self._iterator is None:
            raise RegisteredStreamingRecoveryError("registered replay source is not open")
        return self._iterator.checkpoint_position()

    @property
    def source_open_count(self) -> int:
        if self._iterator is None:
            return 0
        return self._iterator.source_open_count

    def close(self) -> None:
        if self._iterator is not None:
            self._iterator.close()


def _result_digest(result: BacktestResult) -> str:
    journal = result.journal
    event_sha256: str | None = None
    event_count: int | None = None
    if isinstance(journal, ScalableJournal):
        journal.streaming_store.flush()
        event_sha256 = _file_sha256(journal.streaming_store.path)
        event_count = journal.event_count
    return _digest(
        {
            "metrics": result.metrics,
            "bars_processed": result.bars_processed,
            "modules": result.modules,
            "run_manifest": result.run_manifest,
            "module_manifests": result.module_manifests,
            "checkpoint": result.checkpoint,
            "event_sha256": event_sha256,
            "event_count": event_count,
            "journal": {
                "orders": tuple(journal.orders),
                "fills": tuple(journal.fills),
                "trades": tuple(journal.trades),
                "equity": tuple(journal.equity),
                "setups": tuple(journal.setups),
                "setup_records": tuple(journal.setup_records),
                "features": tuple(journal.features),
                "signals": tuple(journal.signals),
                "skips": tuple(journal.skips),
                "diagnostics": tuple(journal.diagnostics),
                "errors": tuple(journal.errors),
            },
        }
    )


def _cleanup_result(result: BacktestResult | None) -> None:
    if result is not None and isinstance(result.journal, ScalableJournal):
        result.journal.streaming_store.cleanup()


def run_with_registered_streaming_replay_restart(
    engine_factory: EngineFactory,
    source: PreparedCandleSource,
    *,
    restart_at_index: int,
    seed: int = 0,
    commit_sha: str | None = None,
) -> RegisteredStreamingReplayRun:
    """Verify bounded prefix replay and reopen the source at the persisted cursor.

    This is deterministic prefix replay, not direct in-memory state restoration. The
    restarted simulation replays the prefix to reconstruct runtime state, verifies the
    canonical boundary digest, closes that prefix reader, and opens a new Parquet
    iterator at the persisted next-row position before continuing.
    """

    if source.source_mode != "streaming":
        raise RegisteredStreamingRecoveryError(
            "registered streaming recovery requires source_mode=streaming"
        )
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be int")
    if (
        not isinstance(restart_at_index, int)
        or isinstance(restart_at_index, bool)
        or restart_at_index < 0
        or restart_at_index >= source.total_bars - 1
    ):
        raise ValueError("restart_at_index must leave at least one candle to continue")

    target_bars = restart_at_index + 1
    previous_random_state = random.getstate()
    continuous_result: BacktestResult | None = None
    restarted_result: BacktestResult | None = None
    first_source = _ReplayPreparedSource(source)
    checkpoint: RegisteredStreamingReplayCheckpoint | None = None
    continuous_sha256 = ""
    first_open_count = 0
    second_source: _ReplayPreparedSource | None = None

    try:
        random.seed(seed)

        def capture_first(boundary: _RuntimeBoundary) -> None:
            nonlocal checkpoint
            if checkpoint is not None:
                raise RegisteredStreamingRecoveryError(
                    "checkpoint boundary was captured more than once"
                )
            checkpoint = RegisteredStreamingReplayCheckpoint(
                kernel=boundary.kernel,
                bars_processed=boundary.bars_processed,
                source_position=boundary.source_position,
                runtime_sha256=boundary.runtime_sha256,
                event_sha256=boundary.event_sha256,
                equity_sha256=boundary.equity_sha256,
                seed=seed,
                commit_sha=commit_sha,
            )

        first_engine = _instrument_engine(
            engine_factory(),
            target_bars=target_bars,
            observer=capture_first,
            position_provider=first_source.checkpoint_position,
        )
        continuous_result = first_engine.run_prepared(first_source)
        if checkpoint is None:
            raise RegisteredStreamingRecoveryError("checkpoint boundary was not reached")
        continuous_sha256 = _result_digest(continuous_result)
        first_open_count = first_source.source_open_count

        _cleanup_result(continuous_result)
        continuous_result = None
        del first_engine
        gc.collect()

        second_source = _ReplayPreparedSource(source, checkpoint)
        verified = False

        def verify_replayed(boundary: _RuntimeBoundary) -> None:
            nonlocal verified
            actual = RegisteredStreamingReplayCheckpoint(
                kernel=boundary.kernel,
                bars_processed=boundary.bars_processed,
                source_position=boundary.source_position,
                runtime_sha256=boundary.runtime_sha256,
                event_sha256=boundary.event_sha256,
                equity_sha256=boundary.equity_sha256,
                seed=seed,
                commit_sha=commit_sha,
            )
            if actual != checkpoint:
                raise RegisteredStreamingRecoveryError(
                    "replayed runtime does not match the persisted registered checkpoint"
                )
            verified = True

        random.seed(seed)
        second_engine = _instrument_engine(
            engine_factory(),
            target_bars=target_bars,
            observer=verify_replayed,
            position_provider=second_source.checkpoint_position,
        )
        restarted_result = second_engine.run_prepared(second_source)
        if not verified:
            raise RegisteredStreamingRecoveryError(
                "restarted simulation did not verify the checkpoint boundary"
            )
        restarted_sha256 = _result_digest(restarted_result)
        if continuous_sha256 != restarted_sha256:
            raise RegisteredStreamingRecoveryError(
                "continuous and cursor-restarted results are not deterministic equivalents"
            )
        return RegisteredStreamingReplayRun(
            result=restarted_result,
            checkpoint=checkpoint,
            restart_verified=True,
            continuous_sha256=continuous_sha256,
            restarted_sha256=restarted_sha256,
            continuous_source_open_count=first_open_count,
            restarted_source_open_count=second_source.source_open_count,
        )
    except BaseException:
        _cleanup_result(restarted_result)
        raise
    finally:
        first_source.close()
        if second_source is not None:
            second_source.close()
        _cleanup_result(continuous_result)
        random.setstate(previous_random_state)
