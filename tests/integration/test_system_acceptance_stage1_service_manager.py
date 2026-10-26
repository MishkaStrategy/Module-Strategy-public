from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from modular_research_runner.owner_http import ResearchRunnerOwnerHttpClient
from modular_trading_runner import (
    RecoveredTradingRunnerOwner,
    RuntimeState,
    TradingRunnerOperatorLifecycle,
)
from modular_trading_runner.acceptance_http import TradingRunnerAcceptanceHttpQuery
from scripts import system_acceptance_stage1_service_manager as service_manager_module
from scripts.system_acceptance_stage1_deployment import Stage1DeploymentSources
from scripts.system_acceptance_stage1_host import Stage1MacServiceConfig
from scripts.system_acceptance_stage1_service_manager import (
    ReconstructedStage1Owners,
    Stage1MacServiceManager,
    Stage1MacServiceManagerConfig,
)
from strategy_factory.contracts import StrategyPackage

from tests.integration.test_system_acceptance_stage1_deployment import (
    _FULL_RC_SHA,
    _backtests,
    _prepared_research,
    _registered_dataset,
    _strategy_package,
    _trading_runner,
    _unused_verification,
)

_CANDIDATE_SHA = "fa652760a60043ab0193a75d20f1521b254507b3"
_TRADING_READER = "trading-reader-token-private"
_TRADING_OPERATOR = "trading-operator-token-private"
_RESEARCH_READER = "research-reader-token-private"
_RESEARCH_OPERATOR = "research-operator-token-private"
_ACCEPTANCE_READER = "acceptance-reader-token-private"
_ACCEPTANCE_OPERATOR = "acceptance-operator-token-private"


def test_service_manager_restart_preserves_owner_identity_and_sanitized_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root, dataset_id = _registered_dataset(tmp_path)
    package = _strategy_package()
    research_runner, prepared = _prepared_research(tmp_path, package.strategy_id)
    backtest_a, backtest_b, recovery, performance = _backtests(
        tmp_path,
        storage_root,
        dataset_id,
    )
    state_path = tmp_path / "acceptance.sqlite3"
    proof_path = tmp_path / "private" / "prestart-proof.json"
    config = Stage1MacServiceManagerConfig(
        acceptance=Stage1MacServiceConfig(
            candidate_sha=_CANDIDATE_SHA,
            state_path=state_path,
            reader_token=_ACCEPTANCE_READER,
            operator_token=_ACCEPTANCE_OPERATOR,
            host_wait_seconds=0.05,
        ),
        proof_path=proof_path,
        trading_reader_token=_TRADING_READER,
        trading_operator_token=_TRADING_OPERATOR,
        research_reader_token=_RESEARCH_READER,
        research_operator_token=_RESEARCH_OPERATOR,
    )
    trading_runners = []
    startup_order: list[str] = []
    write_proof = service_manager_module._write_sanitized_proof
    start_host = service_manager_module.Stage1MacServiceHost.start

    def record_proof_write(path: Path, payload: str) -> None:
        startup_order.append("proof")
        write_proof(path, payload)

    def record_host_start(host: Any) -> None:
        startup_order.append("acceptance_start")
        start_host(host)

    monkeypatch.setattr(service_manager_module, "_write_sanitized_proof", record_proof_write)
    monkeypatch.setattr(
        service_manager_module.Stage1MacServiceHost,
        "start",
        record_host_start,
    )

    def load_owners() -> ReconstructedStage1Owners:
        runner = _trading_runner(package.strategy_id)
        trading_runners.append(runner)
        return ReconstructedStage1Owners(
            strategy_package=package,
            research_runner=research_runner,
            research_prepared=prepared,
            trading=RecoveredTradingRunnerOwner(
                runner=runner,
                lifecycle=TradingRunnerOperatorLifecycle(runner),
                stores=cast(Any, object()),
                journal=cast(Any, object()),
            ),
        )

    def build_sources(
        strategy_package: StrategyPackage,
        research_client: ResearchRunnerOwnerHttpClient,
        research_prepared: Any,
        trading_client: TradingRunnerAcceptanceHttpQuery,
    ) -> Stage1DeploymentSources:
        return Stage1DeploymentSources(
            full_rc_sha=_FULL_RC_SHA,
            dataset_id=dataset_id,
            dataset_version=1,
            dataset_timeframe="5m",
            dataset_storage_root=storage_root,
            strategy_package=strategy_package,
            backtest_a_config=backtest_a,
            backtest_b_config=backtest_b,
            recovery_config=recovery,
            performance=performance,
            research_runner=research_client,
            research_prepared=research_prepared,
            trading_runner=trading_client,
            environment_verify=_unused_verification,
            final_verify=_unused_verification,
        )

    first = Stage1MacServiceManager(
        config=config,
        load_owners=load_owners,
        build_sources=build_sources,
    )
    first.start()
    first_proof = proof_path.read_text(encoding="utf-8")
    assert startup_order == ["proof", "acceptance_start"]
    assert first.running
    assert '"allow_soak_24h":false' in first_proof
    assert '"allow_soak_72h":false' in first_proof
    for secret in (
        _TRADING_READER,
        _TRADING_OPERATOR,
        _RESEARCH_READER,
        _RESEARCH_OPERATOR,
        _ACCEPTANCE_READER,
        _ACCEPTANCE_OPERATOR,
        str(tmp_path),
    ):
        assert secret not in first_proof
        assert secret not in repr(config)
    first.close()
    assert not first.running

    second = Stage1MacServiceManager(
        config=config,
        load_owners=load_owners,
        build_sources=build_sources,
    )
    second.start()
    try:
        assert startup_order == [
            "proof",
            "acceptance_start",
            "proof",
            "acceptance_start",
        ]
        assert second.running
        assert proof_path.read_text(encoding="utf-8") == first_proof
        trading = TradingRunnerAcceptanceHttpQuery(
            "http://127.0.0.1:8780",
            reader_token=_TRADING_READER,
        ).snapshot()
        research = ResearchRunnerOwnerHttpClient(
            "http://127.0.0.1:8782",
            experiment_id=prepared.manifest.experiment_id,
            reader_token=_RESEARCH_READER,
            operator_token=_RESEARCH_OPERATOR,
        ).owner_identity()
        assert trading.runtime_id == "stage1-runner"
        assert trading.portfolio_id == "stage1-portfolio"
        assert trading.strategy_id == package.strategy_id
        assert trading.configuration_hash in first_proof
        assert research.experiment_id == prepared.manifest.experiment_id
        assert research.specification_sha256 == prepared.specification.fingerprint
        assert package.package_hash in first_proof
    finally:
        second.close()

    assert not second.running
    assert [runner.state for runner in trading_runners] == [
        RuntimeState.STOPPED,
        RuntimeState.STOPPED,
    ]
