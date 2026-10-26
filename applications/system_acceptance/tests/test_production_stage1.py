from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from modular_system_acceptance.bootstrap import SystemAcceptanceProductionBindings
from modular_system_acceptance.contracts import AcceptanceOwnerError
from modular_system_acceptance.executors import CheckpointedSoakExecutor
from modular_system_acceptance.production_backtest import (
    BacktestDeterminismProof,
    BacktestRunProof,
)
from modular_system_acceptance.production_backtest_config_identity import BacktestExecutionIdentity
from modular_system_acceptance.production_config_identity import (
    AcceptanceConfigurationMaterial,
    OwnerContractIdentity,
)
from modular_system_acceptance.production_runtime import RuntimeSmokeProof
from modular_system_acceptance.production_stage1 import (
    RuntimeConfigurationIdentity,
    bind_stage1_production_sources,
    build_stage1_system_acceptance_process,
)
from modular_system_acceptance.production_verification import VerificationProof

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
PACKAGE_HASH = "c" * 64
BACKTEST_CONFIG_DIGEST = "d" * 64
BACKTEST_PERFORMANCE_DIGEST = "e" * 64
RUNNER_CONFIG_DIGEST = "f" * 64
RESEARCH_FINGERPRINT = "1" * 64
FUNDING_DIGEST = "2" * 64
RUNNER_RUNTIME_ID = "paper-stage1"
RUNNER_PORTFOLIO_ID = "portfolio-stage1"
RESEARCH_EXPERIMENT_ID = "experiment-stage1"


@dataclass(frozen=True)
class Descriptor:
    dataset_id: str = "btc-usdtm-5m"
    version: int = 1
    dataset_hash: str = DATASET_DIGEST


@dataclass(frozen=True)
class Selection:
    descriptor: Descriptor = Descriptor()
    timeframe: str = "5m"
    row_count: int = 721_597


@dataclass(frozen=True)
class Package:
    strategy_id: str = "strategy-btc-v1"
    package_hash: str = PACKAGE_HASH


@dataclass(frozen=True)
class Specification:
    fingerprint: str = RESEARCH_FINGERPRINT


@dataclass(frozen=True)
class Manifest:
    experiment_id: str = RESEARCH_EXPERIMENT_ID


@dataclass(frozen=True)
class Prepared:
    specification: Specification = Specification()
    manifest: Manifest = Manifest()


class Repository:
    def experiment_status(self, experiment_id: str) -> str:
        assert experiment_id == RESEARCH_EXPERIMENT_ID
        return "COMPLETED"


@dataclass(frozen=True)
class Summary:
    status: str = "COMPLETED"


class Runner:
    repository = Repository()

    def execute(self, experiment_id: str) -> Summary:
        assert experiment_id == RESEARCH_EXPERIMENT_ID
        return Summary()


def owner_contracts() -> tuple[OwnerContractIdentity, ...]:
    return tuple(
        sorted(
            (
                OwnerContractIdentity("backtest", "application-v1"),
                OwnerContractIdentity("market_data", "registered-dataset-v1"),
                OwnerContractIdentity("research", "prepared-experiment-v1"),
                OwnerContractIdentity("strategy_factory", "strategy-package-v1"),
                OwnerContractIdentity("system_acceptance", "production-bootstrap-v1"),
                OwnerContractIdentity("trading_runner", "paper-runtime-v1"),
            )
        )
    )


def material(**overrides: object) -> AcceptanceConfigurationMaterial:
    values: dict[str, object] = {
        "full_rc_sha": FULL_RC,
        "dataset_id": "btc-usdtm-5m",
        "dataset_digest": DATASET_DIGEST,
        "dataset_version": 1,
        "dataset_timeframe": "5m",
        "strategy_id": "strategy-btc-v1",
        "strategy_package_hash": PACKAGE_HASH,
        "backtest_config_digest": BACKTEST_CONFIG_DIGEST,
        "backtest_performance_digest": BACKTEST_PERFORMANCE_DIGEST,
        "valuation_policy": "last",
        "funding_identity_digest": FUNDING_DIGEST,
        "mark_identity_digest": None,
        "trading_runner_config_digest": RUNNER_CONFIG_DIGEST,
        "trading_runner_runtime_id": RUNNER_RUNTIME_ID,
        "trading_runner_portfolio_id": RUNNER_PORTFOLIO_ID,
        "research_experiment_id": RESEARCH_EXPERIMENT_ID,
        "research_specification_fingerprint": RESEARCH_FINGERPRINT,
        "owner_contracts": owner_contracts(),
    }
    values.update(overrides)
    return AcceptanceConfigurationMaterial(**values)  # type: ignore[arg-type]


def backtest_identity(
    *,
    config_digest: str = BACKTEST_CONFIG_DIGEST,
    performance_digest: str = BACKTEST_PERFORMANCE_DIGEST,
) -> BacktestExecutionIdentity:
    return BacktestExecutionIdentity(
        config_digest=config_digest,
        performance_digest=performance_digest,
        valuation_policy="last",
        funding_identity_digest=FUNDING_DIGEST,
        mark_identity_digest=None,
    )


def runtime_identity(
    *,
    config_digest: str = RUNNER_CONFIG_DIGEST,
    runtime_id: str = RUNNER_RUNTIME_ID,
    portfolio_id: str = RUNNER_PORTFOLIO_ID,
    strategy_id: str = "strategy-btc-v1",
    execution_mode: str = "paper",
) -> RuntimeConfigurationIdentity:
    return RuntimeConfigurationIdentity(
        config_digest=config_digest,
        runtime_id=runtime_id,
        portfolio_id=portfolio_id,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
    )


def bindings(
    config: AcceptanceConfigurationMaterial,
    *,
    selection: Selection | None = None,
    prepared: Prepared | None = None,
    runtime_calls: list[str] | None = None,
    backtest_calls: list[str] | None = None,
    runtime_proof_id: str | None = None,
) -> SystemAcceptanceProductionBindings:
    selected = selection or Selection()
    research_prepared = prepared or Prepared()
    observed_runtime_calls = runtime_calls if runtime_calls is not None else []
    observed_backtest_calls = backtest_calls if backtest_calls is not None else []

    def runtime_probe() -> RuntimeSmokeProof:
        observed_runtime_calls.append("probe")
        return RuntimeSmokeProof(
            runtime_id=runtime_proof_id or config.trading_runner_runtime_id,
            full_rc_sha=FULL_RC,
            config_digest=config.config_digest,
            execution_mode="paper",
            ready=True,
            live=True,
            component_count=6,
            critical_failure_count=0,
        )

    def backtest(_: Any) -> BacktestRunProof:
        observed_backtest_calls.append("run")
        return BacktestRunProof(
            run_id="stage1-backtest",
            artifact_digest="7" * 64,
            full_rc_sha=FULL_RC,
            dataset_digest=DATASET_DIGEST,
            strategy_digest=PACKAGE_HASH,
            config_digest=config.config_digest,
        )

    def unused_compare(_: Any) -> BacktestDeterminismProof:
        raise AssertionError("determinism callback must not execute during assembly")

    def unused_verify() -> VerificationProof:
        raise AssertionError("verification callback must not execute during assembly")

    return SystemAcceptanceProductionBindings(
        environment_verify=unused_verify,
        dataset_resolver=lambda: selected,
        dataset_version=1,
        dataset_timeframe="5m",
        backtest_a=backtest,
        backtest_b=backtest,
        determinism_compare=unused_compare,
        research_runner=cast(Any, Runner()),
        research_prepared=cast(Any, research_prepared),
        research_specification_fingerprint=RESEARCH_FINGERPRINT,
        runtime_probe=runtime_probe,
        recovery_run=backtest,
        final_verify=unused_verify,
    )


def test_bind_stage1_verifies_all_frozen_owner_identities() -> None:
    config = material()
    verified: list[str] = []

    frozen = bind_stage1_production_sources(
        material=config,
        bindings=bindings(config),
        strategy_package=Package(),
        verify_strategy_package=lambda package: verified.append(package.package_hash),
        backtest_execution_identity=backtest_identity,
        runtime_configuration_identity=runtime_identity,
    )

    assert verified == [PACKAGE_HASH]
    assert frozen.dataset_version == 1
    assert frozen.dataset_timeframe == "5m"
    assert frozen.research_specification_fingerprint == RESEARCH_FINGERPRINT


def test_strategy_package_identity_mismatch_fails_before_process() -> None:
    config = material()

    with pytest.raises(AcceptanceOwnerError, match="StrategyPackage identity"):
        bind_stage1_production_sources(
            material=config,
            bindings=bindings(config),
            strategy_package=Package(package_hash="9" * 64),
            verify_strategy_package=lambda _: None,
            backtest_execution_identity=backtest_identity,
            runtime_configuration_identity=runtime_identity,
        )


def test_registered_dataset_mismatch_fails_before_process() -> None:
    config = material()
    wrong = Selection(descriptor=Descriptor(dataset_hash="9" * 64))

    with pytest.raises(AcceptanceOwnerError, match="dataset selection"):
        bind_stage1_production_sources(
            material=config,
            bindings=bindings(config, selection=wrong),
            strategy_package=Package(),
            verify_strategy_package=lambda _: None,
            backtest_execution_identity=backtest_identity,
            runtime_configuration_identity=runtime_identity,
        )


def test_backtest_execution_mismatch_fails_before_process() -> None:
    config = material()

    with pytest.raises(AcceptanceOwnerError, match="Backtest execution identity differs"):
        bind_stage1_production_sources(
            material=config,
            bindings=bindings(config),
            strategy_package=Package(),
            verify_strategy_package=lambda _: None,
            backtest_execution_identity=lambda: backtest_identity(config_digest="9" * 64),
            runtime_configuration_identity=runtime_identity,
        )


def test_backtest_execution_drift_is_rechecked_before_every_run() -> None:
    config = material()
    current = [BACKTEST_CONFIG_DIGEST]
    backtest_calls: list[str] = []
    frozen = bind_stage1_production_sources(
        material=config,
        bindings=bindings(config, backtest_calls=backtest_calls),
        strategy_package=Package(),
        verify_strategy_package=lambda _: None,
        backtest_execution_identity=lambda: backtest_identity(config_digest=current[0]),
        runtime_configuration_identity=runtime_identity,
    )

    proof = frozen.backtest_a(cast(Any, object()))
    assert proof.run_id == "stage1-backtest"
    assert backtest_calls == ["run"]

    current[0] = "6" * 64
    with pytest.raises(AcceptanceOwnerError, match="Backtest execution identity differs"):
        frozen.backtest_b(cast(Any, object()))
    with pytest.raises(AcceptanceOwnerError, match="Backtest execution identity differs"):
        frozen.recovery_run(cast(Any, object()))
    assert backtest_calls == ["run"]


def test_research_fingerprint_mismatch_fails_before_process() -> None:
    config = material(research_specification_fingerprint="8" * 64)

    with pytest.raises(AcceptanceOwnerError, match="Research binding fingerprint"):
        bind_stage1_production_sources(
            material=config,
            bindings=bindings(config),
            strategy_package=Package(),
            verify_strategy_package=lambda _: None,
            backtest_execution_identity=backtest_identity,
            runtime_configuration_identity=runtime_identity,
        )


def test_research_experiment_mismatch_fails_before_process() -> None:
    config = material(research_experiment_id="other-experiment")

    with pytest.raises(AcceptanceOwnerError, match="Research experiment differs"):
        bind_stage1_production_sources(
            material=config,
            bindings=bindings(config),
            strategy_package=Package(),
            verify_strategy_package=lambda _: None,
            backtest_execution_identity=backtest_identity,
            runtime_configuration_identity=runtime_identity,
        )


@pytest.mark.parametrize(
    "observed",
    (
        runtime_identity(config_digest="7" * 64),
        runtime_identity(runtime_id="paper-stage1-other"),
        runtime_identity(portfolio_id="portfolio-stage1-other"),
        runtime_identity(strategy_id="strategy-btc-v2"),
    ),
)
def test_runtime_configuration_mismatch_fails_before_process(
    observed: RuntimeConfigurationIdentity,
) -> None:
    config = material()

    with pytest.raises(AcceptanceOwnerError, match="Trading Runner configuration differs"):
        bind_stage1_production_sources(
            material=config,
            bindings=bindings(config),
            strategy_package=Package(),
            verify_strategy_package=lambda _: None,
            backtest_execution_identity=backtest_identity,
            runtime_configuration_identity=lambda: observed,
        )


def test_runtime_configuration_drift_is_rechecked_on_every_probe() -> None:
    config = material()
    current = [runtime_identity()]
    runtime_calls: list[str] = []
    frozen = bind_stage1_production_sources(
        material=config,
        bindings=bindings(config, runtime_calls=runtime_calls),
        strategy_package=Package(),
        verify_strategy_package=lambda _: None,
        backtest_execution_identity=backtest_identity,
        runtime_configuration_identity=lambda: current[0],
    )

    assert frozen.runtime_probe().ready is True
    assert runtime_calls == ["probe"]

    current[0] = runtime_identity(portfolio_id="portfolio-stage1-other")
    with pytest.raises(AcceptanceOwnerError, match="Trading Runner configuration differs"):
        frozen.runtime_probe()
    assert runtime_calls == ["probe"]


def test_runtime_proof_id_mismatch_fails_closed() -> None:
    config = material()
    frozen = bind_stage1_production_sources(
        material=config,
        bindings=bindings(config, runtime_proof_id="paper-stage1-other"),
        strategy_package=Package(),
        verify_strategy_package=lambda _: None,
        backtest_execution_identity=backtest_identity,
        runtime_configuration_identity=runtime_identity,
    )

    with pytest.raises(AcceptanceOwnerError, match="runtime proof differs"):
        frozen.runtime_probe()


def test_non_paper_runtime_identity_is_rejected() -> None:
    with pytest.raises(AcceptanceOwnerError, match="PAPER-only"):
        runtime_identity(execution_mode="live")


def test_stage1_process_forces_24h_disabled_and_hides_private_state(tmp_path: Path) -> None:
    config = material()
    soak = CheckpointedSoakExecutor(lambda _: True)
    process = build_stage1_system_acceptance_process(
        material=config,
        bindings=bindings(config),
        strategy_package=Package(),
        verify_strategy_package=lambda _: None,
        backtest_execution_identity=backtest_identity,
        runtime_configuration_identity=runtime_identity,
        soak=soak,
        state_path=tmp_path / "acceptance.sqlite3",
        reader_token="reader-token-stage1",
        operator_token="operator-token-stage1",
        port=0,
    )
    try:
        assert process.config.identity == config.trusted_identity()
        assert process.config.allow_soak_24h is False
        rendered = repr(process.config)
        assert "reader-token-stage1" not in rendered
        assert "operator-token-stage1" not in rendered
        assert str(tmp_path) not in rendered
    finally:
        process.close()
