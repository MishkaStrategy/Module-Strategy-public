from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from modular_control_plane import (
    BacktestControlService,
    BacktestExecution,
    BacktestInspection,
    BacktestMetricChange,
    ComponentHealth,
    ComponentId,
    ControlPlaneService,
    HealthStatus,
    SQLiteBacktestJobStore,
    StrategyControlService,
)
from modular_control_plane.api import create_api
from modular_control_plane.backtest import BacktestComparisonData
from modular_control_plane.strategy import (
    StrategyFactoryScan,
    StrategyFactorySnapshot,
    StrategyRunNotFound,
)

NOW = datetime(2026, 8, 7, 4, 15, tzinfo=UTC)


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
            summary="ready",
        )


class FakeBacktestRunner:
    def __init__(self, root: Path) -> None:
        self._root = root

    def run(self, config_id: str) -> BacktestExecution:
        result = self._root / f"run-{config_id}"
        result.mkdir(parents=True, exist_ok=True)
        bars = 100 if config_id == "left" else 110
        return BacktestExecution(
            run_id=result.name,
            output_directory=result,
            bars_processed=bars,
            restart_verified=False,
        )

    def inspect(self, result_directory: Path) -> BacktestInspection:
        value = 0.1 if result_directory.name == "run-left" else 0.2
        return BacktestInspection(run_id=result_directory.name, metrics={"net_return": value})

    def compare(self, left: Path, right: Path) -> BacktestComparisonData:
        return BacktestComparisonData(
            left_run_id=left.name,
            right_run_id=right.name,
            changes={
                "net_return": BacktestMetricChange(left=0.1, right=0.2, delta=0.1),
            },
        )


class FakeStrategyReader:
    def __init__(self) -> None:
        self.snapshot = StrategyFactorySnapshot(
            run_id="factory-run-1",
            factory_status="COMPLETED",
            stage="package",
            stage_sequence=14,
            updated_at=NOW,
            request_fingerprint="a" * 64,
            dataset_fingerprint="b" * 64,
            commit_sha="c" * 40,
            manifest_id="manifest-1",
            completed_artifact_count=3,
            submitted_batch_count=2,
            processed_event_count=9,
            decision_count=4,
            decision_chain_head="d" * 64,
            decision_chain_verified=True,
        )

    def scan(self, *, max_runs: int) -> StrategyFactoryScan:
        assert max_runs >= 1
        return StrategyFactoryScan(runs=(self.snapshot,))

    def get(self, run_id: str) -> StrategyFactorySnapshot:
        if run_id != self.snapshot.run_id:
            raise StrategyRunNotFound("missing")
        return self.snapshot


def backtest_control(tmp_path: Path) -> BacktestControlService:
    return BacktestControlService(
        SQLiteBacktestJobStore(tmp_path / "backtests.sqlite3", clock=fixed_clock),
        FakeBacktestRunner(tmp_path / "results"),
        clock=fixed_clock,
    )


def strategy_control() -> StrategyControlService:
    return StrategyControlService(FakeStrategyReader(), clock=fixed_clock)


def authorize_operator(request: Request) -> str:
    if request.headers.get("authorization") != "Bearer operator-token":
        raise HTTPException(status_code=401, detail="operator authentication required")
    return "local-operator"


def test_version_is_explicitly_paper_only_and_local_deployment_scoped() -> None:
    client = TestClient(create_api(ControlPlaneService(clock=fixed_clock), clock=fixed_clock))

    response = client.get("/api/control/v1/version")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "api_version": "v1",
        "mode": "PAPER",
        "remote_access_allowed": False,
    }


def test_system_health_projects_real_service_state() -> None:
    service = ControlPlaneService(
        [
            StaticAdapter(ComponentId.MARKET_DATA),
            StaticAdapter(ComponentId.RESEARCH, HealthStatus.DEGRADED),
        ],
        clock=fixed_clock,
    )
    client = TestClient(create_api(service, clock=fixed_clock))

    response = client.get("/api/control/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "PAPER"
    assert payload["status"] == "DEGRADED"
    assert [item["component"] for item in payload["components"]] == ["market_data", "research"]


def test_component_health_returns_fail_closed_missing_adapter_projection() -> None:
    service = ControlPlaneService(
        required_components=(ComponentId.PAPER_RUNTIME,),
        clock=fixed_clock,
    )
    client = TestClient(create_api(service, clock=fixed_clock))

    response = client.get("/api/control/v1/components/paper_runtime/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "UNAVAILABLE"
    assert payload["details"]["reason"] == "adapter_not_registered"


def test_unknown_component_has_actionable_typed_error() -> None:
    client = TestClient(create_api(ControlPlaneService(clock=fixed_clock), clock=fixed_clock))

    response = client.get("/api/control/v1/components/not-a-component/health")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    error = response.json()["error"]
    assert error["code"] == "CONTROL_COMPONENT_UNKNOWN"
    assert error["operation"] == "component_health"
    assert error["action"]
    assert error["retryable"] is False
    assert error["error_id"].startswith("ctrl_")
    assert error["technical_details"] == {"requested_component": "not-a-component"}


def test_service_level_failure_is_redacted_to_error_type() -> None:
    secret = "TOP-SECRET should not be rendered"

    def broken_clock() -> datetime:
        raise RuntimeError(secret)

    service = ControlPlaneService(clock=broken_clock)
    client = TestClient(create_api(service, clock=fixed_clock))

    response = client.get("/api/control/v1/health")

    assert response.status_code == 503
    rendered = response.text
    error = response.json()["error"]
    assert error["code"] == "CONTROL_PLANE_UNAVAILABLE"
    assert error["retryable"] is True
    assert error["technical_details"] == {"error_type": "RuntimeError"}
    assert secret not in rendered
    assert "TOP-SECRET" not in rendered


def test_injected_reader_authorizer_guards_every_read_endpoint() -> None:
    seen: list[str] = []

    def authorize(request: Request) -> None:
        seen.append(request.url.path)
        if request.headers.get("authorization") != "Bearer reader-token":
            raise HTTPException(status_code=401, detail="reader authentication required")

    client = TestClient(
        create_api(
            ControlPlaneService(clock=fixed_clock),
            authorize_reader=authorize,
            clock=fixed_clock,
        )
    )

    assert client.get("/api/control/v1/version").status_code == 401
    assert (
        client.get(
            "/api/control/v1/version",
            headers={"Authorization": "Bearer reader-token"},
        ).status_code
        == 200
    )
    assert client.get("/api/control/v1/health").status_code == 401
    assert client.get("/api/control/v1/components/backtest/health").status_code == 401
    assert client.get("/api/control/v1/strategy/runs").status_code == 401
    assert client.get("/api/control/v1/backtests").status_code == 401
    assert seen == [
        "/api/control/v1/version",
        "/api/control/v1/version",
        "/api/control/v1/health",
        "/api/control/v1/components/backtest/health",
        "/api/control/v1/strategy/runs",
        "/api/control/v1/backtests",
    ]


def test_strategy_api_lists_and_reads_governance_without_paths() -> None:
    control = strategy_control()
    client = TestClient(
        create_api(
            ControlPlaneService([control], clock=fixed_clock),
            strategy_control=control,
            clock=fixed_clock,
        )
    )

    listed = client.get("/api/control/v1/strategy/runs")
    single = client.get("/api/control/v1/strategy/runs/factory-run-1")

    assert listed.status_code == 200
    assert listed.json()["total_valid"] == 1
    assert listed.json()["runs"][0]["factory_status"] == "COMPLETED"
    assert listed.json()["runs"][0]["status"] == "SUCCEEDED"
    assert listed.json()["runs"][0]["decision_chain_verified"] is True
    assert single.status_code == 200
    assert single.json()["stage"] == "package"
    assert "path" not in listed.text.lower()


def test_strategy_api_returns_typed_missing_and_unconfigured_errors() -> None:
    unconfigured = TestClient(create_api(ControlPlaneService(clock=fixed_clock), clock=fixed_clock))
    assert unconfigured.get("/api/control/v1/strategy/runs").json()["error"]["code"] == (
        "CONTROL_STRATEGY_NOT_CONFIGURED"
    )

    control = strategy_control()
    configured = TestClient(
        create_api(
            ControlPlaneService([control], clock=fixed_clock),
            strategy_control=control,
            clock=fixed_clock,
        )
    )
    missing = configured.get("/api/control/v1/strategy/runs/missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CONTROL_STRATEGY_RUN_NOT_FOUND"


def test_backtest_mutation_fails_closed_without_operator_authorizer(tmp_path: Path) -> None:
    control = backtest_control(tmp_path)
    client = TestClient(
        create_api(
            ControlPlaneService([control], clock=fixed_clock),
            backtest_control=control,
            submit_backtest=control.run_job,
            clock=fixed_clock,
        )
    )

    response = client.post(
        "/api/control/v1/backtests",
        headers={"Idempotency-Key": "run-one"},
        json={"schema_version": 1, "config_id": "left"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "CONTROL_OPERATOR_AUTH_NOT_CONFIGURED"
    assert response.headers["cache-control"] == "no-store"


def test_backtest_mutation_fails_closed_without_durable_executor(tmp_path: Path) -> None:
    control = backtest_control(tmp_path)
    client = TestClient(
        create_api(
            ControlPlaneService([control], clock=fixed_clock),
            authorize_operator=authorize_operator,
            backtest_control=control,
            clock=fixed_clock,
        )
    )

    response = client.post(
        "/api/control/v1/backtests",
        headers={
            "Authorization": "Bearer operator-token",
            "Idempotency-Key": "run-one",
        },
        json={"schema_version": 1, "config_id": "left"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_BACKTEST_EXECUTOR_NOT_CONFIGURED"
    assert control.list_jobs() == ()


def test_backtest_api_runs_durable_job_reads_result_and_compares(tmp_path: Path) -> None:
    control = backtest_control(tmp_path)
    service = ControlPlaneService([control], clock=fixed_clock)
    client = TestClient(
        create_api(
            service,
            authorize_operator=authorize_operator,
            backtest_control=control,
            submit_backtest=control.run_job,
            clock=fixed_clock,
        )
    )

    left_response = client.post(
        "/api/control/v1/backtests",
        headers={
            "Authorization": "Bearer operator-token",
            "Idempotency-Key": "left-run",
        },
        json={"schema_version": 1, "config_id": "left"},
    )
    right_response = client.post(
        "/api/control/v1/backtests",
        headers={
            "Authorization": "Bearer operator-token",
            "Idempotency-Key": "right-run",
        },
        json={"schema_version": 1, "config_id": "right"},
    )

    assert left_response.status_code == 202
    assert right_response.status_code == 202
    left_id = left_response.json()["record"]["job"]["job_id"]
    right_id = right_response.json()["record"]["job"]["job_id"]

    left_job = client.get(f"/api/control/v1/backtests/{left_id}")
    assert left_job.status_code == 200
    assert left_job.json()["job"]["status"] == "SUCCEEDED"
    assert left_job.json()["result"]["run_id"] == "run-left"

    result = client.get(f"/api/control/v1/backtests/{left_id}/result")
    assert result.status_code == 200
    assert result.json()["metrics"] == {"net_return": 0.1}
    assert "output_directory" not in result.text
    assert str(tmp_path) not in result.text

    comparison = client.post(
        "/api/control/v1/backtests/compare",
        json={
            "schema_version": 1,
            "left_job_id": left_id,
            "right_job_id": right_id,
        },
    )
    assert comparison.status_code == 200
    assert comparison.json()["changes"]["net_return"]["delta"] == 0.1


def test_backtest_api_idempotency_reuses_identical_request_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    control = backtest_control(tmp_path)
    client = TestClient(
        create_api(
            ControlPlaneService([control], clock=fixed_clock),
            authorize_operator=authorize_operator,
            backtest_control=control,
            submit_backtest=control.run_job,
            clock=fixed_clock,
        )
    )
    headers = {
        "Authorization": "Bearer operator-token",
        "Idempotency-Key": "stable-key",
    }

    first = client.post(
        "/api/control/v1/backtests",
        headers=headers,
        json={"schema_version": 1, "config_id": "left"},
    )
    repeated = client.post(
        "/api/control/v1/backtests",
        headers=headers,
        json={"schema_version": 1, "config_id": "left"},
    )
    conflict = client.post(
        "/api/control/v1/backtests",
        headers=headers,
        json={"schema_version": 1, "config_id": "right"},
    )

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json()["created"] is False
    assert repeated.json()["record"]["job"]["job_id"] == first.json()["record"]["job"]["job_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONTROL_BACKTEST_IDEMPOTENCY_CONFLICT"


def test_backtest_scheduler_failure_leaves_retryable_durable_queue(tmp_path: Path) -> None:
    control = backtest_control(tmp_path)
    service = ControlPlaneService([control], clock=fixed_clock)
    secret = "TOP-SECRET scheduler detail"

    def broken_submitter(_job_id: str) -> None:
        raise RuntimeError(secret)

    failed_client = TestClient(
        create_api(
            service,
            authorize_operator=authorize_operator,
            backtest_control=control,
            submit_backtest=broken_submitter,
            clock=fixed_clock,
        )
    )
    headers = {
        "Authorization": "Bearer operator-token",
        "Idempotency-Key": "recoverable-queue",
    }

    failed = failed_client.post(
        "/api/control/v1/backtests",
        headers=headers,
        json={"schema_version": 1, "config_id": "left"},
    )
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "CONTROL_BACKTEST_EXECUTOR_UNAVAILABLE"
    assert failed.json()["error"]["retryable"] is True
    assert secret not in failed.text
    queued = control.list_jobs()
    assert len(queued) == 1
    assert queued[0].job.status.value == "QUEUED"

    recovery_client = TestClient(
        create_api(
            service,
            authorize_operator=authorize_operator,
            backtest_control=control,
            submit_backtest=control.run_job,
            clock=fixed_clock,
        )
    )
    recovered = recovery_client.post(
        "/api/control/v1/backtests",
        headers=headers,
        json={"schema_version": 1, "config_id": "left"},
    )
    assert recovered.status_code == 202
    assert recovered.json()["created"] is False
    assert control.list_jobs()[0].job.status.value == "SUCCEEDED"


def test_backtest_reads_fail_closed_when_component_is_not_configured() -> None:
    client = TestClient(create_api(ControlPlaneService(clock=fixed_clock), clock=fixed_clock))

    response = client.get("/api/control/v1/backtests")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_BACKTEST_NOT_CONFIGURED"
