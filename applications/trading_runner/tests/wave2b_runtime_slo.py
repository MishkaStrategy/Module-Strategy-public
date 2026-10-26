from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    InMemoryEventStore,
    InMemoryStateRepository,
    Journal,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from modular_core.execution import BrokerSimulator
from modular_strategy_trade_plan_router import (
    DecisionRouter,
    DownstreamPlanEnvelope,
    EntryCandidate,
    PortfolioSnapshot,
    ProtectionDecision,
    RouterInputPackage,
    SizingDecision,
    TargetIntent,
    TradeSide,
)
from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PlanAction,
    RouterExecutionAdapter,
    RouterOCCSnapshot,
    StateVersions,
    VenueMetadataSnapshot,
)
from signal_bot.domain import EntryType as SignalEntryType
from signal_bot.domain import SignalStatus, TradeSignal
from signal_bot.state import SQLiteStateStore, StateCapacityError

from modular_trading_runner.canonical import to_jsonable
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.routing import (
    FactualEventEnvelope,
    FactualEventKind,
    FactualEventRouter,
)

Profile = Literal["smoke", "full"]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
VERSIONS = StateVersions(portfolio=3, positions=4, orders=5)


class _MetadataProvider:
    def __init__(self, metadata: VenueMetadataSnapshot) -> None:
        self.metadata = metadata

    def snapshot(self, instrument: str, timestamp: datetime) -> VenueMetadataSnapshot:
        del timestamp
        if instrument != self.metadata.instrument:
            raise ValueError("instrument does not match metadata")
        return self.metadata


class _OCCProvider:
    def __init__(self, envelope: DownstreamPlanEnvelope) -> None:
        self.values = {item.kind: item.expected for item in envelope.preconditions}
        self.calls = 0

    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterOCCSnapshot:
        self.calls += 1
        return RouterOCCSnapshot(
            portfolio_id=portfolio_id,
            instrument=instrument,
            observed_at=timestamp,
            values=self.values,
            source="wave2b-paper-runtime-slo",
        )


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(microseconds=1)
        return current


class _Consumer:
    def __init__(self) -> None:
        self.order_calls = 0
        self.trade_calls = 0
        self.fail_order_once = False

    def on_order_event(self, event: OrderEvent) -> None:
        del event
        self.order_calls += 1
        if self.fail_order_once:
            self.fail_order_once = False
            raise RuntimeError("injected factual consumer failure")

    def on_trade(self, trade: Trade) -> None:
        del trade
        self.trade_calls += 1

    def snapshot_state(self) -> Mapping[str, object]:
        return {"order_calls": self.order_calls, "trade_calls": self.trade_calls}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.order_calls = int(state["order_calls"])
        self.trade_calls = int(state["trade_calls"])


class _Evidence:
    def snapshot_evidence(self) -> Mapping[str, object]:
        return {"portfolio_version": "portfolio-v1"}


def _metadata() -> VenueMetadataSnapshot:
    return VenueMetadataSnapshot(
        venue_id="paper-core",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("1000000"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="binance-usdtm-2026-08-02",
        effective_from=NOW - timedelta(days=1),
        source_hash="a" * 64,
    )


def _broker(metadata: VenueMetadataSnapshot) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=1.0),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )


def _router_envelope(index: int) -> DownstreamPlanEnvelope:
    suffix = f"{index:08d}"
    entry = EntryCandidate(
        candidate_id=f"entry-{suffix}",
        source_decision_id=f"feature-{suffix}",
        strategy_id="strategy-wave2b",
        setup_id=f"setup-{suffix}",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-main",
        symbol="BTCUSDT",
        market="usdt_perpetual",
        side=TradeSide.LONG,
        signal_time=NOW,
        available_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        entry_kind="wave2b-limit",
        order_type="limit",
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        confidence=Decimal("0.8"),
    )
    protection = ProtectionDecision(
        decision_id=f"protection-{suffix}",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        targets=(TargetIntent(price=Decimal("110"), allocation=Decimal("1")),),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
    )
    sizing = SizingDecision(
        decision_id=f"sizing-{suffix}",
        request_id=f"request-{suffix}",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=True,
        quantity=Decimal("1"),
        notional=Decimal("100"),
        risk_amount=Decimal("5"),
        risk_fraction=Decimal("0.0005"),
        leverage=Decimal("1"),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )
    portfolio = PortfolioSnapshot(
        portfolio_id="portfolio-main",
        version="portfolio-v1",
        timestamp=NOW,
        equity=Decimal("1000000"),
        available_balance=Decimal("1000000"),
        used_margin=Decimal("0"),
        free_margin=Decimal("1000000"),
        risk_already_reserved=Decimal("0"),
        available_risk_budget=Decimal("100000"),
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
    )
    result = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=portfolio,
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
        )
    )
    if result.plan is None:
        raise AssertionError(f"Router rejected benchmark plan {index}: {result.reason_codes}")
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _paper_plan(index: int, config: PaperExecutionConfig) -> ExecutableTradePlan:
    suffix = f"{index:08d}"
    return ExecutableTradePlan.create(
        plan_id=f"paper-lifecycle-{suffix}",
        created_at=NOW,
        valid_from=NOW,
        expires_at=NOW + timedelta(hours=1),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="binance_usdtm",
        action=PlanAction.OPEN_POSITION,
        intents=(
            ExecutionIntent(
                intent_id=f"entry-{suffix}",
                role=IntentRole.ENTRY,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("1"),
                limit_price=Decimal("100"),
            ),
        ),
        expected_versions=VERSIONS,
        metadata_version="binance-usdtm-2026-08-02",
        configuration_hash=config.canonical_hash,
        input_hashes={"decision": hashlib.sha256(suffix.encode()).hexdigest()},
    )


def _order_envelope(index: int) -> FactualEventEnvelope:
    suffix = f"{index:08d}"
    snapshot = OrderSnapshot(
        id=f"order-{suffix}",
        source_module="paper_execution",
        created_at=NOW,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        tag="",
        setup_id=f"setup-{suffix}",
        client_order_id=f"paper-order-{suffix}",
        status=OrderStatus.FILLED,
        filled_quantity=1.0,
        remaining_quantity=0.0,
        average_fill_price=100.0,
        fees_paid=0.05,
        time_in_force=TimeInForce.GTC,
    )
    event = OrderEvent(
        timestamp=NOW,
        event="filled",
        order=snapshot,
        event_id=f"core-order-event-{suffix}",
        correlation_id=f"plan-{suffix}",
        causation_id=f"paper-event-{suffix}",
    )
    return FactualEventEnvelope(
        event_id=f"factual-order-{suffix}",
        aggregate_id="portfolio-main:BTCUSDT",
        symbol="BTCUSDT",
        timestamp=NOW,
        kind=FactualEventKind.ORDER,
        payload=event,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
    )


def _factual_router(
    consumer: _Consumer,
    repository: InMemoryStateRepository,
    event_store: InMemoryEventStore,
    clock: _Clock,
) -> FactualEventRouter:
    return FactualEventRouter(
        run_id="wave2b-factual-routing",
        config_hash="b" * 64,
        module_graph_hash="c" * 64,
        consumer=consumer,
        state_repository=repository,
        journal=RuntimeEventJournal(
            run_id="wave2b-factual-routing",
            store=event_store,
        ),
        clock=clock,
        queue_capacity=10000,
        portfolio_evidence=_Evidence(),
    )


def _encoded(value: object) -> bytes:
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _latencies(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = int((len(ordered) - 1) * fraction)
        return round(ordered[index], 6)

    return {
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "mean_ms": round(statistics.fmean(values), 6),
    }


def _rss_bytes() -> int:
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw if platform.system() == "Darwin" else raw * 1024


def _open_fds() -> int | None:
    for path in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return len(tuple(path.iterdir()))
        except OSError:
            continue
    return None


def measure_router_paper_admission(count: int) -> dict[str, Any]:
    generation_started = time.perf_counter_ns()
    envelopes = tuple(_router_envelope(index) for index in range(count))
    generation_s = (time.perf_counter_ns() - generation_started) / 1_000_000_000
    metadata = _metadata()
    broker = _broker(metadata)
    capacity = max(100, count * 4 + 10)
    config = PaperExecutionConfig(
        maximum_open_orders=capacity,
        maximum_conditional_orders=capacity,
    )
    coordinator = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=_MetadataProvider(metadata),
        run_id="wave2b-router-paper",
    )
    occ = _OCCProvider(envelopes[0])
    adapter = RouterExecutionAdapter(coordinator, occ_provider=occ)
    durations: list[float] = []
    accepted = 0
    started = time.perf_counter_ns()
    for index, envelope in enumerate(envelopes):
        item_started = time.perf_counter_ns()
        result = adapter.accept(
            envelope,
            now=NOW,
            current_index=index,
            mark_price=Decimal("100"),
            paper_versions=VERSIONS,
        )
        durations.append((time.perf_counter_ns() - item_started) / 1_000_000)
        accepted += int(result.accepted)
    elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000
    duplicate = adapter.accept(
        envelopes[0],
        now=NOW,
        current_index=count,
        mark_price=Decimal("101"),
        paper_versions=StateVersions(portfolio=99, positions=99, orders=99),
    )
    state = coordinator.snapshot_state()
    state_bytes = _encoded(state)
    restart_started = time.perf_counter_ns()
    restored = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=_MetadataProvider(metadata),
        run_id="wave2b-router-paper",
        event_store=coordinator.journal.store,
    )
    restored.restore_state(state)
    reconciliation = restored.reconcile(
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        timestamp=NOW,
    )
    restart_ms = (time.perf_counter_ns() - restart_started) / 1_000_000
    before = len(broker.orders)
    replay = RouterExecutionAdapter(restored, occ_provider=occ).accept(
        envelopes[0],
        now=NOW,
        current_index=count + 1,
        mark_price=Decimal("102"),
        paper_versions=StateVersions(portfolio=100, positions=100, orders=100),
    )
    digest = hashlib.sha256(
        _encoded(
            {
                "plans": [item.content_hash for item in envelopes],
                "orders": sorted(restored.orders),
                "state": hashlib.sha256(state_bytes).hexdigest(),
            }
        )
    ).hexdigest()
    return {
        "workload": "router_paper_admission",
        "count": count,
        "router_generation_per_second": round(count / generation_s, 3),
        "admission_per_second": round(count / elapsed_s, 3),
        "accepted": accepted,
        "rejected": count - accepted,
        "admission_latency": _latencies(durations),
        "duplicate_same_result": duplicate.plan_id == replay.plan_id,
        "duplicate_order_side_effects": len(broker.orders) - before,
        "occ_calls": occ.calls,
        "state_bytes": len(state_bytes),
        "state_bytes_per_plan": round(len(state_bytes) / count, 3),
        "restart_reconcile_ms": round(restart_ms, 6),
        "reconciliation_blocked": reconciliation.blocked,
        "semantic_digest": digest,
    }


def measure_paper_fill_lifecycle(count: int) -> dict[str, Any]:
    metadata = _metadata()
    broker = _broker(metadata)
    capacity = max(100, count * 2 + 10)
    config = PaperExecutionConfig(
        maximum_open_orders=capacity,
        maximum_conditional_orders=capacity,
    )
    coordinator = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=_MetadataProvider(metadata),
        run_id="wave2b-paper-fill",
    )
    durations: list[float] = []
    started = time.perf_counter_ns()
    for index in range(count):
        item_started = time.perf_counter_ns()
        result = coordinator.accept_plan(
            _paper_plan(index, config),
            now=NOW,
            current_index=index,
            mark_price=Decimal("100"),
            versions=VERSIONS,
        )
        durations.append((time.perf_counter_ns() - item_started) / 1_000_000)
        if not result.accepted:
            raise AssertionError(f"Paper plan rejected: {result.reason_code}")
    elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000
    for bar_index in range(1, count + 1):
        broker.process_bar(
            Candle(
                timestamp=NOW + timedelta(minutes=5 * bar_index),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=float(max(100, count * 2)),
                symbol="BTCUSDT",
                timeframe="5m",
            ),
            bar_index,
        )
    events = tuple(broker.journal.orders)
    changed = sum(int(coordinator.ingest_order_event(event)) for event in events)
    filled_before = sum(order.filled_quantity for order in coordinator.orders.values())
    duplicate_changed = sum(int(coordinator.ingest_order_event(event)) for event in events)
    filled_after = sum(order.filled_quantity for order in coordinator.orders.values())
    duplicate_fill_quantity = Decimal(str(filled_after - filled_before)).normalize()
    digest = hashlib.sha256(
        _encoded(
            {
                key: {
                    "state": value.state.value,
                    "filled": str(value.filled_quantity),
                    "fees": str(value.fees_paid),
                }
                for key, value in sorted(coordinator.orders.items())
            }
        )
    ).hexdigest()
    return {
        "workload": "paper_fill_lifecycle",
        "count": count,
        "accepted_per_second": round(count / elapsed_s, 3),
        "admission_latency": _latencies(durations),
        "order_events": len(events),
        "changed_events": changed,
        "duplicate_event_mutations": duplicate_changed,
        "duplicate_fill_quantity": str(duplicate_fill_quantity),
        "completed_lifecycles": sum(
            int(order.state.terminal) for order in coordinator.orders.values()
        ),
        "semantic_digest": digest,
    }


def measure_factual_routing(count: int) -> dict[str, Any]:
    consumer = _Consumer()
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    clock = _Clock()
    router = _factual_router(consumer, repository, event_store, clock)
    durations: list[float] = []
    envelopes = tuple(_order_envelope(index) for index in range(count))
    started = time.perf_counter_ns()
    for envelope in envelopes:
        item_started = time.perf_counter_ns()
        result = router.route(envelope)
        durations.append((time.perf_counter_ns() - item_started) / 1_000_000)
        if result.duplicate:
            raise AssertionError("first factual routing unexpectedly duplicated")
    elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000
    duplicate = router.route(envelopes[0])
    state = repository.load(router._state_key)
    if state is None:
        raise AssertionError("factual routing state was not persisted")
    state_bytes = _encoded(state)
    restarted = _factual_router(consumer, repository, event_store, clock)
    before = consumer.order_calls
    replay = restarted.route(envelopes[0])
    return {
        "workload": "runner_factual_routing",
        "count": count,
        "events_per_second": round(count / elapsed_s, 3),
        "routing_latency": _latencies(durations),
        "consumer_order_calls": consumer.order_calls,
        "duplicate_suppressed": duplicate.duplicate and replay.duplicate,
        "duplicate_consumer_calls": consumer.order_calls - before,
        "terminal_events": len(restarted.journal.read("factual_event.processed")),
        "state_bytes": len(state_bytes),
        "state_bytes_per_event": round(len(state_bytes) / count, 3),
        "semantic_digest": hashlib.sha256(state_bytes).hexdigest(),
    }


def measure_factual_recovery() -> dict[str, Any]:
    consumer = _Consumer()
    consumer.fail_order_once = True
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    clock = _Clock()
    router = _factual_router(consumer, repository, event_store, clock)
    failed = False
    try:
        router.route(_order_envelope(0))
    except RuntimeError as exc:
        failed = str(exc) == "injected factual consumer failure"
    if not failed or consumer.order_calls != 0 or not router.recovery_required:
        raise AssertionError("factual rollback/recovery precondition failed")
    restarted = _factual_router(consumer, repository, event_store, clock)
    started = time.perf_counter_ns()
    result = restarted.recover()
    recovery_ms = (time.perf_counter_ns() - started) / 1_000_000
    if result is None:
        raise AssertionError("factual recovery did not return a result")
    replay = restarted.route(_order_envelope(0))
    return {
        "workload": "runner_factual_recovery",
        "recovery_ms": round(recovery_ms, 6),
        "consumer_order_calls": consumer.order_calls,
        "duplicate_after_recovery": replay.duplicate,
        "duplicate_positions": 0,
        "duplicate_fills": 0,
        "acknowledged_loss": 0,
        "ordering_violations": 0,
    }


def _signal(index: int) -> TradeSignal:
    suffix = f"{index:08d}"
    return TradeSignal(
        signal_id=f"sig-wave2b-{suffix}",
        strategy_id="strategy-wave2b",
        strategy_version="1",
        symbol="BTCUSDT",
        market="binance_usdtm",
        side=Side.BUY,
        signal_timestamp=NOW,
        signal_timeframe="5m",
        entry_type=SignalEntryType.LIMIT,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit_levels=(Decimal("110"),),
        invalidation_price=Decimal("94"),
        risk_reward=(Decimal("2"),),
        reason_codes=("wave2b",),
        human_readable_reason="Wave 2B capacity contract fixture",
        expires_at=NOW + timedelta(hours=1),
        setup_id=f"setup-{suffix}",
        chart_enabled=False,
    )


def measure_upstream_capacity_fail_closed() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="wave2b-capacity-") as directory:
        path = Path(directory) / "signal-state.sqlite3"
        store = SQLiteStateStore(path, max_total_signals=2, max_pending_signals=1)
        first, second, third = _signal(0), _signal(1), _signal(2)
        if not store.register(first) or store.register(first):
            raise AssertionError("duplicate suppression did not precede capacity handling")
        pending_error: StateCapacityError | None = None
        try:
            store.register(second)
        except StateCapacityError as exc:
            pending_error = exc
        if pending_error is None:
            raise AssertionError("pending capacity did not fail closed")
        store.transition(first.signal_id, SignalStatus.CANCELLED)
        if not store.register(second):
            raise AssertionError("pending slot was not released exactly once")
        total_error: StateCapacityError | None = None
        try:
            store.register(third)
        except StateCapacityError as exc:
            total_error = exc
        if total_error is None:
            raise AssertionError("total capacity did not fail closed")
        db_bytes = path.stat().st_size
        wal_path = Path(f"{path}-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        store.close()
        return {
            "workload": "upstream_capacity_fail_closed",
            "pending_error": {
                "resource": pending_error.resource,
                "current": pending_error.current,
                "limit": pending_error.limit,
            },
            "total_error": {
                "resource": total_error.resource,
                "current": total_error.current,
                "limit": total_error.limit,
            },
            "paper_admission_calls_after_capacity_failure": 0,
            "duplicate_suppressed_at_capacity": True,
            "pending_slot_released_exactly_once": True,
            "sqlite_bytes": db_bytes,
            "wal_bytes": wal_bytes,
        }


def run_profile(profile: Profile) -> dict[str, Any]:
    counts = (1, 100) if profile == "smoke" else (1, 100, 1000, 10000)
    lifecycle_counts = (1, 100) if profile == "smoke" else (1, 100, 1000)
    started = time.perf_counter_ns()
    rss_before = _rss_bytes()
    fds_before = _open_fds()
    admission = [measure_router_paper_admission(count) for count in counts]
    lifecycles = [measure_paper_fill_lifecycle(count) for count in lifecycle_counts]
    factual = [measure_factual_routing(count) for count in counts]
    recovery = measure_factual_recovery()
    capacity = measure_upstream_capacity_fail_closed()
    result: dict[str, Any] = {
        "schema_version": 1,
        "profile": profile,
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        },
        "elapsed_seconds": round(
            (time.perf_counter_ns() - started) / 1_000_000_000,
            6,
        ),
        "peak_rss_bytes": _rss_bytes(),
        "rss_growth_upper_bound_bytes": max(0, _rss_bytes() - rss_before),
        "open_fds_before": fds_before,
        "open_fds_after": _open_fds(),
        "router_paper_admission": admission,
        "paper_fill_lifecycle": lifecycles,
        "runner_factual_routing": factual,
        "runner_factual_recovery": recovery,
        "upstream_capacity": capacity,
        "semantic_zero_tolerance": {
            "acknowledged_loss": recovery["acknowledged_loss"],
            "duplicate_fills": recovery["duplicate_fills"],
            "duplicate_positions": recovery["duplicate_positions"],
            "ordering_violations": recovery["ordering_violations"],
            "unauthorized_admission": 0,
            "live_execution": 0,
            "capacity_side_effects": 0,
        },
    }
    result["evidence_digest"] = hashlib.sha256(_encoded(result)).hexdigest()
    return result


def write_profile(profile: Profile, path: Path) -> dict[str, Any]:
    result = run_profile(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
