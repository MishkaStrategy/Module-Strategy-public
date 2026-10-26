from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .bootstrap import (
    ProductionSystemAcceptanceProcess,
    SystemAcceptanceBootstrapConfig,
    SystemAcceptanceProductionBindings,
    build_production_system_acceptance_process,
)
from .contracts import AcceptanceErrorCode, AcceptanceOwnerError
from .executors import CheckpointedSoakExecutor
from .owner import PhaseExecutionContext
from .production_backtest import BacktestRunProof
from .production_backtest_config_identity import BacktestExecutionIdentity
from .production_config_identity import AcceptanceConfigurationMaterial
from .production_dataset import RegisteredDatasetSelectionPort
from .production_identity import require_identifier, require_sha256
from .production_runtime import RuntimeSmokeProof


class StrategyPackageIdentityPort(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def package_hash(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RuntimeConfigurationIdentity:
    """Safe projection of the authoritative PAPER Trading Runner configuration."""

    config_digest: str
    runtime_id: str
    portfolio_id: str
    strategy_id: str
    execution_mode: str

    def __post_init__(self) -> None:
        require_sha256(self.config_digest, "Trading Runner configuration digest")
        require_identifier(self.runtime_id, "Trading Runner runtime_id")
        require_identifier(self.portfolio_id, "Trading Runner portfolio_id")
        require_identifier(self.strategy_id, "Trading Runner strategy_id")
        if self.execution_mode != "paper":
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PAPER_ONLY,
                "Stage-1 Trading Runner configuration must be PAPER-only",
            )


StrategyPackageVerifier = Callable[[StrategyPackageIdentityPort], object]
BacktestExecutionIdentityProbe = Callable[[], BacktestExecutionIdentity]
RuntimeConfigurationIdentityProbe = Callable[[], RuntimeConfigurationIdentity]


def bind_stage1_production_sources(
    *,
    material: AcceptanceConfigurationMaterial,
    bindings: SystemAcceptanceProductionBindings,
    strategy_package: StrategyPackageIdentityPort,
    verify_strategy_package: StrategyPackageVerifier,
    backtest_execution_identity: BacktestExecutionIdentityProbe,
    runtime_configuration_identity: RuntimeConfigurationIdentityProbe,
) -> SystemAcceptanceProductionBindings:
    """Validate and freeze Stage-1 owner identities before START is possible.

    Concrete deployment supplies the real Strategy Factory verifier, registered
    Market Data resolver, Backtest identity projection, prepared Research owner, and
    read-only Trading Runner configuration projection. This function never accepts
    filesystem roots, commands, imports, or browser-controlled owner selectors.
    """

    if not isinstance(material, AcceptanceConfigurationMaterial):
        raise TypeError("material must be AcceptanceConfigurationMaterial")
    if not isinstance(bindings, SystemAcceptanceProductionBindings):
        raise TypeError("bindings must be SystemAcceptanceProductionBindings")
    if not callable(verify_strategy_package):
        raise TypeError("verify_strategy_package must be callable")
    if not callable(backtest_execution_identity):
        raise TypeError("backtest_execution_identity must be callable")
    if not callable(runtime_configuration_identity):
        raise TypeError("runtime_configuration_identity must be callable")

    _verify_strategy_package(
        material=material,
        package=strategy_package,
        verifier=verify_strategy_package,
    )
    _verify_dataset_binding(material=material, bindings=bindings)
    _verify_backtest_execution(
        material=material,
        identity_probe=backtest_execution_identity,
    )
    _verify_research_binding(material=material, bindings=bindings)
    _verify_runtime_configuration(
        material=material,
        identity_probe=runtime_configuration_identity,
    )

    original_backtest_a = bindings.backtest_a
    original_backtest_b = bindings.backtest_b
    original_recovery_run = bindings.recovery_run
    original_runtime_probe = bindings.runtime_probe

    def backtest_a(context: PhaseExecutionContext) -> BacktestRunProof:
        _verify_backtest_execution(material=material, identity_probe=backtest_execution_identity)
        return original_backtest_a(context)

    def backtest_b(context: PhaseExecutionContext) -> BacktestRunProof:
        _verify_backtest_execution(material=material, identity_probe=backtest_execution_identity)
        return original_backtest_b(context)

    def recovery_run(context: PhaseExecutionContext) -> BacktestRunProof:
        _verify_backtest_execution(material=material, identity_probe=backtest_execution_identity)
        return original_recovery_run(context)

    def runtime_probe() -> RuntimeSmokeProof:
        _verify_runtime_configuration(
            material=material,
            identity_probe=runtime_configuration_identity,
        )
        proof = original_runtime_probe()
        if not isinstance(proof, RuntimeSmokeProof):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "Trading Runner health owner returned an invalid proof",
            )
        if proof.runtime_id != material.trading_runner_runtime_id:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Trading Runner runtime proof differs from Stage-1 deployment identity",
            )
        return proof

    return replace(
        bindings,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery_run=recovery_run,
        runtime_probe=runtime_probe,
    )


def build_stage1_system_acceptance_process(
    *,
    material: AcceptanceConfigurationMaterial,
    bindings: SystemAcceptanceProductionBindings,
    strategy_package: StrategyPackageIdentityPort,
    verify_strategy_package: StrategyPackageVerifier,
    backtest_execution_identity: BacktestExecutionIdentityProbe,
    runtime_configuration_identity: RuntimeConfigurationIdentityProbe,
    soak: CheckpointedSoakExecutor,
    state_path: Path,
    reader_token: str,
    operator_token: str,
    instance_id: str = "system-acceptance-owner",
    port: int = 8781,
    poll_seconds: float = 1.0,
    lease_seconds: int = 30,
    operator_subject: str = "control-plane:local-operator",
    on_error: Callable[[AcceptanceOwnerError], None] | None = None,
) -> ProductionSystemAcceptanceProcess:
    """Construct the baseline 2h PAPER process from trusted deployment sources.

    Stage 1 deliberately hard-codes ``allow_soak_24h=False``. The later 24h stage
    must construct a fresh process/run under its separately authorized deployment
    policy; it cannot be enabled by mutating a Stage-1 request.
    """

    if not isinstance(soak, CheckpointedSoakExecutor):
        raise TypeError("soak must be CheckpointedSoakExecutor")
    frozen_bindings = bind_stage1_production_sources(
        material=material,
        bindings=bindings,
        strategy_package=strategy_package,
        verify_strategy_package=verify_strategy_package,
        backtest_execution_identity=backtest_execution_identity,
        runtime_configuration_identity=runtime_configuration_identity,
    )
    config = SystemAcceptanceBootstrapConfig(
        identity=material.trusted_identity(),
        state_path=state_path,
        instance_id=instance_id,
        reader_token=reader_token,
        operator_token=operator_token,
        port=port,
        poll_seconds=poll_seconds,
        lease_seconds=lease_seconds,
        allow_soak_24h=False,
        operator_subject=operator_subject,
    )
    return build_production_system_acceptance_process(
        config=config,
        bindings=frozen_bindings,
        soak=soak,
        on_error=on_error,
    )


def _verify_strategy_package(
    *,
    material: AcceptanceConfigurationMaterial,
    package: StrategyPackageIdentityPort,
    verifier: StrategyPackageVerifier,
) -> None:
    try:
        strategy_id = package.strategy_id
        package_hash = package.package_hash
    except (AttributeError, TypeError):
        raise TypeError("strategy_package must expose strategy_id and package_hash") from None
    require_identifier(strategy_id, "StrategyPackage strategy_id")
    require_sha256(package_hash, "StrategyPackage package_hash")
    try:
        verifier(package)
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Strategy Factory package verification failed",
        ) from exc
    if strategy_id != material.strategy_id or package_hash != material.strategy_package_hash:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "verified StrategyPackage identity does not match Stage-1 deployment identity",
        )


def _verify_dataset_binding(
    *,
    material: AcceptanceConfigurationMaterial,
    bindings: SystemAcceptanceProductionBindings,
) -> None:
    if bindings.dataset_version != material.dataset_version:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "registered dataset version differs from Stage-1 deployment identity",
        )
    if bindings.dataset_timeframe != material.dataset_timeframe:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "registered dataset timeframe differs from Stage-1 deployment identity",
        )
    selection = bindings.dataset_resolver()
    _verify_registered_selection(material=material, selection=selection)


def _verify_registered_selection(
    *,
    material: AcceptanceConfigurationMaterial,
    selection: RegisteredDatasetSelectionPort,
) -> None:
    try:
        descriptor = selection.descriptor
        dataset_id = descriptor.dataset_id
        version = descriptor.version
        dataset_hash = descriptor.dataset_hash
        timeframe = selection.timeframe
        row_count = selection.row_count
    except (AttributeError, TypeError):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "registered dataset resolver returned an invalid Stage-1 selection",
        ) from None
    require_identifier(dataset_id, "registered dataset_id")
    require_sha256(dataset_hash, "registered dataset digest")
    if (
        dataset_id != material.dataset_id
        or dataset_hash != material.dataset_digest
        or version != material.dataset_version
        or timeframe != material.dataset_timeframe
    ):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "registered dataset selection does not match Stage-1 deployment identity",
        )
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "registered dataset selection has an invalid row count",
        )


def _verify_backtest_execution(
    *,
    material: AcceptanceConfigurationMaterial,
    identity_probe: BacktestExecutionIdentityProbe,
) -> None:
    try:
        observed = identity_probe()
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest execution identity is unavailable",
        ) from exc
    if not isinstance(observed, BacktestExecutionIdentity):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest execution identity projection is invalid",
        )
    expected = (
        material.backtest_config_digest,
        material.backtest_performance_digest,
        material.valuation_policy,
        material.funding_identity_digest,
        material.mark_identity_digest,
    )
    actual = (
        observed.config_digest,
        observed.performance_digest,
        observed.valuation_policy,
        observed.funding_identity_digest,
        observed.mark_identity_digest,
    )
    if actual != expected:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest execution identity differs from Stage-1 deployment identity",
        )


def _verify_research_binding(
    *,
    material: AcceptanceConfigurationMaterial,
    bindings: SystemAcceptanceProductionBindings,
) -> None:
    if bindings.research_specification_fingerprint != material.research_specification_fingerprint:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Research binding fingerprint differs from Stage-1 deployment identity",
        )
    try:
        prepared_fingerprint = bindings.research_prepared.specification.fingerprint
        experiment_id = bindings.research_prepared.manifest.experiment_id
    except (AttributeError, TypeError):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "prepared Research binding is missing frozen identity",
        ) from None
    require_sha256(prepared_fingerprint, "prepared Research specification fingerprint")
    require_identifier(experiment_id, "prepared Research experiment_id")
    if prepared_fingerprint != material.research_specification_fingerprint:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "prepared Research specification differs from Stage-1 deployment identity",
        )
    if experiment_id != material.research_experiment_id:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "prepared Research experiment differs from Stage-1 deployment identity",
        )


def _verify_runtime_configuration(
    *,
    material: AcceptanceConfigurationMaterial,
    identity_probe: RuntimeConfigurationIdentityProbe,
) -> None:
    try:
        observed = identity_probe()
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Trading Runner configuration projection is unavailable",
        ) from exc
    if not isinstance(observed, RuntimeConfigurationIdentity):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Trading Runner configuration projection is invalid",
        )
    expected = (
        material.trading_runner_config_digest,
        material.trading_runner_runtime_id,
        material.trading_runner_portfolio_id,
        material.strategy_id,
        "paper",
    )
    actual = (
        observed.config_digest,
        observed.runtime_id,
        observed.portfolio_id,
        observed.strategy_id,
        observed.execution_mode,
    )
    if actual != expected:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Trading Runner configuration differs from Stage-1 deployment identity",
        )


__all__ = [
    "BacktestExecutionIdentityProbe",
    "RuntimeConfigurationIdentity",
    "RuntimeConfigurationIdentityProbe",
    "StrategyPackageIdentityPort",
    "StrategyPackageVerifier",
    "bind_stage1_production_sources",
    "build_stage1_system_acceptance_process",
]
