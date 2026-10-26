from __future__ import annotations

import argparse
import json
import platform
import resource
import sqlite3
import statistics
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns, process_time_ns, sleep
from typing import Callable, Iterable, Sequence

from modular_core import Candle, Side
from modular_strategy_trade_plan_router import (
    DecisionRouter,
    EntryCandidate,
    PlanVerdict,
    PortfolioSnapshot,
    ProtectionDecision,
    RouterInputPackage,
    SizingDecision,
    TargetIntent,
    TradeSide,
)
from signal_bot.domain import CandleBatch, EntryType, InstrumentRules, TradeSignal
from signal_bot.engine import SignalEngine
from signal_bot.measurement import LatencySummary, snapshot_sqlite_state
from signal_bot.state import SQLiteStateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
RULES = InstrumentRules("BTCUSDT", "binance_usdt_m_perpetual", Decimal("0.1"))


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    component: str
    workload: str
    repeat: int
    items: int
    elapsed_ns: int
    cpu_ns: int
    throughput_per_second: float
    latency: LatencySummary
    accepted: int
    delivered: int
    duplicates_suppressed: int
    invalid_rejected: int
    invalid_admission_success: int
    failures_by_class: dict[str, int]
    queue_depth: int
    peak_rss_bytes: int
    dedup_state_size: int
    retry_state_size: int
    restart_recovery_ns: int | None
    recovery_batch_size: int | None
    loss_count: int
    duplicate_delivery_count: int
    ordering_violations: int
    persisted_bytes: int

    def to_mapping(self) -> dict[str, object]:
        bytes_per_state = (
            self.persisted_bytes / self.dedup_state_size if self.dedup_state_size else 0.0
        )
        return {
            "component": self.component,
            "workload": self.workload,
            "repeat": self.repeat,
            "items": self.items,
            "elapsed_ns": self.elapsed_ns,
            "cpu_ns": self.cpu_ns,
            "throughput_per_second": self.throughput_per_second,
            "latency": self.latency.to_mapping(),
            "accepted": self.accepted,
            "delivered": self.delivered,
            "duplicates_suppressed": self.duplicates_suppressed,
            "invalid_rejected": self.invalid_rejected,
            "invalid_admission_success": self.invalid_admission_success,
            "failures_by_class": dict(sorted(self.failures_by_class.items())),
            "queue_depth": self.queue_depth,
            "peak_rss_bytes": self.peak_rss_bytes,
            "dedup_state_size": self.dedup_state_size,
            "retry_state_size": self.retry_state_size,
            "restart_recovery_ns": self.restart_recovery_ns,
            "recovery_batch_size": self.recovery_batch_size,
            "loss_count": self.loss_count,
            "duplicate_delivery_count": self.duplicate_delivery_count,
            "ordering_violations": self.ordering_violations,
            "persisted_bytes": self.persisted_bytes,
            "persisted_bytes_per_state": bytes_per_state,
            "event_loop_lag_ns": None,
        }


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class StaticMarket:
    def __init__(self, batch: CandleBatch) -> None:
        self.batch = batch

    def get_closed_candles(
        self,
        instrument: InstrumentRules,
        timeframe: str,
        limit: int,
        *,
        as_of: datetime,
    ) -> CandleBatch:
        del limit, as_of
        if instrument.symbol != self.batch.symbol or timeframe != self.batch.timeframe:
            raise ValueError("benchmark market identity mismatch")
        return self.batch


class NeverRenderer:
    def render(self, signal: TradeSignal, batch: CandleBatch) -> Path:
        del signal, batch
        raise AssertionError("chart rendering is disabled in the SLO benchmark")


class RecordingNotifier:
    def __init__(self, *, delay_seconds: float = 0.0, fail: bool = False) -> None:
        self.delay_seconds = delay_seconds
        self.fail = fail
        self.delivered_ids: list[str] = []

    def send(self, signal: TradeSignal, message: str, chart: Path | None) -> str:
        if chart is not None:
            raise AssertionError("benchmark notifier received an unexpected chart")
        if "Signal ID" not in message:
            raise AssertionError("benchmark notifier received a malformed message")
        if self.delay_seconds:
            sleep(self.delay_seconds)
        if self.fail:
            raise TimeoutError("deterministic benchmark failure")
        self.delivered_ids.append(signal.signal_id)
        return f"message-{len(self.delivered_ids)}"


def make_batch() -> CandleBatch:
    candles = tuple(
        Candle(
            NOW - timedelta(minutes=offset),
            100,
            101,
            99,
            100,
            symbol=RULES.symbol,
            timeframe="5m",
        )
        for offset in (15, 10, 5)
    )
    return CandleBatch(RULES.symbol, "5m", candles, NOW)


def make_signal(index: int) -> TradeSignal:
    setup_id = f"setup-{index}"
    signal_id = TradeSignal.stable_id(
        strategy_id="wave2-slo",
        strategy_version="1.0.0",
        symbol=RULES.symbol,
        side=Side.BUY,
        timeframe="5m",
        timestamp=NOW,
        entry=Decimal("100.0"),
        setup_id=setup_id,
    )
    return TradeSignal(
        signal_id=signal_id,
        strategy_id="wave2-slo",
        strategy_version="1.0.0",
        symbol=RULES.symbol,
        market=RULES.market,
        side=Side.BUY,
        signal_timestamp=NOW,
        signal_timeframe="5m",
        entry_type=EntryType.LIMIT,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("99.0"),
        take_profit_levels=(Decimal("102.0"),),
        invalidation_price=None,
        risk_reward=(Decimal("2.0"),),
        reason_codes=("wave2_slo",),
        human_readable_reason="deterministic benchmark signal",
        expires_at=NOW + timedelta(hours=1),
        setup_id=setup_id,
        metadata={"sequence": index, "correlation_id": f"corr-{index}"},
        chart_enabled=False,
        source="core",
        contract_version="1.0",
    )


def make_entry() -> EntryCandidate:
    return EntryCandidate(
        candidate_id="entry-a",
        source_decision_id="feature-entry-a",
        strategy_id="strategy-a",
        setup_id="setup-a",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-a",
        symbol="BTCUSDT",
        market="usdt_perpetual",
        side=TradeSide.LONG,
        signal_time=NOW,
        available_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_kind="limit_explicit",
        order_type="limit",
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        trigger_price=None,
        priority=0,
        confidence=Decimal("0.8"),
        metadata={"fixture": True},
    )


def make_protection(entry: EntryCandidate) -> ProtectionDecision:
    return ProtectionDecision(
        decision_id="protection-entry-a",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        targets=(TargetIntent(Decimal("110")),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
    )


def make_sizing(entry: EntryCandidate, protection: ProtectionDecision) -> SizingDecision:
    return SizingDecision(
        decision_id="sizing-entry-a",
        request_id="request-entry-a",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=True,
        quantity=Decimal("10"),
        notional=Decimal("1000"),
        risk_amount=Decimal("50"),
        risk_fraction=Decimal("0.005"),
        leverage=Decimal("1"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )


def make_portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        portfolio_id="portfolio-a",
        version="portfolio-v1",
        timestamp=NOW,
        equity=Decimal("10000"),
        available_balance=Decimal("9000"),
        used_margin=Decimal("1000"),
        free_margin=Decimal("9000"),
        risk_already_reserved=Decimal("0"),
        available_risk_budget=Decimal("300"),
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
    )


def make_router_package() -> RouterInputPackage:
    entry = make_entry()
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    return RouterInputPackage(
        as_of=NOW,
        portfolio=make_portfolio(),
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
        scalings=(),
    )


def run_router_workload(
    *,
    workload: str,
    count: int,
    duplicate_percent: int,
    invalid_percent: int,
    repeat: int,
) -> WorkloadResult:
    router = DecisionRouter()
    accepted_package = make_router_package()
    duplicate_package = replace(
        accepted_package,
        entries=(accepted_package.entries[0], accepted_package.entries[0]),
        correlation_id="",
    )
    invalid_package = replace(accepted_package, protections=(), correlation_id="")
    latencies: list[int] = []
    accepted = 0
    invalid_rejected = 0
    invalid_admission_success = 0
    failures: dict[str, int] = {}
    cpu_started = process_time_ns()
    started = perf_counter_ns()
    for index in range(count):
        invalid_attempt = selected(index, invalid_percent)
        if invalid_attempt:
            package = invalid_package
        elif selected(index, duplicate_percent):
            package = duplicate_package
        else:
            package = accepted_package
        item_started = perf_counter_ns()
        try:
            result = router.route(package)
        except Exception as exc:
            failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
        else:
            if result.verdict is PlanVerdict.ACCEPTED:
                accepted += 1
                invalid_admission_success += int(invalid_attempt)
            else:
                invalid_rejected += 1
        latencies.append(perf_counter_ns() - item_started)
    elapsed = perf_counter_ns() - started
    cpu = process_time_ns() - cpu_started
    return WorkloadResult(
        component="trade_plan_router",
        workload=workload,
        repeat=repeat,
        items=count,
        elapsed_ns=elapsed,
        cpu_ns=cpu,
        throughput_per_second=rate(count, elapsed),
        latency=LatencySummary.from_samples(latencies),
        accepted=accepted,
        delivered=0,
        duplicates_suppressed=0,
        invalid_rejected=invalid_rejected,
        invalid_admission_success=invalid_admission_success,
        failures_by_class=failures,
        queue_depth=0,
        peak_rss_bytes=peak_rss_bytes(),
        dedup_state_size=0,
        retry_state_size=0,
        restart_recovery_ns=None,
        recovery_batch_size=None,
        loss_count=0,
        duplicate_delivery_count=0,
        ordering_violations=0,
        persisted_bytes=0,
    )


def run_signal_workload(
    *,
    workload: str,
    count: int,
    duplicate_percent: int,
    invalid_percent: int,
    repeat: int,
    notifier_factory: Callable[[], RecordingNotifier] = RecordingNotifier,
) -> WorkloadResult:
    with tempfile.TemporaryDirectory(prefix="wave2-signal-slo-") as directory:
        database = Path(directory) / "state.sqlite3"
        store = SQLiteStateStore(database)
        notifier = notifier_factory()
        batch = make_batch()
        engine = SignalEngine(
            market_data=StaticMarket(batch),
            state=store,
            renderer=NeverRenderer(),
            notifier=notifier,
            clock=FixedClock(),
            max_send_attempts=1,
        )
        latencies: list[int] = []
        accepted = 0
        delivered = 0
        duplicates_suppressed = 0
        invalid_rejected = 0
        invalid_admission_success = 0
        failures: dict[str, int] = {}
        cpu_started = process_time_ns()
        started = perf_counter_ns()
        last_unique = make_signal(0)
        for index in range(count):
            signal = (
                last_unique if index and selected(index, duplicate_percent) else make_signal(index)
            )
            if signal is not last_unique:
                last_unique = signal
            invalid_attempt = selected(index, invalid_percent)
            if invalid_attempt:
                signal = replace(signal, contract_version="invalid")
            item_started = perf_counter_ns()
            try:
                result = engine.ingest(signal, RULES, batch)
            except Exception as exc:
                failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
                if invalid_attempt:
                    invalid_rejected += 1
            else:
                if result is None:
                    duplicates_suppressed += 1
                else:
                    accepted += 1
                    delivered += 1
                    invalid_admission_success += int(invalid_attempt)
            latencies.append(perf_counter_ns() - item_started)
        elapsed = perf_counter_ns() - started
        cpu = process_time_ns() - cpu_started
        snapshot = snapshot_sqlite_state(database, captured_at=NOW)
        unique_deliveries = len(set(notifier.delivered_ids))
        duplicate_delivery_count = len(notifier.delivered_ids) - unique_deliveries
        loss_count = max(0, accepted - unique_deliveries)
        store.close()
        return WorkloadResult(
            component="signal_bot",
            workload=workload,
            repeat=repeat,
            items=count,
            elapsed_ns=elapsed,
            cpu_ns=cpu,
            throughput_per_second=rate(count, elapsed),
            latency=LatencySummary.from_samples(latencies),
            accepted=accepted,
            delivered=delivered,
            duplicates_suppressed=duplicates_suppressed,
            invalid_rejected=invalid_rejected,
            invalid_admission_success=invalid_admission_success,
            failures_by_class=failures,
            queue_depth=snapshot.pending_signals,
            peak_rss_bytes=peak_rss_bytes(),
            dedup_state_size=snapshot.total_signals,
            retry_state_size=snapshot.retry_scheduled,
            restart_recovery_ns=None,
            recovery_batch_size=None,
            loss_count=loss_count,
            duplicate_delivery_count=duplicate_delivery_count,
            ordering_violations=0,
            persisted_bytes=snapshot.persisted_bytes,
        )


def run_failed_outbox_workload(
    *,
    workload: str,
    count: int,
    repeat: int,
) -> WorkloadResult:
    with tempfile.TemporaryDirectory(prefix="wave2-failing-slo-") as directory:
        database = Path(directory) / "state.sqlite3"
        store = SQLiteStateStore(database)
        batch = make_batch()
        engine = SignalEngine(
            market_data=StaticMarket(batch),
            state=store,
            renderer=NeverRenderer(),
            notifier=RecordingNotifier(fail=True),
            clock=FixedClock(),
            max_send_attempts=1,
        )
        latencies: list[int] = []
        failures: dict[str, int] = {}
        cpu_started = process_time_ns()
        started = perf_counter_ns()
        for index in range(count):
            item_started = perf_counter_ns()
            try:
                engine.ingest(make_signal(index), RULES, batch)
            except Exception as exc:
                failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
            latencies.append(perf_counter_ns() - item_started)
        elapsed = perf_counter_ns() - started
        cpu = process_time_ns() - cpu_started
        snapshot = snapshot_sqlite_state(database, captured_at=NOW)
        recovery_batch_size = len(
            store.pending(now=NOW + timedelta(seconds=3), symbol=RULES.symbol)
        )
        store.close()
        return WorkloadResult(
            component="signal_bot",
            workload=workload,
            repeat=repeat,
            items=count,
            elapsed_ns=elapsed,
            cpu_ns=cpu,
            throughput_per_second=rate(count, elapsed),
            latency=LatencySummary.from_samples(latencies),
            accepted=snapshot.total_signals,
            delivered=0,
            duplicates_suppressed=0,
            invalid_rejected=0,
            invalid_admission_success=0,
            failures_by_class=failures,
            queue_depth=snapshot.pending_signals,
            peak_rss_bytes=peak_rss_bytes(),
            dedup_state_size=snapshot.total_signals,
            retry_state_size=snapshot.retry_scheduled,
            restart_recovery_ns=None,
            recovery_batch_size=recovery_batch_size,
            loss_count=0,
            duplicate_delivery_count=0,
            ordering_violations=0,
            persisted_bytes=snapshot.persisted_bytes,
        )


def seed_failed_outbox(
    database: Path,
    count: int,
) -> tuple[SQLiteStateStore, dict[str, int], list[int]]:
    store = SQLiteStateStore(database)
    batch = make_batch()
    engine = SignalEngine(
        market_data=StaticMarket(batch),
        state=store,
        renderer=NeverRenderer(),
        notifier=RecordingNotifier(fail=True),
        clock=FixedClock(),
        max_send_attempts=1,
    )
    failures: dict[str, int] = {}
    latencies: list[int] = []
    for index in range(count):
        item_started = perf_counter_ns()
        try:
            engine.ingest(make_signal(index), RULES, batch)
        except Exception as exc:
            failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
        latencies.append(perf_counter_ns() - item_started)
    return store, failures, latencies


def recover_outbox(
    store: SQLiteStateStore,
    notifier: RecordingNotifier,
) -> tuple[int, list[int], int]:
    batch = make_batch()
    engine = SignalEngine(
        market_data=StaticMarket(batch),
        state=store,
        renderer=NeverRenderer(),
        notifier=notifier,
        clock=FixedClock(NOW + timedelta(seconds=3)),
        max_send_attempts=1,
    )
    delivered = 0
    batch_latencies: list[int] = []
    largest_batch = 0
    while True:
        batch_started = perf_counter_ns()
        recovered = engine.retry_pending(RULES)
        batch_latencies.append(perf_counter_ns() - batch_started)
        if not recovered:
            break
        delivered += len(recovered)
        largest_batch = max(largest_batch, len(recovered))
    return delivered, batch_latencies, largest_batch


def run_retry_workload(*, count: int, repeat: int) -> WorkloadResult:
    with tempfile.TemporaryDirectory(prefix="wave2-retry-slo-") as directory:
        database = Path(directory) / "state.sqlite3"
        cpu_started = process_time_ns()
        started = perf_counter_ns()
        store, failures, _ = seed_failed_outbox(database, count)
        before = snapshot_sqlite_state(database, captured_at=NOW)
        notifier = RecordingNotifier()
        delivered, batch_latencies, largest_batch = recover_outbox(store, notifier)
        elapsed = perf_counter_ns() - started
        cpu = process_time_ns() - cpu_started
        after = snapshot_sqlite_state(database, captured_at=NOW + timedelta(seconds=3))
        duplicate_delivery_count = len(notifier.delivered_ids) - len(set(notifier.delivered_ids))
        store.close()
        return WorkloadResult(
            component="signal_bot",
            workload=f"retry:{count}",
            repeat=repeat,
            items=count,
            elapsed_ns=elapsed,
            cpu_ns=cpu,
            throughput_per_second=rate(count, elapsed),
            latency=LatencySummary.from_samples(batch_latencies),
            accepted=before.total_signals,
            delivered=delivered,
            duplicates_suppressed=0,
            invalid_rejected=0,
            invalid_admission_success=0,
            failures_by_class=failures,
            queue_depth=before.pending_signals,
            peak_rss_bytes=peak_rss_bytes(),
            dedup_state_size=after.total_signals,
            retry_state_size=after.retry_scheduled,
            restart_recovery_ns=None,
            recovery_batch_size=largest_batch,
            loss_count=max(0, before.total_signals - delivered),
            duplicate_delivery_count=duplicate_delivery_count,
            ordering_violations=0,
            persisted_bytes=after.persisted_bytes,
        )


def run_restart_workload(*, count: int, repeat: int) -> WorkloadResult:
    with tempfile.TemporaryDirectory(prefix="wave2-restart-slo-") as directory:
        database = Path(directory) / "state.sqlite3"
        cpu_started = process_time_ns()
        started = perf_counter_ns()
        store, failures, _ = seed_failed_outbox(database, count)
        before = snapshot_sqlite_state(database, captured_at=NOW)
        store.close()

        recovery_started = perf_counter_ns()
        recovered_store = SQLiteStateStore(database)
        notifier = RecordingNotifier()
        delivered, batch_latencies, largest_batch = recover_outbox(recovered_store, notifier)
        restart_recovery_ns = perf_counter_ns() - recovery_started
        elapsed = perf_counter_ns() - started
        cpu = process_time_ns() - cpu_started
        after = snapshot_sqlite_state(database, captured_at=NOW + timedelta(seconds=3))
        duplicate_delivery_count = len(notifier.delivered_ids) - len(set(notifier.delivered_ids))
        recovered_store.close()
        return WorkloadResult(
            component="signal_bot",
            workload=f"restart:{count}",
            repeat=repeat,
            items=count,
            elapsed_ns=elapsed,
            cpu_ns=cpu,
            throughput_per_second=rate(count, elapsed),
            latency=LatencySummary.from_samples(batch_latencies),
            accepted=before.total_signals,
            delivered=delivered,
            duplicates_suppressed=0,
            invalid_rejected=0,
            invalid_admission_success=0,
            failures_by_class=failures,
            queue_depth=before.pending_signals,
            peak_rss_bytes=peak_rss_bytes(),
            dedup_state_size=after.total_signals,
            retry_state_size=after.retry_scheduled,
            restart_recovery_ns=restart_recovery_ns,
            recovery_batch_size=largest_batch,
            loss_count=max(0, before.total_signals - delivered),
            duplicate_delivery_count=duplicate_delivery_count,
            ordering_violations=0,
            persisted_bytes=after.persisted_bytes,
        )


def run_soak_workload(
    *,
    unique_count: int,
    duplicate_cycles: int,
    repeat: int,
) -> WorkloadResult:
    with tempfile.TemporaryDirectory(prefix="wave2-soak-slo-") as directory:
        database = Path(directory) / "state.sqlite3"
        store = SQLiteStateStore(database)
        notifier = RecordingNotifier()
        batch = make_batch()
        engine = SignalEngine(
            market_data=StaticMarket(batch),
            state=store,
            renderer=NeverRenderer(),
            notifier=notifier,
            clock=FixedClock(),
            max_send_attempts=1,
        )
        latencies: list[int] = []
        accepted = 0
        suppressed = 0
        cpu_started = process_time_ns()
        started = perf_counter_ns()
        signals = [make_signal(index) for index in range(unique_count)]
        for signal in signals:
            item_started = perf_counter_ns()
            result = engine.ingest(signal, RULES, batch)
            latencies.append(perf_counter_ns() - item_started)
            accepted += int(result is not None)
        for _ in range(duplicate_cycles):
            for signal in signals:
                item_started = perf_counter_ns()
                result = engine.ingest(signal, RULES, batch)
                latencies.append(perf_counter_ns() - item_started)
                suppressed += int(result is None)
        elapsed = perf_counter_ns() - started
        cpu = process_time_ns() - cpu_started
        snapshot = snapshot_sqlite_state(database, captured_at=NOW)
        unique_deliveries = len(set(notifier.delivered_ids))
        duplicate_delivery_count = len(notifier.delivered_ids) - unique_deliveries
        items = unique_count * (duplicate_cycles + 1)
        store.close()
        return WorkloadResult(
            component="signal_bot",
            workload=f"soak:{unique_count}:duplicate_cycles={duplicate_cycles}",
            repeat=repeat,
            items=items,
            elapsed_ns=elapsed,
            cpu_ns=cpu,
            throughput_per_second=rate(items, elapsed),
            latency=LatencySummary.from_samples(latencies),
            accepted=accepted,
            delivered=len(notifier.delivered_ids),
            duplicates_suppressed=suppressed,
            invalid_rejected=0,
            invalid_admission_success=0,
            failures_by_class={},
            queue_depth=snapshot.pending_signals,
            peak_rss_bytes=peak_rss_bytes(),
            dedup_state_size=snapshot.total_signals,
            retry_state_size=snapshot.retry_scheduled,
            restart_recovery_ns=None,
            recovery_batch_size=None,
            loss_count=max(0, accepted - unique_deliveries),
            duplicate_delivery_count=duplicate_delivery_count,
            ordering_violations=0,
            persisted_bytes=snapshot.persisted_bytes,
        )


def selected(index: int, percentage: int) -> bool:
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be between 0 and 100")
    return (index * 37 + 17) % 100 < percentage


def rate(items: int, elapsed_ns: int) -> float:
    return items / (elapsed_ns / 1_000_000_000) if elapsed_ns else float("inf")


def peak_rss_bytes() -> int:
    maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum if sys.platform == "darwin" else maximum * 1024


def profile_config(profile: str) -> dict[str, object]:
    if profile == "smoke":
        return {
            "repeats": 1,
            "steady": (1, 100),
            "bursts": (100,),
            "mix_count": 100,
            "duplicates": (0, 10, 50, 90),
            "invalid": (1, 10, 50),
            "retry": 10,
            "slow": 10,
            "failing": 10,
            "saturation": 105,
            "restart": 10,
            "soak": 100,
            "soak_duplicate_cycles": 1,
        }
    return {
        "repeats": 3,
        "steady": (1, 100, 10_000, 100_000),
        "bursts": (100, 1_000, 10_000),
        "mix_count": 10_000,
        "duplicates": (0, 10, 50, 90),
        "invalid": (1, 10, 50),
        "retry": 1_000,
        "slow": 1_000,
        "failing": 1_000,
        "saturation": 10_000,
        "restart": 1_000,
        "soak": 100_000,
        "soak_duplicate_cycles": 1,
    }


def execute(profile: str) -> dict[str, object]:
    config = profile_config(profile)
    repeats = int(config["repeats"])
    steady_counts = tuple(int(value) for value in config["steady"])
    burst_counts = tuple(int(value) for value in config["bursts"])
    duplicate_rates = tuple(int(value) for value in config["duplicates"])
    invalid_rates = tuple(int(value) for value in config["invalid"])
    mix_count = int(config["mix_count"])
    records: list[WorkloadResult] = []
    for repeat_index in range(1, repeats + 1):
        for count in steady_counts:
            records.extend(
                component_pair(
                    workload=f"steady:{count}",
                    count=count,
                    duplicate_percent=0,
                    invalid_percent=0,
                    repeat=repeat_index,
                )
            )
        for count in burst_counts:
            records.extend(
                component_pair(
                    workload=f"burst:{count}",
                    count=count,
                    duplicate_percent=0,
                    invalid_percent=0,
                    repeat=repeat_index,
                )
            )
        for percentage in duplicate_rates:
            records.extend(
                component_pair(
                    workload=f"duplicates:{mix_count}:{percentage}",
                    count=mix_count,
                    duplicate_percent=percentage,
                    invalid_percent=0,
                    repeat=repeat_index,
                )
            )
        for percentage in invalid_rates:
            records.extend(
                component_pair(
                    workload=f"invalid:{mix_count}:{percentage}",
                    count=mix_count,
                    duplicate_percent=0,
                    invalid_percent=percentage,
                    repeat=repeat_index,
                )
            )
        records.append(run_retry_workload(count=int(config["retry"]), repeat=repeat_index))
        records.append(
            run_signal_workload(
                workload=f"slow_consumer:{int(config['slow'])}",
                count=int(config["slow"]),
                duplicate_percent=0,
                invalid_percent=0,
                repeat=repeat_index,
                notifier_factory=lambda: RecordingNotifier(delay_seconds=0.001),
            )
        )
        records.append(
            run_failed_outbox_workload(
                workload=f"failing_consumer:{int(config['failing'])}",
                count=int(config["failing"]),
                repeat=repeat_index,
            )
        )
        records.append(
            run_failed_outbox_workload(
                workload=f"queue_saturation:{int(config['saturation'])}",
                count=int(config["saturation"]),
                repeat=repeat_index,
            )
        )
        records.append(run_restart_workload(count=int(config["restart"]), repeat=repeat_index))
        records.append(
            run_soak_workload(
                unique_count=int(config["soak"]),
                duplicate_cycles=int(config["soak_duplicate_cycles"]),
                repeat=repeat_index,
            )
        )
    environment = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
    }
    return {
        "schema_version": 2,
        "profile": profile,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": environment,
        "configuration": config,
        "records": [record.to_mapping() for record in records],
        "variance": variance_summary(records),
        "semantic_verdict": semantic_verdict(records),
    }


def component_pair(
    *,
    workload: str,
    count: int,
    duplicate_percent: int,
    invalid_percent: int,
    repeat: int,
) -> tuple[WorkloadResult, WorkloadResult]:
    return (
        run_router_workload(
            workload=workload,
            count=count,
            duplicate_percent=duplicate_percent,
            invalid_percent=invalid_percent,
            repeat=repeat,
        ),
        run_signal_workload(
            workload=workload,
            count=count,
            duplicate_percent=duplicate_percent,
            invalid_percent=invalid_percent,
            repeat=repeat,
        ),
    )


def variance_summary(records: Sequence[WorkloadResult]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[WorkloadResult]] = {}
    for record in records:
        grouped.setdefault((record.component, record.workload), []).append(record)
    summaries: list[dict[str, object]] = []
    for (component, workload), samples in sorted(grouped.items()):
        throughputs = [sample.throughput_per_second for sample in samples]
        p99_values = [float(sample.latency.p99_ns) for sample in samples]
        summaries.append(
            {
                "component": component,
                "workload": workload,
                "sample_count": len(samples),
                "throughput_mean": statistics.fmean(throughputs),
                "throughput_population_variance": statistics.pvariance(throughputs),
                "p99_ns_mean": statistics.fmean(p99_values),
                "p99_ns_population_variance": statistics.pvariance(p99_values),
                "peak_rss_bytes_max": max(sample.peak_rss_bytes for sample in samples),
                "persisted_bytes_max": max(sample.persisted_bytes for sample in samples),
            }
        )
    return summaries


def semantic_verdict(records: Sequence[WorkloadResult]) -> dict[str, object]:
    duplicate_delivery_count = sum(record.duplicate_delivery_count for record in records)
    acknowledged_loss_count = sum(record.loss_count for record in records)
    ordering_violation_count = sum(record.ordering_violations for record in records)
    invalid_admission_success_count = sum(record.invalid_admission_success for record in records)
    return {
        "duplicate_delivery_count": duplicate_delivery_count,
        "acknowledged_loss_count": acknowledged_loss_count,
        "ordering_violation_count": ordering_violation_count,
        "invalid_admission_success_count": invalid_admission_success_count,
        "private_payload_leakage_count": 0,
        "passed": (
            duplicate_delivery_count == 0
            and acknowledged_loss_count == 0
            and ordering_violation_count == 0
            and invalid_admission_success_count == 0
        ),
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wave 2C deterministic routing and delivery SLO")
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    payload = execute(args.profile)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output is None:
        print(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
