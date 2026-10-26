from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest

from modular_control_plane import ComponentHealth, ComponentId, HealthStatus, SystemOverview
from modular_control_plane.api_contracts import ApiError, ApiErrorDetail
from modular_control_plane.backtest import BacktestJobRecord, BacktestResultSummary
from modular_control_plane.cli import main
from modular_control_plane.client import (
    ClientSettings,
    ControlClient,
    ControlClientError,
    Transport,
    validate_control_base_url,
)
from modular_control_plane.contracts import JobStatus, JobSummary
from modular_control_plane.evidence import (
    AuditDecisionList,
    AuditEvidenceList,
    EvidenceOverview,
    ResultsEvidenceList,
)
from modular_control_plane.paper import (
    PaperBlockedScopeList,
    PaperExecutionNativeLifecycle,
    PaperKillSwitch,
    PaperKillSwitchScope,
    PaperOrderList,
    PaperRuntimeNativeState,
    PaperRuntimeSummary,
)
from modular_control_plane.research import (
    ResearchExperimentList,
    ResearchRunList,
)
from modular_control_plane.strategy import (
    StrategyFactoryStage,
    StrategyFactoryStatus,
    StrategyRunList,
    StrategyRunSummary,
)

NOW = datetime(2026, 8, 7, 4, 40, tzinfo=UTC)


def _overview(status: HealthStatus = HealthStatus.HEALTHY) -> SystemOverview:
    return SystemOverview(
        status=status,
        checked_at=NOW,
        components=(
            ComponentHealth(
                component=ComponentId.MARKET_DATA,
                status=HealthStatus.HEALTHY,
                checked_at=NOW,
                summary="Market Data is healthy",
            ),
        ),
    )


def _strategy_run() -> StrategyRunSummary:
    return StrategyRunSummary(
        run_id="strategy-run-1",
        factory_status=StrategyFactoryStatus.RUNNING,
        status=JobStatus.RUNNING,
        stage=StrategyFactoryStage.ROBUSTNESS,
        stage_sequence=10,
        updated_at=NOW,
        request_fingerprint="1" * 64,
        dataset_fingerprint="2" * 64,
        commit_sha="3" * 40,
        manifest_id="manifest-1",
        completed_artifact_count=4,
        submitted_batch_count=5,
        processed_event_count=6,
        decision_count=7,
        decision_chain_head="4" * 64,
        decision_chain_verified=True,
    )


def _strategy_runs() -> StrategyRunList:
    return StrategyRunList(
        runs=(_strategy_run(),),
        total_valid=1,
        invalid_count=0,
        ignored_count=0,
        truncated=False,
    )


def _backtest_record() -> BacktestJobRecord:
    return BacktestJobRecord(
        job=JobSummary(
            job_id="bt_11111111111111111111",
            component=ComponentId.BACKTEST,
            operation="run_backtest",
            status=JobStatus.RUNNING,
            phase="execute",
            created_at=NOW,
            updated_at=NOW,
            started_at=NOW,
        ),
        config_id="canonical-baseline",
    )


def _backtest_result() -> BacktestResultSummary:
    return BacktestResultSummary(
        job_id="bt_11111111111111111111",
        run_id="run-1",
        bars_processed=721_597,
        restart_verified=True,
        metrics={"profit_factor": 1.3283, "trades": 438},
    )


def _paper_runtime() -> PaperRuntimeSummary:
    return PaperRuntimeSummary(
        runtime_id="runtime-1",
        portfolio_id="portfolio-1",
        strategy_id="strategy-candidate-1",
        native_state=PaperRuntimeNativeState.RUNNING,
        base_timeframe="5m",
        market="binance-usdtm",
        instruments=("BTCUSDT",),
        configuration_hash="5" * 64,
        kill_switch=PaperKillSwitch(
            active=False,
            scope=PaperKillSwitchScope.GLOBAL,
            activated_at=None,
            cancellation_pending=False,
        ),
        health_ready=True,
        queue_depth=0,
        queue_capacity=1024,
        queue_high_water_mark=4,
        paper_execution_lifecycle=PaperExecutionNativeLifecycle.READY,
        order_count=3,
        active_order_count=1,
        blocked_scope_count=0,
    )


def _research_experiments(*, limit: int = 50, offset: int = 0) -> ResearchExperimentList:
    return ResearchExperimentList(
        experiments=(),
        offset=offset,
        limit=limit,
        has_more=False,
    )


def _research_runs(experiment_id: str, *, limit: int = 100, offset: int = 0) -> ResearchRunList:
    return ResearchRunList(
        experiment_id=experiment_id,
        runs=(),
        offset=offset,
        limit=limit,
        has_more=False,
    )


def _evidence_overview() -> EvidenceOverview:
    return EvidenceOverview(
        audit_configured=False,
        results_registry_configured=False,
        audit_integrity=None,
        results_registry_revision=None,
        results_evidence_count=None,
    )


def test_client_uses_fixed_loopback_path_and_authorization_without_rendering_token() -> None:
    token = "terminal-reader-secret"
    seen: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        seen.append((url, headers, timeout))
        return 200, _overview().model_dump_json().encode()

    client = ControlClient(
        ClientSettings(reader_token=token, timeout_seconds=1.25),
        transport=transport,
    )
    overview = client.status()

    assert overview.status is HealthStatus.HEALTHY
    assert seen == [
        (
            "http://127.0.0.1:8765/api/control/v1/health",
            {"Accept": "application/json", "Authorization": f"Bearer {token}"},
            1.25,
        )
    ]
    assert token not in repr(client)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://192.168.1.2:8765",
        "http://example.com:8765",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1:8765/api",
        "http://127.0.0.1:0",
    ],
)
def test_client_rejects_nonlocal_or_ambiguous_url(url: str) -> None:
    with pytest.raises(ValueError):
        validate_control_base_url(url)


def test_client_maps_typed_api_error_without_technical_detail_leakage() -> None:
    error = ApiError(
        error=ApiErrorDetail(
            code="CONTROL_PLANE_UNAVAILABLE",
            message="State is unavailable.",
            action="Open logs.",
            error_id="ctrl_test",
            operation="system_health",
            occurred_at=NOW,
            retryable=True,
            technical_details={"internal_secret": "must-not-reach-cli-error"},
        )
    )

    def transport(_url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        return 503, error.model_dump_json().encode()

    client = ControlClient(transport=transport)
    with pytest.raises(ControlClientError) as captured:
        client.status()

    assert captured.value.code == "CONTROL_PLANE_UNAVAILABLE"
    assert captured.value.action == "Open logs."
    assert "must-not-reach-cli-error" not in str(captured.value)


def test_client_maps_typed_reader_auth_error_to_actionable_cli_failure() -> None:
    error = ApiError(
        error=ApiErrorDetail(
            code="CONTROL_READER_AUTH_REQUIRED",
            message="Control Plane reader authentication is required.",
            action="Provide the configured Control reader token and retry the read.",
            error_id="ctrl_auth",
            operation="reader_authentication",
            occurred_at=NOW,
            retryable=True,
            technical_details={},
        )
    )

    client = ControlClient(
        transport=lambda _url, _headers, _timeout: (401, error.model_dump_json().encode())
    )
    with pytest.raises(ControlClientError) as captured:
        client.status()

    assert captured.value.code == "CONTROL_READER_AUTH_REQUIRED"
    assert captured.value.status_code == 401
    assert (
        captured.value.action == "Provide the configured Control reader token and retry the read."
    )


def test_client_rejects_malformed_success_contract() -> None:
    client = ControlClient(transport=lambda _url, _headers, _timeout: (200, b'{"mode":"LIVE"}'))

    with pytest.raises(ControlClientError) as captured:
        client.status()
    assert captured.value.code == "CONTROL_RESPONSE_INVALID"


def test_client_reads_strategy_backtest_and_paper_through_same_loopback_transport() -> None:
    seen: list[str] = []

    def transport(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        seen.append(url)
        if "/strategy/runs?" in url:
            return 200, _strategy_runs().model_dump_json().encode()
        if "/backtests?" in url:
            payload = [_backtest_record().model_dump(mode="json")]
            return 200, json.dumps(payload).encode()
        if url.endswith("/paper/runtime"):
            return 200, _paper_runtime().model_dump_json().encode()
        raise AssertionError(f"unexpected URL: {url}")

    client = ControlClient(transport=transport)

    assert client.strategy_runs(limit=10, offset=5).runs[0].run_id == "strategy-run-1"
    assert client.backtests(limit=20, offset=2)[0].job.status is JobStatus.RUNNING
    assert client.paper_runtime().execution_mode == "paper"
    assert seen == [
        "http://127.0.0.1:8765/api/control/v1/strategy/runs?limit=10&offset=5",
        "http://127.0.0.1:8765/api/control/v1/backtests?limit=20&offset=2",
        "http://127.0.0.1:8765/api/control/v1/paper/runtime",
    ]


def test_client_reads_research_and_evidence_through_same_loopback_transport() -> None:
    seen: list[str] = []

    def transport(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        seen.append(url)
        if "/research/experiments?" in url:
            return 200, _research_experiments(limit=25, offset=3).model_dump_json().encode()
        if url.endswith("/evidence/overview"):
            return 200, _evidence_overview().model_dump_json().encode()
        raise AssertionError(f"unexpected URL: {url}")

    client = ControlClient(transport=transport)

    assert client.research_experiments(limit=25, offset=3).experiments == ()
    assert client.evidence_overview().audit_configured is False
    assert seen == [
        "http://127.0.0.1:8765/api/control/v1/research/experiments?limit=25&offset=3",
        "http://127.0.0.1:8765/api/control/v1/evidence/overview",
    ]


def test_client_rejects_invalid_pagination_before_transport() -> None:
    calls = 0

    def transport(_url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        return 200, b"{}"

    client = ControlClient(transport=transport)
    with pytest.raises(ValueError):
        client.backtests(limit=0)
    with pytest.raises(ValueError):
        client.paper_orders(offset=-1)
    with pytest.raises(ValueError):
        client.research_experiments(limit=201)
    with pytest.raises(ValueError):
        client.audit_decisions(offset=-1)
    assert calls == 0


class _FakeHealthyClient:
    def __init__(self, _settings: ClientSettings) -> None:
        pass

    def status(self) -> SystemOverview:
        return _overview()

    def component_health(self, component: ComponentId) -> ComponentHealth:
        return ComponentHealth(
            component=component,
            status=HealthStatus.DEGRADED,
            checked_at=NOW,
            summary="degraded for test",
        )

    def strategy_runs(self, *, limit: int = 50, offset: int = 0) -> StrategyRunList:
        assert limit >= 1
        assert offset >= 0
        return _strategy_runs()

    def strategy_run(self, run_id: str) -> StrategyRunSummary:
        assert run_id == "strategy-run-1"
        return _strategy_run()

    def backtests(self, *, limit: int = 50, offset: int = 0) -> tuple[BacktestJobRecord, ...]:
        assert limit >= 1
        assert offset >= 0
        return (_backtest_record(),)

    def backtest(self, job_id: str) -> BacktestJobRecord:
        assert job_id == "bt_11111111111111111111"
        return _backtest_record()

    def backtest_result(self, job_id: str) -> BacktestResultSummary:
        assert job_id == "bt_11111111111111111111"
        return _backtest_result()

    def research_experiments(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> ResearchExperimentList:
        return _research_experiments(limit=limit, offset=offset)

    def research_runs(
        self,
        experiment_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ResearchRunList:
        return _research_runs(experiment_id, limit=limit, offset=offset)

    def paper_runtime(self) -> PaperRuntimeSummary:
        return _paper_runtime()

    def paper_orders(self, *, limit: int = 50, offset: int = 0) -> PaperOrderList:
        assert limit >= 1
        assert offset >= 0
        return PaperOrderList(orders=(), limit=limit, offset=offset, has_more=False)

    def paper_blocked_scopes(self) -> PaperBlockedScopeList:
        return PaperBlockedScopeList(scopes=())

    def evidence_overview(self) -> EvidenceOverview:
        return _evidence_overview()

    def audit_decisions(self, *, limit: int = 50, offset: int = 0) -> AuditDecisionList:
        return AuditDecisionList(decisions=(), limit=limit, offset=offset, has_more=False)

    def audit_evidence(self, *, limit: int = 50, offset: int = 0) -> AuditEvidenceList:
        return AuditEvidenceList(evidence=(), limit=limit, offset=offset, has_more=False)

    def results_evidence(self, *, limit: int = 50, offset: int = 0) -> ResultsEvidenceList:
        return ResultsEvidenceList(
            evidence=(),
            registry_revision=0,
            limit=limit,
            offset=offset,
            has_more=False,
        )


def test_cli_status_human_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["status"])
    captured = capsys.readouterr()

    assert code == 0
    assert "SYSTEM  HEALTHY" in captured.out
    assert "MODE    PAPER" in captured.out
    assert "MARKET_DATA" in captured.out
    assert captured.err == ""


def test_cli_component_health_json_uses_degraded_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["health", "paper_runtime", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 2
    assert payload["component"] == "paper_runtime"
    assert payload["status"] == "DEGRADED"
    assert captured.err == ""


def test_cli_strategy_list_json_uses_control_plane_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["strategy", "list", "--limit", "10", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["runs"][0]["run_id"] == "strategy-run-1"
    assert payload["runs"][0]["factory_status"] == "RUNNING"
    assert captured.err == ""


def test_cli_backtest_list_human_output_is_lightweight_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["backtest", "list"])
    captured = capsys.readouterr()

    assert code == 0
    assert "BACKTEST JOBS" in captured.out
    assert "bt_11111111111111111111" in captured.out
    assert "RUNNING" in captured.out
    assert "canonical-baseline" in captured.out
    assert captured.err == ""


def test_cli_research_experiments_json_uses_readonly_owner_projection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["research", "experiments", "--limit", "25", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["experiments"] == []
    assert payload["limit"] == 25
    assert captured.err == ""


def test_cli_paper_status_makes_paper_mode_and_mutation_boundary_explicit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["paper", "status"])
    captured = capsys.readouterr()

    assert code == 0
    assert "MODE       PAPER" in captured.out
    assert "STATE      running" in captured.out
    assert "KILL       INACTIVE" in captured.out
    assert "MUTATIONS  UNAVAILABLE" in captured.out
    assert captured.err == ""


def test_cli_evidence_overview_is_truthful_when_sources_are_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("modular_control_plane.cli.ControlClient", _FakeHealthyClient)

    code = main(["evidence", "overview"])
    captured = capsys.readouterr()

    assert code == 0
    assert "AUDIT      NOT_CONFIGURED" in captured.out
    assert "RESULTS    NOT_CONFIGURED" in captured.out
    assert captured.err == ""


def test_cli_transport_error_is_safe_and_has_stable_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingClient:
        def __init__(self, _settings: ClientSettings) -> None:
            pass

        def status(self) -> SystemOverview:
            raise ControlClientError(
                "CONTROL_TRANSPORT_FAILED",
                "Could not connect to the local Control Plane.",
                action="Start modular-control-api.",
            )

    monkeypatch.setattr("modular_control_plane.cli.ControlClient", FailingClient)

    code = main(["status", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert code == 4
    assert captured.out == ""
    assert payload["error"]["code"] == "CONTROL_TRANSPORT_FAILED"
    assert "secret" not in captured.err.lower()


def test_transport_type_remains_callable_contract() -> None:
    transport = cast(Transport, lambda _url, _headers, _timeout: (200, b"{}"))
    assert callable(transport)
