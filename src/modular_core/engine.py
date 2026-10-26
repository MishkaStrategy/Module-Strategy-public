from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import cast

from .adapters import DataFeed, SimulationClock
from .audit import Journal, Metrics, calculate_metrics, to_primitive
from .checkpoints import KernelCheckpoint, build_checkpoint
from .config import EngineConfig
from .data import timeframe_to_seconds, validate_candles
from .events import EventStore, InMemoryEventStore
from .execution import BrokerSimulator
from .features import FeatureStore
from .instruments import InstrumentSpec
from .manifest import DependencyPlan, ModuleManifest, ModuleRegistry, parameters_for
from .models import Candle, EquityPoint, ReasonCode, SetupEvent, SetupStatus
from .module_api import (
    MarketView,
    ModuleDecision,
    StrategyModule,
    normalize_modules,
)
from .readiness import ReadinessView, calculate_readiness
from .series import ReadOnlySeries
from .run_manifest import RunManifest, build_run_manifest
from .sessions import SessionDefinition, SessionView, TradingCalendar
from .setups import SetupRegistry, SetupTransitionError
from .timeframes import TimeframeSpec, TimeframeStore

KERNEL_VERSION = "0.4.2"


@dataclass(frozen=True, slots=True)
class BacktestResult:
    metrics: Metrics
    journal: Journal
    bars_processed: int
    modules: tuple[str, ...]
    run_manifest: RunManifest
    module_manifests: tuple[ModuleManifest, ...]
    checkpoint: KernelCheckpoint

    def export_directory(self, path: str | Path) -> Path:
        output = self.journal.export_directory(path)
        (output / "metrics.json").write_text(
            json.dumps(to_primitive(self.metrics), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output / "run.json").write_text(
            json.dumps(
                {
                    "bars_processed": self.bars_processed,
                    "modules": list(self.modules),
                    "manifest": to_primitive(self.run_manifest),
                    "checkpoint": to_primitive(self.checkpoint),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return output


def _engine_config(value: EngineConfig | None) -> EngineConfig:
    if value is None:
        return EngineConfig()
    if not isinstance(value, EngineConfig):
        raise TypeError("config must be EngineConfig or None")
    return value


def _timeframe_specs(value: object) -> tuple[TimeframeSpec, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("timeframes must be a sequence")
    result = tuple(value)
    if any(not isinstance(item, TimeframeSpec) for item in result):
        raise TypeError("timeframes must contain TimeframeSpec")
    return cast(tuple[TimeframeSpec, ...], result)


def _instrument_spec(value: InstrumentSpec | None) -> InstrumentSpec | None:
    if value is not None and not isinstance(value, InstrumentSpec):
        raise TypeError("instrument must be InstrumentSpec or None")
    return value


def _module_registry(value: ModuleRegistry | None) -> ModuleRegistry:
    if value is None:
        return ModuleRegistry()
    if not isinstance(value, ModuleRegistry):
        raise TypeError("module_registry must be ModuleRegistry or None")
    return value


def _event_store_factory(
    value: Callable[[], EventStore],
) -> Callable[[], EventStore]:
    if not callable(value):
        raise TypeError("event_store_factory must be callable")
    return value


def _execution_factory(
    value: Callable[[EngineConfig, Journal, InstrumentSpec], BrokerSimulator] | None,
) -> Callable[[EngineConfig, Journal, InstrumentSpec], BrokerSimulator]:
    if value is None:
        return BrokerSimulator
    if not callable(value):
        raise TypeError("execution_factory must be callable or None")
    return value


def _event_store(value: object) -> EventStore:
    required_methods = ("append", "append_many", "read")
    if any(not callable(getattr(value, name, None)) for name in required_methods):
        raise TypeError("event_store_factory must return EventStore")
    return cast(EventStore, value)


class BacktestEngine:
    def __init__(
        self,
        modules: StrategyModule | Sequence[StrategyModule],
        config: EngineConfig | None = None,
        *,
        timeframes: Sequence[TimeframeSpec] = (),
        instrument: InstrumentSpec | None = None,
        sessions: Sequence[SessionDefinition] = (),
        module_registry: ModuleRegistry | None = None,
        event_store_factory: Callable[[], EventStore] = InMemoryEventStore,
        execution_factory: Callable[[EngineConfig, Journal, InstrumentSpec], BrokerSimulator]
        | None = None,
    ) -> None:
        self.modules = normalize_modules(modules)
        self.module = self.modules[0]
        self.config = _engine_config(config)
        self.timeframes = _timeframe_specs(timeframes)
        self.instrument = _instrument_spec(instrument)
        self.calendar = TradingCalendar(sessions)
        self.module_registry = _module_registry(module_registry)
        self.event_store_factory = _event_store_factory(event_store_factory)
        self.execution_factory = _execution_factory(execution_factory)

    @classmethod
    def from_module_config(
        cls,
        module_config: str | Path | dict[str, object],
        config: EngineConfig | None = None,
        *,
        timeframes: Sequence[TimeframeSpec] = (),
        instrument: InstrumentSpec | None = None,
        sessions: Sequence[SessionDefinition] = (),
        module_registry: ModuleRegistry | None = None,
        event_store_factory: Callable[[], EventStore] = InMemoryEventStore,
        execution_factory: (
            Callable[[EngineConfig, Journal, InstrumentSpec], BrokerSimulator] | None
        ) = None,
    ) -> "BacktestEngine":
        _engine_config(config)
        _timeframe_specs(timeframes)
        _instrument_spec(instrument)
        TradingCalendar(sessions)
        _event_store_factory(event_store_factory)
        _execution_factory(execution_factory)
        registry = _module_registry(module_registry)
        modules = registry.load_config(module_config)
        return cls(
            modules,
            config,
            timeframes=timeframes,
            instrument=instrument,
            sessions=sessions,
            module_registry=registry,
            event_store_factory=event_store_factory,
            execution_factory=execution_factory,
        )

    def run(self, candles: Iterable[Candle] | DataFeed) -> BacktestResult:
        bars = validate_candles(candles, self.config.data)
        plan = self.module_registry.validate_and_plan(
            self.modules,
            base_timeframe=bars[0].timeframe,
        )
        effective_timeframes = self._resolve_timeframes(plan, bars[0].timeframe)
        run_manifest = build_run_manifest(
            kernel_version=KERNEL_VERSION,
            created_at=datetime.now(timezone.utc),
            bars=bars,
            config=self.config,
            manifests=plan.manifests,
            module_parameters=tuple(parameters_for(module) for module in plan.modules),
        )
        instrument = self._resolve_instrument(bars[0])
        event_store = _event_store(self.event_store_factory())
        journal = Journal(run_id=run_manifest.run_id, event_store=event_store)
        broker = self.execution_factory(self.config, journal, instrument)
        if not isinstance(broker, BrokerSimulator):
            raise TypeError("execution_factory must return BrokerSimulator")
        feature_store = FeatureStore()
        setup_registry = SetupRegistry()
        clock = SimulationClock()
        expected_seconds = self.config.data.expected_interval_seconds or timeframe_to_seconds(
            bars[0].timeframe
        )
        store = TimeframeStore(
            effective_timeframes,
            expected_base_interval=(
                timedelta(seconds=expected_seconds) if expected_seconds is not None else None
            ),
        )
        order_event_cursor = 0
        trade_cursor = 0
        active_modules: list[StrategyModule] = []
        manifest_by_name = {manifest.name: manifest for manifest in plan.manifests}
        quarantined_modules: set[str] = set()
        dependents_by_module = self._module_dependents(plan)
        primary_error: BaseException | None = None

        try:
            journal.emit_event(
                timestamp=bars[0].timestamp,
                event_type="run.started",
                source="kernel",
                payload={"run_manifest": to_primitive(run_manifest)},
                symbol=bars[0].symbol,
            )
            for module in plan.modules:
                try:
                    module.on_start()
                except Exception as exc:
                    journal.log_error(bars[0].timestamp, f"module_start:{module.name}", exc)
                    if self.config.strict_module_errors:
                        raise
                    journal.log_skip(
                        bars[0].timestamp,
                        module.name,
                        f"module_start_error:{type(exc).__name__}",
                        symbol=bars[0].symbol,
                    )
                    continue
                active_modules.append(module)

            for index, bar in enumerate(bars):
                clock.advance(bar.close_time)
                broker.process_bar(bar, index)
                order_event_cursor, trade_cursor = self._dispatch_events(
                    broker,
                    order_event_cursor,
                    trade_cursor,
                    journal,
                    active_modules,
                    setup_registry,
                    bar.symbol,
                    features=feature_store,
                    manifests=manifest_by_name,
                    dependents=dependents_by_module,
                    quarantined=quarantined_modules,
                )

                base_gap = False
                if index > 0 and expected_seconds is not None:
                    base_gap = bar.timestamp - bars[index - 1].timestamp != timedelta(
                        seconds=expected_seconds
                    )
                store_gap = store.push(bar)
                data_gap = base_gap or store_gap
                if data_gap:
                    removed = feature_store.invalidate_on_gap(symbol=bar.symbol)
                    for feature in removed:
                        journal.log_feature(bar.timestamp, feature, "gap_invalidated")
                    journal.log_diagnostics(
                        bar.timestamp,
                        "kernel",
                        {
                            "event": "data_gap_detected",
                            "base_index": index,
                            "timeframes_reset": bool(effective_timeframes),
                            "features_invalidated": len(removed),
                        },
                    )

                for feature in feature_store.expire(bar.close_time):
                    journal.log_feature(bar.timestamp, feature, "expired")

                timeframe_snapshot = MappingProxyType(store.snapshot())
                base_start = 0
                if self.config.data.max_base_history is not None:
                    base_start = max(
                        0,
                        index + 1 - self.config.data.max_base_history,
                    )
                base_snapshot = ReadOnlySeries(
                    bars,
                    index + 1,
                    start=base_start,
                )
                readiness_cache: dict[
                    tuple[tuple[tuple[str, int], ...], tuple[str, ...]],
                    ReadinessView,
                ] = {}
                view_cache: dict[int, MarketView] = {}
                for module in active_modules:
                    if module.name in quarantined_modules:
                        continue
                    manifest = manifest_by_name[module.name]
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
                        view = self._build_view(
                            base_bars=base_snapshot,
                            timeframe_bars=timeframe_snapshot,
                            index=index,
                            bar=bar,
                            broker=broker,
                            data_gap=data_gap,
                            feature_store=feature_store,
                            readiness=readiness,
                        )
                        view_cache[id(readiness)] = view
                    try:
                        decision = module.on_bar(view)
                        if not isinstance(decision, ModuleDecision):
                            raise TypeError("on_bar must return ModuleDecision")
                    except Exception as exc:
                        journal.log_error(bar.timestamp, f"module:{module.name}", exc)
                        if self.config.strict_module_errors:
                            raise
                        journal.log_skip(
                            bar.timestamp,
                            module.name,
                            f"module_runtime_error:{type(exc).__name__}",
                            symbol=bar.symbol,
                        )
                        self._quarantine_modules(
                            initial_module=module.name,
                            reason=f"on_bar:{type(exc).__name__}",
                            timestamp=bar.timestamp,
                            symbol=bar.symbol,
                            journal=journal,
                            features=feature_store,
                            manifests=manifest_by_name,
                            dependents=dependents_by_module,
                            quarantined=quarantined_modules,
                        )
                        view_cache.clear()
                        continue

                    if decision.is_empty:
                        continue
                    self._apply_decision(
                        decision=decision,
                        module=module,
                        manifest=manifest,
                        bar=bar,
                        index=index,
                        broker=broker,
                        journal=journal,
                        features=feature_store,
                        setups=setup_registry,
                    )
                    if decision.changes_view_state:
                        view_cache.clear()
                    order_event_cursor, trade_cursor = self._dispatch_events(
                        broker,
                        order_event_cursor,
                        trade_cursor,
                        journal,
                        active_modules,
                        setup_registry,
                        bar.symbol,
                        features=feature_store,
                        manifests=manifest_by_name,
                        dependents=dependents_by_module,
                        quarantined=quarantined_modules,
                    )

                self._append_equity(journal, broker, bar)

            last = bars[-1]
            broker.cancel_all(last.timestamp, ReasonCode.END_OF_DATA.value)
            if self.config.close_open_position_at_end and broker.portfolio.position is not None:
                broker.close_position(
                    last.timestamp,
                    last.close,
                    ReasonCode.END_OF_DATA.value,
                    index=len(bars) - 1,
                    liquidity="taker",
                    reference_price=last.close,
                )
                previous_exposure = journal.equity[-1].position_open
                journal.equity[-1] = EquityPoint(
                    timestamp=last.timestamp,
                    cash=broker.portfolio.cash,
                    unrealized_pnl=0.0,
                    equity=broker.portfolio.cash,
                    position_open=previous_exposure,
                )
            self._dispatch_events(
                broker,
                order_event_cursor,
                trade_cursor,
                journal,
                active_modules,
                setup_registry,
                last.symbol,
                features=feature_store,
                manifests=manifest_by_name,
                dependents=dependents_by_module,
                quarantined=quarantined_modules,
            )
            journal.setup_records = list(setup_registry.all())
            journal.emit_event(
                timestamp=last.timestamp,
                event_type="run.finished",
                source="kernel",
                payload={"bars_processed": len(bars)},
                symbol=last.symbol,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            finish_errors: list[Exception] = []
            for module in reversed(active_modules):
                try:
                    module.on_finish()
                except Exception as exc:
                    journal.log_error(bars[-1].timestamp, f"module_finish:{module.name}", exc)
                    journal.log_skip(
                        bars[-1].timestamp,
                        module.name,
                        f"module_finish_error:{type(exc).__name__}",
                        symbol=bars[-1].symbol,
                    )
                    finish_errors.append(exc)
            if finish_errors and self.config.strict_module_errors and primary_error is None:
                raise ExceptionGroup("module finish failures", finish_errors)

        checkpoint = build_checkpoint(
            run_id=run_manifest.run_id,
            kernel_version=KERNEL_VERSION,
            created_at=bars[-1].close_time,
            last_bar_index=len(bars) - 1,
            last_bar_time=bars[-1].close_time,
            data_hash=run_manifest.data_hash,
            config_hash=run_manifest.config_hash,
            module_hash=run_manifest.module_hash,
            modules=active_modules,
        )
        return BacktestResult(
            metrics=calculate_metrics(journal, self.config.initial_cash),
            journal=journal,
            bars_processed=len(bars),
            modules=tuple(module.name for module in active_modules),
            run_manifest=run_manifest,
            module_manifests=tuple(manifest_by_name[module.name] for module in active_modules),
            checkpoint=checkpoint,
        )

    def _apply_decision(
        self,
        *,
        decision: ModuleDecision,
        module: StrategyModule,
        manifest: ModuleManifest,
        bar: Candle,
        index: int,
        broker: BrokerSimulator,
        journal: Journal,
        features: FeatureStore,
        setups: SetupRegistry,
    ) -> None:
        self._preflight_decision(
            decision=decision,
            module=module,
            manifest=manifest,
            bar=bar,
            features=features,
            setups=setups,
        )
        for setup in decision.setups:
            if setup.module != module.name:
                self._module_contract_error(
                    journal,
                    bar,
                    module,
                    "setup.module must match emitting module name",
                )
                continue
            try:
                setups.apply(setup, bar.timestamp, symbol=bar.symbol)
            except SetupTransitionError as exc:
                journal.log_error(bar.timestamp, f"setup:{module.name}", exc)
                journal.log_skip(
                    bar.timestamp,
                    module.name,
                    ReasonCode.SETUP_TRANSITION_REJECTED.value,
                    symbol=bar.symbol,
                )
                if self.config.strict_setup_transitions:
                    raise
                continue
            journal.log_setup(bar.timestamp, setup, symbol=bar.symbol)
            journal.setup_records = list(setups.all())

        for feature in decision.features:
            if feature.source_module != module.name:
                self._feature_contract_error(
                    journal,
                    bar,
                    module,
                    "feature.source_module must match emitting module",
                )
                continue
            if feature.key not in manifest.produced_features:
                self._feature_contract_error(
                    journal,
                    bar,
                    module,
                    f"undeclared produced feature: {feature.key}",
                )
                continue
            if feature.created_at > bar.close_time:
                self._feature_contract_error(
                    journal, bar, module, "feature cannot be created in the future"
                )
                continue
            stored = features.publish(feature)
            journal.log_feature(bar.timestamp, stored, "published")

        for deletion in decision.feature_deletes:
            if (
                deletion.source_module != module.name
                or deletion.key not in manifest.produced_features
            ):
                self._feature_contract_error(
                    journal,
                    bar,
                    module,
                    f"invalid feature delete contract: {deletion.key}",
                )
                continue
            for removed in features.delete(deletion):
                journal.log_feature(bar.timestamp, removed, "deleted")

        for signal in decision.signals:
            if signal.module != module.name:
                self._module_contract_error(
                    journal,
                    bar,
                    module,
                    "signal.module must match emitting module name",
                )
                continue
            journal.log_signal(bar.timestamp, signal, symbol=bar.symbol)
        for reason in decision.skips:
            journal.log_skip(bar.timestamp, module.name, reason, symbol=bar.symbol)
        journal.log_diagnostics(bar.timestamp, module.name, dict(decision.diagnostics))
        for cancellation in decision.cancellations:
            broker.cancel(cancellation, timestamp=bar.timestamp, source_module=module.name)
        for update in decision.position_updates:
            broker.update_position(
                update,
                timestamp=bar.timestamp,
                source_module=module.name,
                mark_price=bar.close,
            )
        for close in decision.closes:
            broker.schedule_close(
                close,
                timestamp=bar.timestamp,
                current_index=index,
                source_module=module.name,
            )
        for request in decision.orders:
            if request.setup_id is not None and setups.get(request.setup_id) is None:
                transition_error = SetupTransitionError(
                    f"order references unknown setup {request.setup_id}"
                )
                journal.log_error(bar.timestamp, f"order_setup:{module.name}", transition_error)
                if self.config.strict_setup_transitions:
                    raise transition_error
                continue
            order = broker.submit(
                request,
                source_module=module.name,
                created_at=bar.timestamp,
                current_index=index,
                mark_price=bar.close,
            )
            if order is not None and order.setup_id is not None:
                setups.link_order(order.setup_id, order.id, bar.timestamp)

    def _preflight_decision(
        self,
        *,
        decision: ModuleDecision,
        module: StrategyModule,
        manifest: ModuleManifest,
        bar: Candle,
        features: FeatureStore,
        setups: SetupRegistry,
    ) -> None:
        preview_features = features.copy()
        preview_setups = setups.copy()

        for setup in decision.setups:
            if setup.module != module.name:
                if self.config.strict_module_errors:
                    raise ValueError("setup.module must match emitting module name")
                continue
            try:
                preview_setups.apply(setup, bar.timestamp, symbol=bar.symbol)
            except SetupTransitionError:
                if self.config.strict_setup_transitions:
                    raise

        for feature in decision.features:
            contract_error: str | None = None
            if feature.source_module != module.name:
                contract_error = "feature.source_module must match emitting module"
            elif feature.key not in manifest.produced_features:
                contract_error = f"undeclared produced feature: {feature.key}"
            elif feature.created_at > bar.close_time:
                contract_error = "feature cannot be created in the future"
            if contract_error is not None:
                if self.config.strict_feature_contracts:
                    raise ValueError(contract_error)
                continue
            preview_features.publish(feature)

        for deletion in decision.feature_deletes:
            if (
                deletion.source_module != module.name
                or deletion.key not in manifest.produced_features
            ):
                if self.config.strict_feature_contracts:
                    raise ValueError(f"invalid feature delete contract: {deletion.key}")
                continue
            preview_features.delete(deletion)

        if self.config.strict_module_errors:
            for signal in decision.signals:
                if signal.module != module.name:
                    raise ValueError("signal.module must match emitting module name")

        if self.config.strict_setup_transitions:
            for request in decision.orders:
                if request.setup_id is not None and preview_setups.get(request.setup_id) is None:
                    raise SetupTransitionError(f"order references unknown setup {request.setup_id}")

    def _module_contract_error(
        self,
        journal: Journal,
        bar: Candle,
        module: StrategyModule,
        message: str,
    ) -> None:
        exc = ValueError(message)
        journal.log_error(bar.timestamp, f"module:{module.name}", exc)
        if self.config.strict_module_errors:
            raise exc

    def _feature_contract_error(
        self,
        journal: Journal,
        bar: Candle,
        module: StrategyModule,
        message: str,
    ) -> None:
        exc = ValueError(message)
        journal.log_error(bar.timestamp, f"feature:{module.name}", exc)
        journal.log_skip(
            bar.timestamp,
            module.name,
            ReasonCode.FEATURE_CONTRACT_VIOLATION.value,
            symbol=bar.symbol,
        )
        if self.config.strict_feature_contracts:
            raise exc

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
        equity = broker.portfolio.equity(bar.close)
        return MarketView._from_engine_snapshots(
            base_bars=base_bars,
            timeframe_bars=timeframe_bars,
            index=index,
            cash=broker.portfolio.cash,
            equity=equity,
            position=broker.portfolio.position_snapshot(bar.close),
            pending_orders=broker.pending_snapshots(),
            exit_pending=broker.pending_close is not None,
            data_gap=data_gap,
            features=feature_store.snapshot(bar.close_time),
            readiness=readiness,
            sessions=SessionView(self.calendar, bar.close_time),
        )

    @staticmethod
    def _append_equity(journal: Journal, broker: BrokerSimulator, bar: Candle) -> None:
        unrealized = broker.portfolio.unrealized_pnl(bar.close)
        journal.record_equity(
            EquityPoint(
                timestamp=bar.timestamp,
                cash=broker.portfolio.cash,
                unrealized_pnl=unrealized,
                equity=broker.portfolio.cash + unrealized,
                position_open=broker.portfolio.position is not None,
            ),
            symbol=bar.symbol,
        )

    def _dispatch_events(
        self,
        broker: BrokerSimulator,
        order_cursor: int,
        trade_cursor: int,
        journal: Journal,
        modules: Sequence[StrategyModule],
        setups: SetupRegistry,
        symbol: str,
        *,
        features: FeatureStore,
        manifests: Mapping[str, ModuleManifest],
        dependents: Mapping[str, tuple[str, ...]],
        quarantined: set[str],
    ) -> tuple[int, int]:
        if order_cursor == len(journal.orders) and trade_cursor == len(journal.trades):
            return order_cursor, trade_cursor
        new_order_events = journal.orders[order_cursor:]
        new_trades = journal.trades[trade_cursor:]
        for event in new_order_events:
            if event.order.setup_id is not None:
                status_by_event = {
                    "submitted": SetupStatus.ORDERED,
                    "partially_filled": SetupStatus.PARTIALLY_FILLED,
                    "filled": SetupStatus.FILLED,
                    "expired": SetupStatus.EXPIRED,
                    "rejected": SetupStatus.REJECTED,
                    "cancelled": SetupStatus.CANCELLED,
                }
                status = status_by_event.get(event.event)
                if status is not None:
                    existing_setup = setups.get(event.order.setup_id)
                    setup_event = SetupEvent(
                        module=(
                            existing_setup.module
                            if existing_setup is not None
                            else event.order.source_module
                        ),
                        setup_id=event.order.setup_id,
                        status=status,
                        reason=event.reason or event.event,
                        metadata={"order_id": event.order.id},
                        symbol=symbol,
                    )
                    try:
                        setups.apply(setup_event, event.timestamp, symbol=symbol)
                        journal.log_setup(event.timestamp, setup_event, symbol=symbol)
                        if status in {SetupStatus.PARTIALLY_FILLED, SetupStatus.FILLED}:
                            position = broker.portfolio.position
                            if position is not None:
                                setups.link_position(
                                    event.order.setup_id,
                                    position.id,
                                    event.timestamp,
                                )
                    except SetupTransitionError as exc:
                        journal.log_error(event.timestamp, "setup:auto_transition", exc)
                        if self.config.strict_setup_transitions:
                            raise
            for module in modules:
                if module.name in quarantined:
                    continue
                try:
                    module.on_order_event(event)
                except Exception as exc:
                    journal.log_error(event.timestamp, f"module_event:{module.name}", exc)
                    if self.config.strict_module_errors:
                        raise
                    self._quarantine_modules(
                        initial_module=module.name,
                        reason=f"on_order_event:{type(exc).__name__}",
                        timestamp=event.timestamp,
                        symbol=symbol,
                        journal=journal,
                        features=features,
                        manifests=manifests,
                        dependents=dependents,
                        quarantined=quarantined,
                    )
        for trade in new_trades:
            if trade.setup_id is not None and trade.position_closed:
                setup_record = setups.get(trade.setup_id)
                setup_event = SetupEvent(
                    module=setup_record.module if setup_record is not None else "kernel",
                    setup_id=trade.setup_id,
                    status=SetupStatus.CLOSED,
                    reason=trade.exit_reason,
                    metadata={"position_id": trade.position_id},
                    symbol=symbol,
                )
                try:
                    setups.apply(setup_event, trade.exit_time, symbol=symbol)
                    journal.log_setup(trade.exit_time, setup_event, symbol=symbol)
                except SetupTransitionError as exc:
                    journal.log_error(trade.exit_time, "setup:close_transition", exc)
                    if self.config.strict_setup_transitions:
                        raise
            for module in modules:
                if module.name in quarantined:
                    continue
                try:
                    module.on_trade(trade)
                except Exception as exc:
                    journal.log_error(trade.exit_time, f"module_trade:{module.name}", exc)
                    if self.config.strict_module_errors:
                        raise
                    self._quarantine_modules(
                        initial_module=module.name,
                        reason=f"on_trade:{type(exc).__name__}",
                        timestamp=trade.exit_time,
                        symbol=symbol,
                        journal=journal,
                        features=features,
                        manifests=manifests,
                        dependents=dependents,
                        quarantined=quarantined,
                    )
        journal.setup_records = list(setups.all())
        return len(journal.orders), len(journal.trades)

    @staticmethod
    def _module_dependents(plan: DependencyPlan) -> dict[str, tuple[str, ...]]:
        dependents: dict[str, list[str]] = {}
        for manifest in plan.manifests:
            for feature in manifest.required_features:
                producer = plan.produced_by.get(feature)
                if producer is not None:
                    dependents.setdefault(producer, []).append(manifest.name)
        return {name: tuple(dict.fromkeys(values)) for name, values in dependents.items()}

    def _quarantine_modules(
        self,
        *,
        initial_module: str,
        reason: str,
        timestamp: datetime,
        symbol: str,
        journal: Journal,
        features: FeatureStore,
        manifests: Mapping[str, ModuleManifest],
        dependents: Mapping[str, tuple[str, ...]],
        quarantined: set[str],
    ) -> None:
        queue: list[tuple[str, str]] = [(initial_module, reason)]
        while queue:
            module_name, module_reason = queue.pop(0)
            if module_name in quarantined or module_name not in manifests:
                continue
            quarantined.add(module_name)
            journal.log_skip(
                timestamp,
                module_name,
                f"module_quarantined:{module_reason}",
                symbol=symbol,
            )
            for feature in features.delete_source(module_name):
                journal.log_feature(timestamp, feature, "module_quarantined")
            for dependent in dependents.get(module_name, ()):
                queue.append((dependent, f"dependency:{module_name}"))

    def _resolve_timeframes(
        self,
        plan: DependencyPlan,
        base_timeframe: str,
    ) -> tuple[TimeframeSpec, ...]:
        specs = {spec.output_timeframe: spec for spec in self.timeframes}
        base_seconds = timeframe_to_seconds(base_timeframe)
        for timeframe in plan.required_timeframes:
            if timeframe in specs:
                continue
            target_seconds = timeframe_to_seconds(timeframe)
            if base_seconds is None or target_seconds is None:
                raise ValueError(
                    f"cannot infer timeframe {timeframe!r} from base {base_timeframe!r}; "
                    "provide TimeframeSpec explicitly"
                )
            if target_seconds < base_seconds or target_seconds % base_seconds:
                raise ValueError(
                    f"timeframe {timeframe!r} is not an integer multiple of {base_timeframe!r}"
                )
            specs[timeframe] = TimeframeSpec(
                ratio=target_seconds // base_seconds,
                output_timeframe=timeframe,
                gap_safe=True,
                align_to_clock=True,
            )
        return tuple(specs.values())

    def _resolve_instrument(self, first_bar: Candle) -> InstrumentSpec:
        if self.instrument is not None:
            if self.instrument.symbol not in {"*", first_bar.symbol}:
                raise ValueError(
                    f"instrument symbol {self.instrument.symbol} does not match data {first_bar.symbol}"
                )
            return self.instrument
        return InstrumentSpec(
            symbol=first_bar.symbol,
            quantity_step=self.config.risk.quantity_step,
            min_quantity=self.config.risk.min_quantity,
            min_notional=self.config.risk.min_notional,
        )
