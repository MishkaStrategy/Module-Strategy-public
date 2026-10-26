from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

try:
    from market_data_ingestion.adapters.fake import FakeExchangeAdapter
    from market_data_ingestion.checkpoint import load_checkpoint
    from market_data_ingestion.contracts import MarketDataRequest
    from market_data_ingestion.dataset_reader import (
        RegisteredKlineSelection,
        iter_registered_klines,
        resolve_registered_kline_dataset,
    )
    from market_data_ingestion.enums import DataType, MarketType, RunState
    from market_data_ingestion.errors import AdapterError
    from market_data_ingestion.pipeline import IngestionService
    from market_data_ingestion.records import KlineRecord
    from market_data_ingestion.registry import SQLiteDatasetRegistry
    from market_data_ingestion.storage import LocalStorage
    from modular_core import (
        Candle,
        EngineConfig,
        ExecutionConfig,
        Journal,
        OrderType,
        Side,
        TimeInForce,
    )
    from modular_core.broker_state import capture_broker_state
    from modular_core.execution import BrokerSimulator
    from modular_observability import (
        Component,
        ComponentHealth,
        ObservabilityPolicy,
        SourceObservation,
        SourceRead,
        opaque_state_ref,
        project_component,
    )
    from modular_risk_supervisor import (
        AuthoritativeRiskEvidence,
        OperatorAction,
        OperatorCommand,
        RiskAdmissionBlocked,
        RiskGuardCode,
        RiskSupervisedPaperAdmission,
        RiskSupervisor,
        RiskSupervisorPolicy,
        SafetyState,
        sign_operator_command,
    )
    from paper_execution import (
        ExecutableTradePlan,
        ExecutionIntent,
        IntentRole,
        PaperExecutionConfig,
        PaperExecutionCoordinator,
        PlanAction,
        StateVersions,
        VenueMetadataSnapshot,
    )
except ModuleNotFoundError as exc:
    pytest.skip(
        f"Market Data recovery owner packages unavailable: {exc.name}", allow_module_level=True
    )

from modular_system_acceptance.fault_recovery import (
    assert_recovered_state_matches,
    load_fault_recovery_matrix,
)

MATRIX_PATH = Path(__file__).resolve().parents[1] / "fault_recovery_matrix.v1.json"
KEY = b"fault-recovery-market-data-key-32b"
MARKET_START = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
MARKET_END = datetime(2026, 8, 28, 2, 5, tzinfo=UTC)
VERSIONS = StateVersions(portfolio=3, positions=4, orders=5)


class Store:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


class StaticMetadataProvider:
    def __init__(self, metadata: VenueMetadataSnapshot) -> None:
        self.metadata = metadata

    def snapshot(self, instrument: str, timestamp: datetime) -> VenueMetadataSnapshot:
        del timestamp
        assert instrument == self.metadata.instrument
        return self.metadata


def _request() -> MarketDataRequest:
    return MarketDataRequest(
        request_id="fault-recovery-market-data",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=MARKET_START,
        end=MARKET_END,
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


def _risk_owner() -> RiskSupervisor:
    return RiskSupervisor(
        runtime_id="paper-runtime-market-data",
        policy=_risk_policy(),
        repository=Store(),
        operator_hmac_key=KEY,
    )


def _risk_evidence(
    *,
    snapshot_id: str,
    observed_at: datetime,
    market_data_observed_at: datetime,
    market_state_id: str,
) -> AuthoritativeRiskEvidence:
    return AuthoritativeRiskEvidence(
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        source_state_ids={
            "portfolio": "portfolio-v10",
            "positions": "positions-v20",
            "paper": "paper-v30",
            "market": market_state_id,
            "runner": "runner-v50",
        },
        session_start_equity=Decimal("1000"),
        current_equity=Decimal("995"),
        peak_equity=Decimal("1000"),
        aggregate_exposure_notional=Decimal("1000"),
        market_data_observed_at=market_data_observed_at,
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
        transport_healthy=True,
        execution_latency_seconds=0.5,
    )


def _operator_recover(issued_at: datetime) -> OperatorCommand:
    signature = sign_operator_command(
        KEY,
        command_id="recover-market-data",
        runtime_id="paper-runtime-market-data",
        action=OperatorAction.RECOVER,
        issued_at=issued_at,
    )
    return OperatorCommand(
        command_id="recover-market-data",
        runtime_id="paper-runtime-market-data",
        action=OperatorAction.RECOVER,
        issued_at=issued_at,
        signature=signature,
    )


def _paper_metadata() -> VenueMetadataSnapshot:
    return VenueMetadataSnapshot(
        venue_id="paper-core",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="fault-recovery-market-data-paper-v1",
        effective_from=MARKET_START - timedelta(days=1),
        source_hash="a" * 64,
    )


def _paper_stack(
    owner: RiskSupervisor,
    *,
    run_id: str,
) -> tuple[BrokerSimulator, PaperExecutionCoordinator, RiskSupervisedPaperAdmission]:
    metadata = _paper_metadata()
    broker = BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=1.0),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )
    coordinator = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
    )
    return (
        broker,
        coordinator,
        RiskSupervisedPaperAdmission(supervisor=owner, paper_admission=coordinator),
    )


def _open_plan(*, plan_id: str, timestamp: datetime) -> ExecutableTradePlan:
    config = PaperExecutionConfig()
    metadata = _paper_metadata()
    return ExecutableTradePlan.create(
        plan_id=plan_id,
        created_at=timestamp,
        valid_from=timestamp,
        expires_at=timestamp + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="linear_futures",
        action=PlanAction.OPEN_POSITION,
        intents=(
            ExecutionIntent(
                intent_id=f"{plan_id}-entry",
                role=IntentRole.ENTRY,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
            ),
        ),
        expected_versions=VERSIONS,
        metadata_version=metadata.metadata_version,
        configuration_hash=config.canonical_hash,
        input_hashes={"market-data-recovery": "b" * 64},
    )


def _registered_selection(
    storage: LocalStorage,
    *,
    dataset_id: str,
    version: int,
) -> RegisteredKlineSelection:
    return resolve_registered_kline_dataset(
        storage.root,
        dataset_id,
        version,
        timeframe="5m",
        consumer="modular_backtest",
        deep=True,
    )


def _records(selection: RegisteredKlineSelection) -> tuple[KlineRecord, ...]:
    records = tuple(iter_registered_klines(selection))
    assert len(records) >= 2
    return records


def _core_bar(record: KlineRecord) -> Candle:
    return Candle(
        timestamp=record.open_time,
        open=float(record.open),
        high=float(record.high),
        low=float(record.low),
        close=float(record.close),
        volume=float(record.volume),
        symbol=record.symbol,
        timeframe=record.timeframe,
    )


def _execute_registered_paper_order(
    *,
    owner: RiskSupervisor,
    broker: BrokerSimulator,
    coordinator: PaperExecutionCoordinator,
    boundary: RiskSupervisedPaperAdmission,
    execution_record: KlineRecord,
    admission_time: datetime,
    plan_id: str,
) -> None:
    opened = boundary.accept_plan(
        _open_plan(plan_id=plan_id, timestamp=admission_time),
        now=admission_time,
        current_index=0,
        mark_price=Decimal(execution_record.open),
        versions=VERSIONS,
    )
    assert opened.accepted
    assert len(opened.client_order_ids) == 1
    broker.process_bar(_core_bar(execution_record), 1)
    client_id = opened.client_order_ids[0]
    for event in tuple(broker.journal.orders):
        if event.order.client_order_id == client_id:
            coordinator.ingest_order_event(event)
    assert owner.decision(now=admission_time).state is SafetyState.HEALTHY


def _final_factual_state(
    *,
    storage: LocalStorage,
    run_id: str,
    selection: RegisteredKlineSelection,
    broker: BrokerSimulator,
) -> dict[str, object]:
    checkpoint = load_checkpoint(storage.root / "checkpoints" / f"{run_id}.json")
    broker_state = capture_broker_state(broker)
    return {
        "market_cursor": {
            "completed_chunks": checkpoint.completed_chunks,
            "pending_chunks": checkpoint.pending_chunks,
        },
        "dataset_version": {
            "dataset_id": selection.descriptor.dataset_id,
            "version": selection.descriptor.version,
            "dataset_hash": selection.descriptor.dataset_hash,
        },
        "order_count": broker_state.order_sequence,
        "fill_count": broker_state.fill_sequence,
    }


def _continuous_path(tmp_path: Path) -> dict[str, object]:
    request = _request()
    storage = LocalStorage(tmp_path / "continuous")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter(chunk_bars=4)},
        storage=storage,
        registry=registry,
    )
    result = service.execute(request, format_name="parquet")
    assert result.dataset is not None
    assert result.final_state is RunState.COMPLETED
    selection = _registered_selection(
        storage,
        dataset_id=result.dataset.dataset_id,
        version=result.dataset.version,
    )
    records = _records(selection)
    fresh_record = records[-2]
    execution_record = records[-1]
    fresh_at = fresh_record.close_time

    owner = _risk_owner()
    healthy = owner.evaluate(
        _risk_evidence(
            snapshot_id="continuous-market-ready",
            observed_at=fresh_at,
            market_data_observed_at=fresh_at,
            market_state_id=result.dataset.dataset_hash,
        ),
        now=fresh_at,
    )
    assert healthy.state is SafetyState.HEALTHY
    broker, coordinator, boundary = _paper_stack(owner, run_id="market-continuous")
    admission_time = fresh_at
    _execute_registered_paper_order(
        owner=owner,
        broker=broker,
        coordinator=coordinator,
        boundary=boundary,
        execution_record=execution_record,
        admission_time=admission_time,
        plan_id="market-continuous-open",
    )
    return _final_factual_state(
        storage=storage,
        run_id=result.run_id,
        selection=selection,
        broker=broker,
    )


def _recovered_path(tmp_path: Path) -> dict[str, object]:
    request = _request()
    storage = LocalStorage(tmp_path / "recovered")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    failed_chunk = "fault-recovery-market-data:klines:000002"
    failing = FakeExchangeAdapter(chunk_bars=4, fail_chunks=frozenset({failed_chunk}))
    service = IngestionService(adapters={"fake": failing}, storage=storage, registry=registry)
    with pytest.raises(AdapterError, match="injected adapter failure"):
        service.execute(request, format_name="parquet")

    checkpoints = tuple((storage.root / "checkpoints").glob("mdi:*.json"))
    assert len(checkpoints) == 1
    paused = load_checkpoint(checkpoints[0])
    assert paused.state is RunState.PAUSED
    assert paused.completed_chunks == ("fault-recovery-market-data:klines:000001",)
    assert failed_chunk in paused.pending_chunks

    instrument = failing.resolve_instrument(request.market_type, request.symbol).reference
    plan = failing.plan_download(request, instrument)
    completed_chunk = next(
        chunk for chunk in plan.chunks if chunk.chunk_id == paused.completed_chunks[-1]
    )
    fault_time = completed_chunk.end + timedelta(seconds=31)

    owner = _risk_owner()
    stale = owner.evaluate(
        _risk_evidence(
            snapshot_id="market-interrupted",
            observed_at=fault_time,
            market_data_observed_at=completed_chunk.end,
            market_state_id=paused.run_id,
        ),
        now=fault_time,
    )
    assert stale.state is SafetyState.HALT
    assert RiskGuardCode.MARKET_DATA_STALE in stale.guard_codes

    projected = project_component(
        SourceRead(
            component=Component.MARKET_DATA,
            observation=SourceObservation(
                component=Component.MARKET_DATA,
                health=ComponentHealth.HEALTHY,
                observed_at=completed_chunk.end,
                state_ref=opaque_state_ref(
                    {
                        "run_id": paused.run_id,
                        "completed_chunks": paused.completed_chunks,
                    }
                ),
            ),
        ),
        now=fault_time,
        policy=ObservabilityPolicy.paper_default(freshness_seconds=30.0),
    )
    assert projected.health is ComponentHealth.UNAVAILABLE
    assert "source_stale" in projected.reason_codes

    broker, coordinator, boundary = _paper_stack(owner, run_id="market-recovered")
    before = capture_broker_state(broker)
    with pytest.raises(RiskAdmissionBlocked) as blocked:
        boundary.accept_plan(
            _open_plan(plan_id="blocked-while-market-stale", timestamp=fault_time),
            now=fault_time,
            current_index=0,
            mark_price=Decimal("100"),
            versions=VERSIONS,
        )
    assert blocked.value.safety_state == SafetyState.HALT.value
    assert RiskGuardCode.MARKET_DATA_STALE.value in blocked.value.reason_codes
    assert capture_broker_state(broker) == before

    healthy_adapter = FakeExchangeAdapter(chunk_bars=4)
    resumed = IngestionService(
        adapters={"fake": healthy_adapter},
        storage=storage,
        registry=registry,
    )
    result = resumed.execute(request, format_name="parquet")
    assert result.dataset is not None
    assert result.final_state is RunState.COMPLETED_WITH_WARNINGS
    assert healthy_adapter.restored_chunks == list(paused.completed_chunks)
    assert set(healthy_adapter.acquired_chunks) == set(paused.pending_chunks)

    selection = _registered_selection(
        storage,
        dataset_id=result.dataset.dataset_id,
        version=result.dataset.version,
    )
    records = _records(selection)
    fresh_record = records[-2]
    execution_record = records[-1]
    fresh_at = fresh_record.close_time
    assert fresh_at > fault_time

    latched = owner.evaluate(
        _risk_evidence(
            snapshot_id="market-resumed",
            observed_at=fresh_at,
            market_data_observed_at=fresh_at,
            market_state_id=result.dataset.dataset_hash,
        ),
        now=fresh_at,
    )
    assert latched.state is SafetyState.HALT
    assert RiskGuardCode.RECOVERY_REQUIRED in latched.guard_codes

    recover_at = fresh_at
    recovered = owner.apply_operator_command(_operator_recover(recover_at), now=recover_at)
    assert recovered.state is SafetyState.HEALTHY
    assert recovered.guard_codes == ()

    _execute_registered_paper_order(
        owner=owner,
        broker=broker,
        coordinator=coordinator,
        boundary=boundary,
        execution_record=execution_record,
        admission_time=recover_at,
        plan_id="market-recovered-open",
    )
    return _final_factual_state(
        storage=storage,
        run_id=result.run_id,
        selection=selection,
        broker=broker,
    )


def test_market_data_resume_matches_uninterrupted_registered_paper_state(tmp_path: Path) -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("market_data_stale_interrupt")
    continuous = _continuous_path(tmp_path)
    recovered = _recovered_path(tmp_path)

    assert continuous["order_count"] == 1
    assert continuous["fill_count"] > 0
    left, right = assert_recovered_state_matches(
        continuous,
        recovered,
        fields=scenario.factual_state_fields,
    )
    assert left == right
