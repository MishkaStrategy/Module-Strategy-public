from __future__ import annotations

import os
import select
import signal
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modular_core import (
    AtomicJsonStateRepository,
    Candle,
    EngineConfig,
    ExecutionConfig,
    InstrumentSpec,
    Journal,
    JsonLinesEventStore,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)
from modular_core.broker_state import (
    capture_broker_state,
    restore_broker_state_from_repository,
    save_broker_state,
)
from modular_core.execution import BrokerSimulator
from modular_observability import (
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    UnavailableSource,
    project_component,
)
from modular_risk_supervisor import (
    AuthoritativeRiskEvidence,
    RiskGuardCode,
    RiskSupervisor,
    RiskSupervisorPolicy,
    SafetyState,
)
from modular_system_acceptance.fault_recovery import (
    assert_recovered_state_matches,
    load_fault_recovery_matrix,
)
from modular_trading_runner.checkpoint import RuntimeCheckpointCoordinator
from modular_trading_runner.cycle import PaperBarExecutor, PaperBarRecoveryDecision
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.market_data import (
    BoundedCriticalQueue,
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventValidator,
)
from modular_trading_runner.pipeline import CoreMarketPipeline

MATRIX_PATH = Path(__file__).resolve().parents[1] / "fault_recovery_matrix.v1.json"
NOW = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
RUN_ID = "fault-recovery-process-kill"
CONFIG_HASH = "a" * 64
MODULE_GRAPH_HASH = "b" * 64
BROKER_STATE_KEY = "core-broker:fault-recovery-process-kill"
RISK_KEY = b"fault-recovery-process-kill-key-32"


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class NoopModule(StrategyModule):
    name = "fault-recovery-noop"
    manifest = ModuleManifest(name=name)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        del view
        return ModuleDecision()


class EmptyViewFactory:
    def build(
        self,
        *,
        base_bars: tuple[Candle, ...],
        index: int,
        data_gap: bool,
        features: object,
    ) -> MarketView:
        return MarketView(
            base_bars=base_bars,
            timeframe_bars={},
            index=index,
            cash=10_000.0,
            equity=10_000.0,
            position=None,
            pending_orders=(),
            data_gap=data_gap,
            features=features,
        )


class RiskStore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


class BrokerEvidence:
    def __init__(self, broker: BrokerSimulator) -> None:
        self.broker = broker

    def snapshot_evidence(self, event: MarketEventEnvelope) -> Mapping[str, Any]:
        del event
        return capture_broker_state(self.broker).to_state()


class BrokerReconciliation:
    def resolve(
        self,
        *,
        event: MarketEventEnvelope,
        before: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> PaperBarRecoveryDecision:
        del event
        if current == before:
            return PaperBarRecoveryDecision.RETRY
        before_order = _mapping_int(before, "order_sequence")
        before_fill = _mapping_int(before, "fill_sequence")
        before_position = _mapping_int(before, "position_sequence")
        current_order = _mapping_int(current, "order_sequence")
        current_fill = _mapping_int(current, "fill_sequence")
        current_position = _mapping_int(current, "position_sequence")
        pending = current.get("pending_order_ids")
        if (
            current_order == before_order
            and current_fill == before_fill + 1
            and current_position == before_position + 1
            and isinstance(pending, (tuple, list))
            and not pending
        ):
            return PaperBarRecoveryDecision.APPLIED
        return PaperBarRecoveryDecision.BLOCKED


class PersistingVenue:
    def __init__(
        self,
        *,
        broker: BrokerSimulator,
        repository: AtomicJsonStateRepository,
        crash_phase: str | None = None,
        ready_fd: int | None = None,
    ) -> None:
        self.broker = broker
        self.repository = repository
        self.crash_phase = crash_phase
        self.ready_fd = ready_fd

    def process_bar(self, bar: object, index: int) -> None:
        if self.crash_phase == "before_owner_commit":
            self._announce_and_wait()
        self.broker.process_bar(bar, index)
        save_broker_state(self.repository, BROKER_STATE_KEY, self.broker)
        if self.crash_phase == "after_owner_commit":
            self._announce_and_wait()

    def _announce_and_wait(self) -> None:
        if self.ready_fd is None:
            raise RuntimeError("crash venue is missing readiness pipe")
        os.write(self.ready_fd, b"READY\n")
        while True:
            signal.pause()


def _mapping_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    assert isinstance(item, int) and not isinstance(item, bool)
    return item


def _policy() -> MarketDataPolicy:
    return MarketDataPolicy(
        market="usdt_perpetual",
        instruments=("BTCUSDT",),
        sources=("binance-usdtm",),
        timeframes=("5m",),
        max_event_age_seconds=30.0,
        max_future_skew_seconds=1.0,
        max_tracked_events=16,
    )


def _risk_policy() -> RiskSupervisorPolicy:
    return RiskSupervisorPolicy(
        maximum_daily_loss_fraction=Decimal("0.03"),
        maximum_drawdown_fraction=Decimal("0.06"),
        maximum_exposure_notional=Decimal("10000"),
        maximum_gross_leverage=Decimal("3"),
        maximum_rejection_rate_fraction=Decimal("0.20"),
        maximum_cancel_failure_rate_fraction=Decimal("0.10"),
        minimum_execution_samples=10,
        maximum_mean_abs_slippage_bps=Decimal("25"),
        minimum_fill_quality_fraction=Decimal("0.80"),
        maximum_evidence_age_seconds=15,
        maximum_market_data_age_seconds=15,
        maximum_consumer_stall_seconds=30,
        maximum_execution_latency_seconds=5,
        minimum_disk_free_bytes=1000,
        maximum_future_skew_seconds=2,
    )


def _risk_evidence(*, observed_at: datetime) -> AuthoritativeRiskEvidence:
    return AuthoritativeRiskEvidence(
        snapshot_id="process-kill-transport-down",
        observed_at=observed_at,
        source_state_ids={
            "portfolio": "portfolio-v10",
            "positions": "positions-v20",
            "paper": "paper-v30",
            "market": "market-v40",
            "runner": "runner-unavailable",
        },
        session_start_equity=Decimal("1000"),
        current_equity=Decimal("995"),
        peak_equity=Decimal("1000"),
        aggregate_exposure_notional=Decimal("1000"),
        market_data_observed_at=observed_at,
        consumer_progress_at=observed_at,
        reconciliation_blocked_scopes=0,
        reconciliation_loss_count=0,
        execution_attempts=20,
        execution_rejections=1,
        cancel_attempts=20,
        cancel_failures=1,
        fill_samples=20,
        mean_abs_slippage_bps=Decimal("5"),
        fill_quality_fraction=Decimal("0.95"),
        checkpoint_integrity_ok=True,
        disk_free_bytes=10_000,
        transport_healthy=False,
        execution_latency_seconds=0.5,
    )


def _assert_process_outage_safety(scenario_id: str) -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario(scenario_id)
    fault_time = NOW + timedelta(minutes=10)
    risk_owner = RiskSupervisor(
        runtime_id="paper-runtime-process-kill",
        policy=_risk_policy(),
        repository=RiskStore(),
        operator_hmac_key=RISK_KEY,
    )
    decision = risk_owner.evaluate(_risk_evidence(observed_at=fault_time), now=fault_time)
    assert decision.state is SafetyState(scenario.risk_supervisor.state)
    expected_guard = scenario.risk_supervisor.guard_code
    assert expected_guard is not None
    assert RiskGuardCode(expected_guard) in decision.guard_codes

    expected_observability = scenario.observability
    assert expected_observability.subject == Component.TRADING_RUNNER.value
    read = UnavailableSource(component=Component.TRADING_RUNNER).read(now=fault_time)
    projected = project_component(
        read,
        now=fault_time,
        policy=ObservabilityPolicy.paper_default(freshness_seconds=30.0),
    )
    assert projected.health is ComponentHealth(expected_observability.health)
    expected_reason = expected_observability.reason_code
    assert expected_reason is not None
    assert expected_reason in projected.reason_codes


def _pipeline() -> CoreMarketPipeline:
    module = NoopModule()
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    return CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=EmptyViewFactory(),
        max_history_bars=16,
    )


def _checkpoint(
    *,
    root: Path,
    repository: AtomicJsonStateRepository,
    clock: FixedClock,
) -> tuple[RuntimeCheckpointCoordinator, RuntimeEventJournal]:
    journal = RuntimeEventJournal(
        run_id=RUN_ID,
        store=JsonLinesEventStore(
            root / "runner-events.jsonl",
            fsync=True,
            recover_truncated_tail=True,
        ),
    )
    coordinator = RuntimeCheckpointCoordinator(
        run_id=RUN_ID,
        config_hash=CONFIG_HASH,
        module_graph_hash=MODULE_GRAPH_HASH,
        repository=repository,
        clock=clock,
        journal=journal,
        market_validator=MarketEventValidator(_policy()),
        market_queue=BoundedCriticalQueue(4),
        pipeline=_pipeline(),
    )
    return coordinator, journal


def _broker() -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=1.0),
        ),
        Journal(),
        instrument=InstrumentSpec(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            market_type="linear_futures",
            tick_size=0.1,
            quantity_step=0.001,
            min_quantity=0.001,
            min_notional=5.0,
            contract_size=1.0,
        ),
    )


def _event() -> MarketEventEnvelope:
    candle = Candle(
        timestamp=NOW + timedelta(minutes=5),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketEventEnvelope(
        event_id="process-kill-market-1",
        source_id="binance-usdtm",
        market="usdt_perpetual",
        sequence=1,
        available_at=candle.close_time,
        received_at=candle.close_time + timedelta(seconds=1),
        candle=candle,
    )


def _seed_durable_state(root: Path) -> None:
    repository = AtomicJsonStateRepository(root / "state")
    broker = _broker()
    submitted = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
            client_order_id="process-kill-entry",
        ),
        source_module="fault-recovery",
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    assert submitted is not None
    initial = save_broker_state(repository, BROKER_STATE_KEY, broker)
    assert initial.order_sequence == 1
    assert initial.fill_sequence == 0
    assert initial.position_sequence == 0

    clock = FixedClock(NOW)
    checkpoint, journal = _checkpoint(root=root, repository=repository, clock=clock)
    started = journal.append(
        timestamp=NOW,
        event_type="runtime.started",
        aggregate_id="runner-process-kill",
        payload={},
    )
    assert started.sequence == 1
    checkpoint.capture()


def _paper_executor(
    *,
    repository: AtomicJsonStateRepository,
    broker: BrokerSimulator,
    venue: PersistingVenue,
) -> PaperBarExecutor:
    return PaperBarExecutor(
        run_id=RUN_ID,
        config_hash=CONFIG_HASH,
        state_repository=repository,
        venue=venue,
        evidence=BrokerEvidence(broker),
        reconciliation=BrokerReconciliation(),
    )


def _restore_broker(repository: AtomicJsonStateRepository) -> BrokerSimulator:
    broker = _broker()
    restored = restore_broker_state_from_repository(repository, BROKER_STATE_KEY, broker)
    assert restored.order_sequence == 1
    return broker


def _checkpoint_sequence(repository: AtomicJsonStateRepository, checkpoint_key: str) -> int:
    state = repository.load(checkpoint_key)
    assert state is not None
    journal = state.get("journal")
    assert isinstance(journal, Mapping)
    sequence = journal.get("sequence")
    assert isinstance(sequence, int) and not isinstance(sequence, bool)
    return sequence


def _finalize_runner_checkpoint(
    *,
    root: Path,
    repository: AtomicJsonStateRepository,
) -> int:
    clock = FixedClock(NOW + timedelta(minutes=11))
    checkpoint, journal = _checkpoint(root=root, repository=repository, clock=clock)
    restored = checkpoint.restore()
    assert restored is not None
    recovered = journal.append(
        timestamp=clock.value,
        event_type="runtime.recovery_completed",
        aggregate_id="runner-process-kill",
        payload={},
    )
    assert recovered.sequence == 2
    checkpoint.capture()
    return _checkpoint_sequence(repository, checkpoint.state_key)


def _factual_state(
    *,
    repository: AtomicJsonStateRepository,
    checkpoint_sequence: int,
) -> dict[str, object]:
    broker = _restore_broker(repository)
    snapshot = capture_broker_state(broker)
    assert snapshot.order_sequence == 1
    assert snapshot.fill_sequence == 1
    assert snapshot.position_sequence == 1
    assert snapshot.position is not None
    return {
        "order_count": snapshot.order_sequence,
        "fill_count": snapshot.fill_sequence,
        "position_revision": snapshot.position_sequence,
        "runner_checkpoint_seq": checkpoint_sequence,
    }


def _continuous_path(root: Path) -> dict[str, object]:
    _seed_durable_state(root)
    repository = AtomicJsonStateRepository(root / "state")
    broker = _restore_broker(repository)
    venue = PersistingVenue(broker=broker, repository=repository)
    executor = _paper_executor(repository=repository, broker=broker, venue=venue)
    result = executor.execute(_event(), index=1)
    assert result.duplicate is False
    assert result.recovered is False
    before_duplicate = capture_broker_state(broker)
    duplicate = executor.execute(_event(), index=1)
    assert duplicate.duplicate is True
    assert capture_broker_state(broker) == before_duplicate
    checkpoint_sequence = _finalize_runner_checkpoint(root=root, repository=repository)
    return _factual_state(repository=repository, checkpoint_sequence=checkpoint_sequence)


def _kill_child_at_owner_boundary(root: Path, crash_phase: str) -> None:
    if not hasattr(os, "fork") or not hasattr(signal, "SIGKILL"):
        raise AssertionError("process-kill acceptance requires POSIX fork and SIGKILL")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            repository = AtomicJsonStateRepository(root / "state")
            broker = _restore_broker(repository)
            venue = PersistingVenue(
                broker=broker,
                repository=repository,
                crash_phase=crash_phase,
                ready_fd=write_fd,
            )
            executor = _paper_executor(repository=repository, broker=broker, venue=venue)
            executor.execute(_event(), index=1)
        except BaseException:
            os._exit(97)
        os._exit(98)

    os.close(write_fd)
    try:
        ready, _, _ = select.select([read_fd], [], [], 10.0)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise AssertionError("child did not reach requested process-kill boundary")
        message = os.read(read_fd, 64)
        if message != b"READY\n":
            _, status = os.waitpid(pid, 0)
            raise AssertionError(
                "child exited before process-kill boundary: "
                f"exit={os.waitstatus_to_exitcode(status)} message={message!r}"
            )
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    finally:
        os.close(read_fd)


def _recovered_path(
    root: Path,
    *,
    scenario_id: str,
    crash_phase: str,
) -> dict[str, object]:
    _seed_durable_state(root)
    _kill_child_at_owner_boundary(root, crash_phase)
    _assert_process_outage_safety(scenario_id)

    repository = AtomicJsonStateRepository(root / "state")
    broker = _restore_broker(repository)
    before_recovery = capture_broker_state(broker)
    if crash_phase == "before_owner_commit":
        assert before_recovery.fill_sequence == 0
        assert before_recovery.position_sequence == 0
    else:
        assert before_recovery.fill_sequence == 1
        assert before_recovery.position_sequence == 1

    venue = PersistingVenue(broker=broker, repository=repository)
    executor = _paper_executor(repository=repository, broker=broker, venue=venue)
    assert executor.recovery_required is True
    recovered = executor.recover()
    assert recovered is not None
    assert recovered.recovered is True

    after_recovery = capture_broker_state(broker)
    assert after_recovery.fill_sequence == 1
    assert after_recovery.position_sequence == 1
    duplicate = executor.execute(_event(), index=1)
    assert duplicate.duplicate is True
    assert capture_broker_state(broker) == after_recovery

    checkpoint_sequence = _finalize_runner_checkpoint(root=root, repository=repository)
    return _factual_state(repository=repository, checkpoint_sequence=checkpoint_sequence)


@pytest.mark.parametrize(
    ("scenario_id", "crash_phase"),
    (
        ("runner_process_kill_before_checkpoint", "before_owner_commit"),
        ("runner_process_kill", "after_owner_commit"),
    ),
)
def test_real_sigkill_recovery_matches_uninterrupted_runner_state(
    tmp_path: Path,
    scenario_id: str,
    crash_phase: str,
) -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario(scenario_id)
    continuous = _continuous_path(tmp_path / f"{scenario_id}-continuous")
    recovered = _recovered_path(
        tmp_path / f"{scenario_id}-recovered",
        scenario_id=scenario_id,
        crash_phase=crash_phase,
    )

    left, right = assert_recovered_state_matches(
        continuous,
        recovered,
        fields=scenario.factual_state_fields,
    )
    assert left == right
    assert recovered == {
        "order_count": 1,
        "fill_count": 1,
        "position_revision": 1,
        "runner_checkpoint_seq": 2,
    }
