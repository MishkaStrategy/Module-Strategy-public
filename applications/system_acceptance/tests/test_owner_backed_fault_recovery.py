from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

try:
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
        RiskSupervisorSource,
        SourceObservation,
        SourceRead,
        UnavailableSource,
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
    pytest.skip(f"cross-component owner packages unavailable: {exc.name}", allow_module_level=True)

from modular_system_acceptance.fault_recovery import (
    ObservabilityKind,
    assert_recovered_state_matches,
    load_fault_recovery_matrix,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
KEY = b"fault-recovery-owner-contract-key-32b"
MATRIX_PATH = Path(__file__).resolve().parents[1] / "fault_recovery_matrix.v1.json"
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


def _policy() -> RiskSupervisorPolicy:
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


def _evidence(
    *,
    snapshot_id: str = "snapshot-1",
    observed_at: datetime = NOW,
    **overrides: object,
) -> AuthoritativeRiskEvidence:
    values: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
        "source_state_ids": {
            "portfolio": "portfolio-v10",
            "positions": "positions-v20",
            "paper": "paper-v30",
            "market": "market-v40",
            "runner": "runner-v50",
        },
        "session_start_equity": Decimal("1000"),
        "current_equity": Decimal("995"),
        "peak_equity": Decimal("1000"),
        "aggregate_exposure_notional": Decimal("1000"),
        "market_data_observed_at": observed_at,
        "consumer_progress_at": observed_at,
        "reconciliation_blocked_scopes": 0,
        "reconciliation_loss_count": 0,
        "execution_attempts": 20,
        "execution_rejections": 1,
        "cancel_attempts": 20,
        "cancel_failures": 1,
        "fill_samples": 20,
        "mean_abs_slippage_bps": Decimal("5"),
        "fill_quality_fraction": Decimal("0.95"),
        "checkpoint_integrity_ok": True,
        "disk_free_bytes": 10_000,
        "transport_healthy": True,
        "execution_latency_seconds": 0.5,
    }
    values.update(overrides)
    return AuthoritativeRiskEvidence(**values)  # type: ignore[arg-type]


def _owner() -> RiskSupervisor:
    return RiskSupervisor(
        runtime_id="paper-runtime-1",
        policy=_policy(),
        repository=Store(),
        operator_hmac_key=KEY,
    )


def _command(
    command_id: str,
    action: OperatorAction,
    *,
    issued_at: datetime = NOW,
) -> OperatorCommand:
    signature = sign_operator_command(
        KEY,
        command_id=command_id,
        runtime_id="paper-runtime-1",
        action=action,
        issued_at=issued_at,
    )
    return OperatorCommand(
        command_id=command_id,
        runtime_id="paper-runtime-1",
        action=action,
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
        metadata_version="fault-recovery-paper-v1",
        effective_from=NOW - timedelta(days=1),
        source_hash="a" * 64,
    )


def _paper_stack(
    run_id: str,
) -> tuple[BrokerSimulator, PaperExecutionCoordinator, VenueMetadataSnapshot]:
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
    return broker, coordinator, metadata


def _open_plan(
    *,
    plan_id: str,
    timestamp: datetime,
    metadata: VenueMetadataSnapshot,
) -> ExecutableTradePlan:
    config = PaperExecutionConfig()
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
        input_hashes={"fault-recovery": "b" * 64},
    )


def _close_plan(
    *,
    plan_id: str,
    timestamp: datetime,
    metadata: VenueMetadataSnapshot,
) -> ExecutableTradePlan:
    config = PaperExecutionConfig()
    return ExecutableTradePlan.create(
        plan_id=plan_id,
        created_at=timestamp,
        valid_from=timestamp,
        expires_at=timestamp + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="linear_futures",
        action=PlanAction.CLOSE_POSITION,
        intents=(
            ExecutionIntent(
                intent_id=f"{plan_id}-close",
                role=IntentRole.ENTRY,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                reduce_only=True,
            ),
        ),
        expected_versions=VERSIONS,
        metadata_version=metadata.metadata_version,
        configuration_hash=config.canonical_hash,
        input_hashes={"fault-recovery": "b" * 64},
    )


def _bar(timestamp: datetime) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _seed_paper_baseline(
    owner: RiskSupervisor,
    *,
    run_id: str,
) -> tuple[
    BrokerSimulator, PaperExecutionCoordinator, VenueMetadataSnapshot, RiskSupervisedPaperAdmission
]:
    broker, coordinator, metadata = _paper_stack(run_id)
    boundary = RiskSupervisedPaperAdmission(
        supervisor=owner,
        paper_admission=coordinator,
    )
    opened = boundary.accept_plan(
        _open_plan(plan_id=f"{run_id}-baseline", timestamp=NOW, metadata=metadata),
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        versions=VERSIONS,
    )
    assert opened.accepted
    assert len(opened.client_order_ids) == 1
    fill_time = NOW + timedelta(minutes=1)
    broker.process_bar(_bar(fill_time), 1)
    client_id = opened.client_order_ids[0]
    for event in tuple(broker.journal.orders):
        if event.order.client_order_id == client_id:
            coordinator.ingest_order_event(event)
    assert len(broker.orders) == 1
    assert _paper_fill_count(broker) > 0
    return broker, coordinator, metadata, boundary


def _paper_fill_count(broker: BrokerSimulator) -> int:
    return sum(event.event in {"partially_filled", "filled"} for event in broker.journal.orders)


def _operator_retry_factual_state(*, replay_lost_response: bool) -> dict[str, object]:
    owner = _owner()
    owner.evaluate(_evidence(), now=NOW)
    broker, _, metadata, boundary = _seed_paper_baseline(
        owner,
        run_id="recovered" if replay_lost_response else "continuous",
    )
    commit_time = NOW + timedelta(minutes=2)
    command = _command("operator-halt-timeout-1", OperatorAction.HALT, issued_at=commit_time)
    committed = owner.apply_operator_command(command, now=commit_time)
    observed_at = commit_time
    if replay_lost_response:
        observed_at = commit_time + timedelta(seconds=1)
        replayed = owner.apply_operator_command(command, now=observed_at)
        assert replayed == committed

    with pytest.raises(RiskAdmissionBlocked) as blocked:
        boundary.accept_plan(
            _open_plan(
                plan_id="blocked-after-halt",
                timestamp=observed_at,
                metadata=metadata,
            ),
            now=observed_at,
            current_index=2,
            mark_price=Decimal("100"),
            versions=VERSIONS,
        )
    assert blocked.value.safety_state == SafetyState.HALT.value
    assert RiskGuardCode.OPERATOR_EMERGENCY_HALT.value in blocked.value.reason_codes

    broker_state = capture_broker_state(broker)
    return {
        "order_count": broker_state.order_sequence,
        "fill_count": broker_state.fill_sequence,
        "risk_decision_revision": owner.decision(now=observed_at).sequence,
        "operator_request_count": sum(
            row.get("event_type") == "operator_halt" for row in owner.audit_log
        ),
    }


def _out_of_order_factual_state(*, inject_rejected_snapshot: bool) -> dict[str, object]:
    owner = _owner()
    initial = owner.evaluate(_evidence(snapshot_id="snapshot-initial"), now=NOW)
    assert initial.state is SafetyState.HEALTHY

    broker, coordinator, metadata, boundary = _seed_paper_baseline(
        owner,
        run_id="out-of-order",
    )
    refresh_time = NOW + timedelta(minutes=1)
    refreshed = owner.evaluate(
        _evidence(snapshot_id="snapshot-refresh", observed_at=refresh_time),
        now=refresh_time,
    )
    assert refreshed.state is SafetyState.HEALTHY

    before_fault = capture_broker_state(broker)
    if inject_rejected_snapshot:
        fault_time = refresh_time + timedelta(seconds=1)
        stale_time = refresh_time - timedelta(seconds=1)
        rejected = owner.evaluate(
            _evidence(
                snapshot_id="snapshot-out-of-order",
                observed_at=stale_time,
                market_data_observed_at=stale_time,
                consumer_progress_at=stale_time,
            ),
            now=fault_time,
        )
        assert rejected.state is SafetyState.MANUAL_REVIEW
        assert RiskGuardCode.SNAPSHOT_OUT_OF_ORDER in rejected.guard_codes
        assert owner.decision(now=fault_time).sequence == refreshed.sequence
        assert capture_broker_state(broker) == before_fault

    recovery_time = refresh_time + timedelta(seconds=2)
    recovered = owner.evaluate(
        _evidence(snapshot_id="snapshot-recovered", observed_at=recovery_time),
        now=recovery_time,
    )
    assert recovered.state is SafetyState.HEALTHY
    assert recovered.sequence == refreshed.sequence + 1

    closed = boundary.accept_plan(
        _close_plan(
            plan_id="out-of-order-close",
            timestamp=recovery_time,
            metadata=metadata,
        ),
        now=recovery_time,
        current_index=2,
        mark_price=Decimal("100"),
        versions=VERSIONS,
    )
    assert closed.accepted
    assert len(closed.client_order_ids) == 1
    fill_time = recovery_time + timedelta(minutes=1)
    broker.process_bar(_bar(fill_time), 3)
    client_id = closed.client_order_ids[0]
    for event in tuple(broker.journal.orders):
        if event.order.client_order_id == client_id:
            coordinator.ingest_order_event(event)

    broker_state = capture_broker_state(broker)
    assert broker_state.order_sequence == 2
    assert broker_state.fill_sequence >= 2
    assert broker_state.position is None
    return {
        "order_count": broker_state.order_sequence,
        "fill_count": broker_state.fill_sequence,
        "position_revision": broker_state.position,
        "risk_decision_revision": recovered.sequence,
    }


def _decision_for(scenario_id: str) -> tuple[RiskSupervisor, object, datetime]:
    owner = _owner()
    if scenario_id in {"runner_process_kill", "runner_process_kill_before_checkpoint"}:
        return owner, owner.evaluate(_evidence(transport_healthy=False), now=NOW), NOW
    if scenario_id == "market_data_stale_interrupt":
        evidence = _evidence(market_data_observed_at=NOW - timedelta(seconds=16))
        return owner, owner.evaluate(evidence, now=NOW), NOW
    if scenario_id == "out_of_order_owner_evidence":
        owner.evaluate(_evidence(), now=NOW)
        rejected = owner.evaluate(
            _evidence(
                snapshot_id="snapshot-old",
                observed_at=NOW - timedelta(seconds=1),
                market_data_observed_at=NOW - timedelta(seconds=1),
                consumer_progress_at=NOW - timedelta(seconds=1),
            ),
            now=NOW,
        )
        return owner, rejected, NOW
    if scenario_id == "disk_pressure_read_only":
        return owner, owner.evaluate(_evidence(disk_free_bytes=1000), now=NOW), NOW
    if scenario_id == "corrupt_truncated_checkpoint":
        return owner, owner.evaluate(_evidence(checkpoint_integrity_ok=False), now=NOW), NOW
    if scenario_id == "wall_clock_forward_backward":
        owner.evaluate(_evidence(), now=NOW)
        future = NOW + timedelta(seconds=3)
        rejected = owner.evaluate(
            _evidence(
                snapshot_id="snapshot-future",
                observed_at=future,
                market_data_observed_at=future,
                consumer_progress_at=future,
            ),
            now=NOW,
        )
        return owner, rejected, NOW
    if scenario_id == "partial_outage_recovery_order":
        owner.evaluate(_evidence(snapshot_id="snapshot-bad", transport_healthy=False), now=NOW)
        next_time = NOW + timedelta(seconds=1)
        latched = owner.evaluate(
            _evidence(snapshot_id="snapshot-good", observed_at=next_time),
            now=next_time,
        )
        return owner, latched, next_time
    if scenario_id == "operator_retry_after_timeout":
        owner.evaluate(_evidence(), now=NOW)
        command = _command("halt-1", OperatorAction.HALT)
        first = owner.apply_operator_command(command, now=NOW)
        replay = owner.apply_operator_command(command, now=NOW + timedelta(seconds=1))
        assert replay == first
        return owner, replay, NOW + timedelta(seconds=1)
    if scenario_id == "bounded_queue_resource_pressure":
        return owner, owner.evaluate(_evidence(disk_free_bytes=1000), now=NOW), NOW
    owner.evaluate(_evidence(), now=NOW)
    return owner, owner.decision(now=NOW), NOW


def test_matrix_risk_expectations_are_authoritative_risk_supervisor_contracts() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)

    for scenario in matrix.scenarios:
        owner, decision, _ = _decision_for(scenario.scenario_id)
        del owner
        expected_state = SafetyState(scenario.risk_supervisor.state)
        assert getattr(decision, "state") is expected_state, scenario.scenario_id
        guard = scenario.risk_supervisor.guard_code
        if guard is None:
            assert getattr(decision, "guard_codes") == (), scenario.scenario_id
        else:
            assert RiskGuardCode(guard) in getattr(decision, "guard_codes"), scenario.scenario_id


def test_matrix_component_observability_uses_authoritative_backend_contract() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)
    policy = ObservabilityPolicy.paper_default(freshness_seconds=30.0)

    for scenario in matrix.scenarios:
        expected = scenario.observability
        if expected.kind is ObservabilityKind.TRANSPORT:
            assert expected.subject == "control_plane_transport"
            assert expected.subject not in {component.value for component in Component}
            assert expected.health == "unavailable"
            assert expected.reason_code == "transport_unavailable"
            continue

        component = Component(expected.subject)
        expected_health = ComponentHealth(expected.health)
        if component is Component.RISK_SUPERVISOR:
            owner, _, moment = _decision_for(scenario.scenario_id)
            read = RiskSupervisorSource(owner).read(now=moment)
            assert read.observation is not None, scenario.scenario_id
            projected = project_component(read, now=moment, policy=policy)
        elif scenario.scenario_id == "market_data_stale_interrupt":
            source = SourceObservation(
                component=Component.MARKET_DATA,
                health=ComponentHealth.HEALTHY,
                observed_at=NOW - timedelta(seconds=31),
                state_ref=opaque_state_ref({"cursor": "market-v1"}),
            )
            projected = project_component(
                SourceRead(component=Component.MARKET_DATA, observation=source),
                now=NOW,
                policy=policy,
            )
        else:
            read = UnavailableSource(component=component).read(now=NOW)
            projected = project_component(read, now=NOW, policy=policy)

        assert projected.health is expected_health, scenario.scenario_id
        if expected.reason_code is None:
            assert projected.reason_codes == (), scenario.scenario_id
        else:
            assert expected.reason_code in projected.reason_codes, scenario.scenario_id


def test_ephemeral_invalid_evidence_never_overwrites_last_authoritative_projection() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)

    for scenario_id in ("out_of_order_owner_evidence", "wall_clock_forward_backward"):
        scenario = matrix.scenario(scenario_id)
        owner, rejected, moment = _decision_for(scenario_id)
        assert getattr(rejected, "state") is SafetyState.MANUAL_REVIEW
        current = owner.decision(now=moment)
        assert current.state is SafetyState.HEALTHY
        read = RiskSupervisorSource(owner).read(now=moment)
        assert read.observation is not None
        assert read.observation.health is ComponentHealth.HEALTHY
        assert read.observation.reason_codes == ()
        assert scenario.observability.health == ComponentHealth.HEALTHY.value
        assert scenario.observability.reason_code is None


def test_operator_retry_after_lost_response_matches_continuous_paper_factual_state() -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("operator_retry_after_timeout")
    continuous = _operator_retry_factual_state(replay_lost_response=False)
    recovered = _operator_retry_factual_state(replay_lost_response=True)

    assert continuous["order_count"] == 1
    assert continuous["fill_count"] > 0
    assert continuous["operator_request_count"] == 1
    assert recovered["operator_request_count"] == 1
    left, right = assert_recovered_state_matches(
        continuous,
        recovered,
        fields=scenario.factual_state_fields,
    )
    assert left == right


def test_out_of_order_recovery_matches_continuous_paper_factual_state() -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("out_of_order_owner_evidence")
    continuous = _out_of_order_factual_state(inject_rejected_snapshot=False)
    recovered = _out_of_order_factual_state(inject_rejected_snapshot=True)

    assert continuous["order_count"] == 2
    assert continuous["fill_count"] >= 2
    left, right = assert_recovered_state_matches(
        continuous,
        recovered,
        fields=scenario.factual_state_fields,
    )
    assert left == right
