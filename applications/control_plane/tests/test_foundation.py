from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from modular_control_plane import (
    BacktestComparisonRequest,
    BacktestControlService,
    BacktestExecution,
    BacktestInspection,
    BacktestMetricChange,
    BacktestRunRequest,
    ComponentHealth,
    ComponentId,
    ControlEvent,
    ControlPlaneService,
    EventType,
    HealthStatus,
    JobError,
    JobProgress,
    JobStatus,
    JobSummary,
    SQLiteBacktestJobStore,
    SystemOverview,
)
from modular_control_plane.adapters.strategy import StrategyFactoryStateReader
from modular_control_plane.backtest import (
    BacktestComparisonData,
    BacktestIdempotencyConflict,
)
from modular_control_plane.strategy import StrategyRunCorrupted, StrategyRunNotFound

NOW = datetime(2026, 8, 7, 3, 50, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


@dataclass(frozen=True)
class StaticAdapter:
    component: ComponentId
    status: HealthStatus = HealthStatus.HEALTHY

    def health(self) -> ComponentHealth:
        return ComponentHealth(
            component=self.component,
            status=self.status,
            checked_at=NOW,
            summary=f"{self.component.value} is {self.status.value.lower()}",
        )


@dataclass(frozen=True)
class MismatchedAdapter:
    component: ComponentId = ComponentId.BACKTEST

    def health(self) -> ComponentHealth:
        return ComponentHealth(
            component=ComponentId.RESEARCH,
            status=HealthStatus.HEALTHY,
            checked_at=NOW,
            summary="wrong identity",
        )


@dataclass(frozen=True)
class ExplodingAdapter:
    component: ComponentId = ComponentId.SIGNAL_BOT

    def health(self) -> ComponentHealth:
        raise RuntimeError("TOP-SECRET bearer value must never escape")


class FakeBacktestRunner:
    def __init__(self, root: Path) -> None:
        self._root = root

    def run(self, config_id: str) -> BacktestExecution:
        result = self._root / f"run-{config_id}"
        result.mkdir(parents=True, exist_ok=True)
        return BacktestExecution(
            run_id=result.name,
            output_directory=result,
            bars_processed=120,
            restart_verified=True,
        )

    def inspect(self, result_directory: Path) -> BacktestInspection:
        return BacktestInspection(
            run_id=result_directory.name,
            metrics={"net_return": 0.12, "trades": 7},
        )

    def compare(self, left: Path, right: Path) -> BacktestComparisonData:
        return BacktestComparisonData(
            left_run_id=left.name,
            right_run_id=right.name,
            changes={
                "net_return": BacktestMetricChange(left=0.12, right=0.18, delta=0.06),
            },
        )


class SecretExplodingBacktestRunner(FakeBacktestRunner):
    def run(self, config_id: str) -> BacktestExecution:
        raise RuntimeError(f"TOP-SECRET token leaked from {config_id}")


def test_job_status_terminal_semantics_do_not_conflate_failure_and_cancellation() -> None:
    assert JobStatus.SUCCEEDED.terminal
    assert JobStatus.FAILED.terminal
    assert JobStatus.CANCELLED.terminal
    assert not JobStatus.RUNNING.terminal
    assert JobStatus.FAILED is not JobStatus.CANCELLED


def test_determinate_progress_derives_percent_from_measured_units() -> None:
    progress = JobProgress(
        phase="BACKTESTING",
        determinate=True,
        completed_units=25,
        total_units=100,
    )
    assert progress.percent == 25.0


def test_indeterminate_progress_rejects_invented_percent() -> None:
    with pytest.raises(ValidationError, match="indeterminate progress"):
        JobProgress(phase="PLANNING", determinate=False, percent=50.0)


def test_job_summary_requires_error_only_for_failed_terminal_job() -> None:
    error = JobError(
        code="BACKTEST_FAILED",
        message="Backtest could not complete",
        action="Open logs and inspect the failure evidence",
        retryable=False,
        error_id="err-1",
    )
    failed = JobSummary(
        job_id="job-1",
        component=ComponentId.BACKTEST,
        operation="backtest",
        status=JobStatus.FAILED,
        phase="FAILED",
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
        error=error,
    )
    assert failed.status is JobStatus.FAILED

    with pytest.raises(ValidationError, match="failed jobs require error"):
        JobSummary(
            job_id="job-2",
            component=ComponentId.BACKTEST,
            operation="backtest",
            status=JobStatus.FAILED,
            phase="FAILED",
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )

    with pytest.raises(ValidationError, match="only failed jobs"):
        JobSummary(
            job_id="job-3",
            component=ComponentId.BACKTEST,
            operation="backtest",
            status=JobStatus.CANCELLED,
            phase="CANCELLED",
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
            error=error,
        )


def test_contracts_require_timezone_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ComponentHealth(
            component=ComponentId.MARKET_DATA,
            status=HealthStatus.HEALTHY,
            checked_at=datetime(2026, 8, 7, 3, 50),
            summary="healthy",
        )


def test_event_payload_is_bounded_to_safe_scalar_values() -> None:
    event = ControlEvent(
        event_id="market-data:42",
        source_cursor="42",
        component=ComponentId.MARKET_DATA,
        resource_type="job",
        resource_id="job-42",
        operation="download",
        event_type=EventType.PROGRESS,
        status=JobStatus.RUNNING,
        phase="DOWNLOADING",
        timestamp=NOW,
        payload={"completed_chunks": 5, "warning": None},
    )
    assert event.payload == {"completed_chunks": 5, "warning": None}

    with pytest.raises(ValidationError):
        ControlEvent(
            event_id="bad",
            component=ComponentId.MARKET_DATA,
            resource_type="job",
            resource_id="job-42",
            event_type=EventType.LOG,
            timestamp=NOW,
            payload={"nested": {"raw": "not allowed"}},  # type: ignore[dict-item]
        )


def test_service_aggregates_healthy_components_in_stable_order() -> None:
    service = ControlPlaneService(
        [
            StaticAdapter(ComponentId.RESEARCH),
            StaticAdapter(ComponentId.BACKTEST),
        ],
        clock=fixed_clock,
    )
    overview = service.overview()

    assert overview.mode == "PAPER"
    assert overview.status is HealthStatus.HEALTHY
    assert tuple(item.component for item in overview.components) == (
        ComponentId.BACKTEST,
        ComponentId.RESEARCH,
    )
    assert service.registered_components == (ComponentId.BACKTEST, ComponentId.RESEARCH)


def test_required_missing_component_fails_closed() -> None:
    service = ControlPlaneService(
        [StaticAdapter(ComponentId.MARKET_DATA)],
        required_components=(ComponentId.MARKET_DATA, ComponentId.PAPER_RUNTIME),
        clock=fixed_clock,
    )
    overview = service.overview()

    assert overview.status is HealthStatus.DEGRADED
    missing = next(
        item for item in overview.components if item.component is ComponentId.PAPER_RUNTIME
    )
    assert missing.status is HealthStatus.UNAVAILABLE
    assert missing.details["reason"] == "adapter_not_registered"


def test_all_missing_required_components_make_system_unavailable() -> None:
    service = ControlPlaneService(
        required_components=(ComponentId.BACKTEST, ComponentId.RESEARCH),
        clock=fixed_clock,
    )
    assert service.overview().status is HealthStatus.UNAVAILABLE


def test_degraded_component_degrades_system_without_hiding_other_components() -> None:
    service = ControlPlaneService(
        [
            StaticAdapter(ComponentId.MARKET_DATA),
            StaticAdapter(ComponentId.RESEARCH, HealthStatus.DEGRADED),
        ],
        clock=fixed_clock,
    )
    overview = service.overview()
    assert overview.status is HealthStatus.DEGRADED
    assert len(overview.components) == 2


def test_adapter_exception_is_isolated_and_secret_message_is_redacted() -> None:
    service = ControlPlaneService([ExplodingAdapter()], clock=fixed_clock)
    snapshot = service.health(ComponentId.SIGNAL_BOT)
    rendered = snapshot.model_dump_json()

    assert snapshot.status is HealthStatus.UNAVAILABLE
    assert snapshot.details == {
        "error_type": "RuntimeError",
        "reason": "health_check_failed",
    }
    assert "TOP-SECRET" not in rendered
    assert "bearer" not in rendered


def test_adapter_identity_mismatch_fails_closed() -> None:
    service = ControlPlaneService([MismatchedAdapter()], clock=fixed_clock)
    snapshot = service.health(ComponentId.BACKTEST)

    assert snapshot.status is HealthStatus.UNAVAILABLE
    assert snapshot.details["reason"] == "adapter_component_mismatch"
    assert snapshot.details["reported_component"] == ComponentId.RESEARCH.value


def test_duplicate_adapter_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate component adapter"):
        ControlPlaneService(
            [StaticAdapter(ComponentId.BACKTEST), StaticAdapter(ComponentId.BACKTEST)],
            clock=fixed_clock,
        )


def test_naive_clock_fails_closed() -> None:
    service = ControlPlaneService(
        required_components=(ComponentId.BACKTEST,),
        clock=lambda: datetime(2026, 8, 7, 3, 50),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        service.overview()


def test_overview_contract_cannot_switch_to_live_mode() -> None:
    with pytest.raises(ValidationError):
        SystemOverview(
            mode="LIVE",  # type: ignore[arg-type]
            status=HealthStatus.HEALTHY,
            checked_at=NOW,
            components=(),
        )


def test_backtest_store_enforces_actor_scoped_idempotency(tmp_path: Path) -> None:
    store = SQLiteBacktestJobStore(tmp_path / "backtests.sqlite3", clock=fixed_clock)
    first, created = store.create_job(
        BacktestRunRequest(config_id="baseline"),
        idempotency_key="same-request",
        actor_id="local-operator",
    )
    repeated, repeated_created = store.create_job(
        BacktestRunRequest(config_id="baseline"),
        idempotency_key="same-request",
        actor_id="local-operator",
    )

    assert created is True
    assert repeated_created is False
    assert repeated.job.job_id == first.job.job_id
    assert first.job.status is JobStatus.QUEUED

    with pytest.raises(BacktestIdempotencyConflict):
        store.create_job(
            BacktestRunRequest(config_id="candidate"),
            idempotency_key="same-request",
            actor_id="local-operator",
        )


def test_backtest_service_runs_durable_job_and_verifies_result_and_compare(tmp_path: Path) -> None:
    store = SQLiteBacktestJobStore(tmp_path / "backtests.sqlite3", clock=fixed_clock)
    service = BacktestControlService(
        store, FakeBacktestRunner(tmp_path / "results"), clock=fixed_clock
    )

    left = service.create_run(
        BacktestRunRequest(config_id="left"),
        idempotency_key="left-run",
        actor_id="local-operator",
    ).record
    right = service.create_run(
        BacktestRunRequest(config_id="right"),
        idempotency_key="right-run",
        actor_id="local-operator",
    ).record

    left_done = service.run_job(left.job.job_id)
    right_done = service.run_job(right.job.job_id)
    assert left_done.job.status is JobStatus.SUCCEEDED
    assert left_done.result is not None
    assert left_done.result.bars_processed == 120
    assert left_done.result.restart_verified is True
    assert right_done.job.status is JobStatus.SUCCEEDED

    result = service.get_result(left.job.job_id)
    assert result.run_id == "run-left"
    assert result.metrics == {"net_return": 0.12, "trades": 7}

    comparison = service.compare(
        BacktestComparisonRequest(
            left_job_id=left.job.job_id,
            right_job_id=right.job.job_id,
        )
    )
    assert comparison.left_run_id == "run-left"
    assert comparison.right_run_id == "run-right"
    assert comparison.changes["net_return"].delta == pytest.approx(0.06)


def test_backtest_unexpected_runner_failure_redacts_secret_message(tmp_path: Path) -> None:
    store = SQLiteBacktestJobStore(tmp_path / "backtests.sqlite3", clock=fixed_clock)
    service = BacktestControlService(
        store,
        SecretExplodingBacktestRunner(tmp_path / "results"),
        clock=fixed_clock,
    )
    created = service.create_run(
        BacktestRunRequest(config_id="secret-case"),
        idempotency_key="secret-run",
        actor_id="local-operator",
    ).record

    failed = service.run_job(created.job.job_id)
    rendered = failed.model_dump_json()

    assert failed.job.status is JobStatus.FAILED
    assert failed.job.error is not None
    assert failed.job.error.code == "BACKTEST_EXECUTION_FAILED"
    assert failed.job.error.details == {"error_type": "RuntimeError"}
    assert "TOP-SECRET" not in rendered
    assert "token leaked" not in rendered


def test_backtest_store_marks_interrupted_running_job_as_retryable_failure(tmp_path: Path) -> None:
    path = tmp_path / "backtests.sqlite3"
    first_store = SQLiteBacktestJobStore(path, clock=fixed_clock)
    created, _ = first_store.create_job(
        BacktestRunRequest(config_id="baseline"),
        idempotency_key="interrupted-run",
        actor_id="local-operator",
    )
    started = first_store.start_job(created.job.job_id)
    assert started.job.status is JobStatus.RUNNING
    assert started.job.progress is not None
    assert started.job.progress.determinate is False

    recovered_store = SQLiteBacktestJobStore(path, clock=fixed_clock)
    recovered = recovered_store.get_job(created.job.job_id)

    assert recovered.job.status is JobStatus.FAILED
    assert recovered.job.phase == "INTERRUPTED"
    assert recovered.job.error is not None
    assert recovered.job.error.code == "BACKTEST_SERVER_INTERRUPTED"
    assert recovered.job.error.retryable is True


def test_backtest_health_reports_durable_counts_without_historical_failure_degradation(
    tmp_path: Path,
) -> None:
    store = SQLiteBacktestJobStore(tmp_path / "backtests.sqlite3", clock=fixed_clock)
    service = BacktestControlService(
        store, FakeBacktestRunner(tmp_path / "results"), clock=fixed_clock
    )
    created = service.create_run(
        BacktestRunRequest(config_id="baseline"),
        idempotency_key="health-run",
        actor_id="local-operator",
    ).record
    service.run_job(created.job.job_id)

    health = service.health()
    assert health.component is ComponentId.BACKTEST
    assert health.status is HealthStatus.HEALTHY
    assert health.details["jobs_total"] == 1
    assert health.details["jobs_succeeded"] == 1


def _write_strategy_checkpoint(
    state_root: Path,
    *,
    run_id: str = "factory-run-1",
    decision_chain_head: str | None = None,
) -> Path:
    from strategy_factory.checkpoint import AtomicCheckpointStore
    from strategy_factory.contracts import FactoryStage, FactoryStatus, StrategyFactoryCheckpoint

    checkpoint_root = state_root / run_id / "checkpoints"
    checkpoint = StrategyFactoryCheckpoint(
        run_id=run_id,
        request_fingerprint="1" * 64,
        status=FactoryStatus.COMPLETED,
        stage=FactoryStage.PACKAGE,
        stage_sequence=14,
        dataset_fingerprint="2" * 64,
        commit_sha="3" * 40,
        manifest_id="manifest-1",
        completed_artifacts={},
        submitted_batches={},
        processed_event_ids=frozenset(),
        budget_state={},
        decision_chain_head=decision_chain_head,
        updated_at=NOW,
    )
    return AtomicCheckpointStore(checkpoint_root).save(checkpoint)


def test_strategy_reader_missing_run_does_not_create_state(tmp_path: Path) -> None:
    state_root = tmp_path / "strategy-state"
    state_root.mkdir()
    reader = StrategyFactoryStateReader(state_root)

    with pytest.raises(StrategyRunNotFound):
        reader.get("missing-run")

    assert tuple(state_root.iterdir()) == ()
    assert not (state_root / "missing-run").exists()


def test_strategy_reader_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    state_root = tmp_path / "strategy-state"
    state_root.mkdir()
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (state_root / "linked-run").symlink_to(outside, target_is_directory=True)
    reader = StrategyFactoryStateReader(state_root)

    scan = reader.scan(max_runs=10)

    assert scan.runs == ()
    assert scan.invalid_count == 1
    with pytest.raises(StrategyRunNotFound):
        reader.get("linked-run")


def test_strategy_reader_fails_closed_on_checkpoint_checksum_tamper(tmp_path: Path) -> None:
    state_root = tmp_path / "strategy-state"
    checkpoint_path = _write_strategy_checkpoint(state_root)
    envelope = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    envelope["payload"]["manifest_id"] = "tampered-manifest"
    checkpoint_path.write_text(json.dumps(envelope), encoding="utf-8")
    reader = StrategyFactoryStateReader(state_root)

    with pytest.raises(StrategyRunCorrupted):
        reader.get("factory-run-1")


def test_strategy_reader_fails_closed_on_decision_chain_head_mismatch(tmp_path: Path) -> None:
    state_root = tmp_path / "strategy-state"
    _write_strategy_checkpoint(state_root, decision_chain_head="4" * 64)
    reader = StrategyFactoryStateReader(state_root)

    with pytest.raises(StrategyRunCorrupted):
        reader.get("factory-run-1")
