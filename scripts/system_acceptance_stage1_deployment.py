"""Repository-owned Stage-1 System Acceptance deployment composition.

This module deliberately lives outside ``applications/system_acceptance`` so the
System Acceptance owner package remains dependency-free. A deployment/service manager
imports this module inside an environment where the authoritative owner packages are
installed and supplies only server-owned runtime objects and private paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from market_data_ingestion import RegisteredKlineSelection, resolve_registered_kline_dataset
from modular_backtest import (
    BacktestAppConfig,
    RegisteredMarkPriceSource,
    RunOutcome,
    VALUATION_POLICY_LAST,
    run_backtest,
)
from modular_backtest.funding import RegisteredFundingSource
from modular_backtest.results import verify_artifact_manifest
from modular_backtest.scalability import PerformanceConfig
from modular_system_acceptance import (
    STAGE1_OWNER_CONTRACTS_V1,
    AcceptanceConfigurationMaterial,
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    BacktestExecutionIdentity,
    PhaseExecutionContext,
    ProductionSystemAcceptanceProcess,
    ResourceSample,
    RuntimeConfigurationIdentity,
    SystemAcceptanceProductionBindings,
    VerifiedBacktestDeterminismBinding,
    VerifiedBacktestRunBinding,
    build_backtest_execution_identity,
    build_stage1_system_acceptance_process,
)
from modular_system_acceptance.executors import CheckpointedSoakExecutor
from modular_system_acceptance.production_research import (
    PreparedResearchExperimentPort,
    ResearchRunnerPort,
)
from modular_system_acceptance.production_runtime import RuntimeSmokeProof
from modular_system_acceptance.production_stage1 import StrategyPackageIdentityPort
from modular_system_acceptance.production_verification import VerificationProof
from modular_trading_runner import TradingRunner
from modular_trading_runner.acceptance_query import ReadOnlyTradingRuntimeAcceptanceQuery
from strategy_factory.contracts import AdmissionStatus, StrategyPackage
from strategy_factory.package import StrategyPackageBuilder

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TradingRuntimeAcceptanceSnapshotPort(Protocol):
    @property
    def runtime_id(self) -> str: ...

    @property
    def portfolio_id(self) -> str: ...

    @property
    def strategy_id(self) -> str: ...

    @property
    def execution_mode(self) -> str: ...

    @property
    def base_timeframe(self) -> str: ...

    @property
    def configuration_hash(self) -> str: ...

    @property
    def native_state(self) -> str: ...

    @property
    def ready(self) -> bool: ...

    @property
    def live(self) -> bool: ...

    @property
    def component_count(self) -> int: ...

    @property
    def critical_failure_count(self) -> int: ...


class TradingRuntimeAcceptanceQueryPort(Protocol):
    def snapshot(self) -> TradingRuntimeAcceptanceSnapshotPort: ...


@dataclass(frozen=True, slots=True)
class Stage1DeploymentSources:
    """Server-owned authoritative inputs for one immutable Stage-1 composition."""

    full_rc_sha: str
    dataset_id: str
    dataset_version: int
    dataset_timeframe: str
    dataset_storage_root: Path = field(repr=False)
    strategy_package: StrategyPackage = field(repr=False)
    backtest_a_config: BacktestAppConfig = field(repr=False)
    backtest_b_config: BacktestAppConfig = field(repr=False)
    recovery_config: BacktestAppConfig = field(repr=False)
    performance: PerformanceConfig = field(repr=False)
    research_runner: ResearchRunnerPort = field(repr=False)
    research_prepared: PreparedResearchExperimentPort = field(repr=False)
    trading_runner: TradingRuntimeAcceptanceQueryPort | TradingRunner = field(repr=False)
    environment_verify: Callable[[], VerificationProof] = field(repr=False)
    final_verify: Callable[[], VerificationProof] = field(repr=False)
    funding_source: RegisteredFundingSource | None = field(default=None, repr=False)
    valuation_policy: str = VALUATION_POLICY_LAST
    mark_price_source: RegisteredMarkPriceSource | None = field(default=None, repr=False)
    sample_resources: Callable[[], ResourceSample | None] | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class Stage1DeploymentComposition:
    """Frozen safe identity plus the private owner bindings that implement it."""

    material: AcceptanceConfigurationMaterial
    bindings: SystemAcceptanceProductionBindings = field(repr=False)
    strategy_package: StrategyPackage = field(repr=False)
    verify_strategy_package: Callable[[StrategyPackageIdentityPort], object] = field(repr=False)
    backtest_execution_identity: Callable[[], BacktestExecutionIdentity] = field(repr=False)
    runtime_configuration_identity: Callable[[], RuntimeConfigurationIdentity] = field(repr=False)
    soak: CheckpointedSoakExecutor = field(repr=False)

    def build_process(
        self,
        *,
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
        """Create the loopback durable Stage-1 process from this frozen composition."""

        return build_stage1_system_acceptance_process(
            material=self.material,
            bindings=self.bindings,
            strategy_package=self.strategy_package,
            verify_strategy_package=self.verify_strategy_package,
            backtest_execution_identity=self.backtest_execution_identity,
            runtime_configuration_identity=self.runtime_configuration_identity,
            soak=self.soak,
            state_path=state_path,
            reader_token=reader_token,
            operator_token=operator_token,
            instance_id=instance_id,
            port=port,
            poll_seconds=poll_seconds,
            lease_seconds=lease_seconds,
            operator_subject=operator_subject,
            on_error=on_error,
        )


def materialize_stage1_deployment(
    sources: Stage1DeploymentSources,
) -> Stage1DeploymentComposition:
    """Resolve real owner identities and construct the Stage-1 production bindings.

    Research must already be prepared before this function is called. The composition
    never invokes Research preparation and never derives identities from browser input,
    raw paths, or caller-supplied digest strings. Trading Runtime may be supplied as the
    already-owned runner for compatibility or, preferably, as its bounded read-only
    acceptance query; either path observes the same owner and never creates a runtime.
    """

    if not isinstance(sources, Stage1DeploymentSources):
        raise TypeError("sources must be Stage1DeploymentSources")

    package_builder = StrategyPackageBuilder()
    _verify_strategy_package(package_builder, sources.strategy_package)
    selection = _resolve_dataset(sources)
    runtime_query = _trading_runtime_query(sources.trading_runner)
    runtime_snapshot = _runtime_snapshot(runtime_query)
    _verify_cross_owner_strategy_identity(sources, runtime_snapshot)
    _require_distinct_backtest_outputs(sources)

    def resolve_dataset() -> RegisteredKlineSelection:
        return _resolve_dataset(sources)

    def backtest_execution_identity() -> BacktestExecutionIdentity:
        current = resolve_dataset()
        return build_backtest_execution_identity(
            backtest_a_config=sources.backtest_a_config,
            backtest_b_config=sources.backtest_b_config,
            recovery_config=sources.recovery_config,
            performance=sources.performance,
            dataset_id=current.descriptor.dataset_id,
            dataset_digest=current.descriptor.dataset_hash,
            dataset_version=current.descriptor.version,
            dataset_timeframe=current.timeframe,
            valuation_policy=sources.valuation_policy,
            funding_source=sources.funding_source,
            mark_source=sources.mark_price_source,
        )

    backtest_identity = backtest_execution_identity()
    runtime_identity = _runtime_configuration_identity(runtime_snapshot)
    research_experiment_id = sources.research_prepared.manifest.experiment_id
    research_fingerprint = sources.research_prepared.specification.fingerprint
    material = AcceptanceConfigurationMaterial(
        full_rc_sha=sources.full_rc_sha,
        dataset_id=selection.descriptor.dataset_id,
        dataset_digest=selection.descriptor.dataset_hash,
        dataset_version=selection.descriptor.version,
        dataset_timeframe=selection.timeframe,
        strategy_id=sources.strategy_package.strategy_id,
        strategy_package_hash=sources.strategy_package.package_hash,
        backtest_config_digest=backtest_identity.config_digest,
        backtest_performance_digest=backtest_identity.performance_digest,
        valuation_policy=backtest_identity.valuation_policy,
        funding_identity_digest=backtest_identity.funding_identity_digest,
        mark_identity_digest=backtest_identity.mark_identity_digest,
        trading_runner_config_digest=runtime_identity.config_digest,
        trading_runner_runtime_id=runtime_identity.runtime_id,
        trading_runner_portfolio_id=runtime_identity.portfolio_id,
        research_experiment_id=research_experiment_id,
        research_specification_fingerprint=research_fingerprint,
        owner_contracts=STAGE1_OWNER_CONTRACTS_V1,
    )
    identity = material.trusted_identity()

    def run_owner(config: BacktestAppConfig) -> Callable[[PhaseExecutionContext], RunOutcome]:
        def run(context: PhaseExecutionContext) -> RunOutcome:
            del context
            return run_backtest(
                config,
                sources.performance,
                funding_source=sources.funding_source,
                valuation_policy=sources.valuation_policy,
                mark_price_source=sources.mark_price_source,
            )

        return run

    backtest_a = VerifiedBacktestRunBinding(
        identity=identity,
        run=run_owner(sources.backtest_a_config),
        verify_manifest=verify_artifact_manifest,
    )
    backtest_b = VerifiedBacktestRunBinding(
        identity=identity,
        run=run_owner(sources.backtest_b_config),
        verify_manifest=verify_artifact_manifest,
    )
    recovery = VerifiedBacktestRunBinding(
        identity=identity,
        run=run_owner(sources.recovery_config),
        verify_manifest=verify_artifact_manifest,
        require_restart=True,
    )
    determinism = VerifiedBacktestDeterminismBinding(
        left=lambda: _published_backtest_result_directory(
            sources.backtest_a_config.output.directory
        ),
        right=lambda: _published_backtest_result_directory(
            sources.backtest_b_config.output.directory
        ),
        verify_manifest=verify_artifact_manifest,
    )

    def runtime_configuration_identity() -> RuntimeConfigurationIdentity:
        return _runtime_configuration_identity(_runtime_snapshot(runtime_query))

    def runtime_probe() -> RuntimeSmokeProof:
        snapshot = _runtime_snapshot(runtime_query)
        _verify_cross_owner_strategy_identity(sources, snapshot)
        _require_runtime_configuration_identity(snapshot, runtime_identity)
        return RuntimeSmokeProof(
            runtime_id=snapshot.runtime_id,
            full_rc_sha=material.full_rc_sha,
            config_digest=material.config_digest,
            execution_mode=snapshot.execution_mode,
            ready=snapshot.ready,
            live=snapshot.live,
            component_count=snapshot.component_count,
            critical_failure_count=snapshot.critical_failure_count,
        )

    def soak_healthy(context: PhaseExecutionContext) -> bool:
        del context
        if runtime_configuration_identity() != runtime_identity:
            return False
        proof = runtime_probe()
        return proof.ready and proof.live and proof.critical_failure_count == 0

    bindings = SystemAcceptanceProductionBindings(
        environment_verify=sources.environment_verify,
        dataset_resolver=resolve_dataset,
        dataset_version=selection.descriptor.version,
        dataset_timeframe=selection.timeframe,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        determinism_compare=determinism,
        research_runner=sources.research_runner,
        research_prepared=sources.research_prepared,
        research_specification_fingerprint=research_fingerprint,
        runtime_probe=runtime_probe,
        recovery_run=recovery,
        final_verify=sources.final_verify,
    )
    soak = CheckpointedSoakExecutor(
        soak_healthy,
        sample_resources=sources.sample_resources,
    )

    def verify_strategy_package(package: StrategyPackageIdentityPort) -> object:
        if not isinstance(package, StrategyPackage):
            raise TypeError(
                "Stage-1 StrategyPackage must be the authoritative StrategyPackage type"
            )
        _verify_strategy_package(package_builder, package)
        return None

    return Stage1DeploymentComposition(
        material=material,
        bindings=bindings,
        strategy_package=sources.strategy_package,
        verify_strategy_package=verify_strategy_package,
        backtest_execution_identity=backtest_execution_identity,
        runtime_configuration_identity=runtime_configuration_identity,
        soak=soak,
    )


def _verify_strategy_package(builder: StrategyPackageBuilder, package: StrategyPackage) -> None:
    try:
        builder.verify(package)
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Strategy Factory package verification failed",
        ) from exc
    if package.admission_status is not AdmissionStatus.APPROVED_FOR_PAPER:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Stage-1 requires a StrategyPackage approved for PAPER",
        )
    if package.live_trading_authorized:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PAPER_ONLY,
            "Stage-1 StrategyPackage must not authorize LIVE trading",
        )


def _verify_cross_owner_strategy_identity(
    sources: Stage1DeploymentSources,
    runtime: TradingRuntimeAcceptanceSnapshotPort,
) -> None:
    strategy_id = sources.strategy_package.strategy_id
    prepared_strategy_id = getattr(sources.research_prepared.specification, "strategy_id", None)
    if prepared_strategy_id != strategy_id or runtime.strategy_id != strategy_id:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "StrategyPackage, prepared Research and Trading Runner strategy identities differ",
        )
    if runtime.execution_mode != "paper":
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PAPER_ONLY,
            "Stage-1 Trading Runner must be PAPER-only",
        )
    if runtime.base_timeframe != sources.dataset_timeframe:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Trading Runner timeframe differs from the registered Stage-1 dataset",
        )


def _resolve_dataset(sources: Stage1DeploymentSources) -> RegisteredKlineSelection:
    try:
        return resolve_registered_kline_dataset(
            sources.dataset_storage_root,
            sources.dataset_id,
            sources.dataset_version,
            timeframe=sources.dataset_timeframe,
            consumer="modular_backtest",
            deep=True,
        )
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "registered Stage-1 dataset could not be resolved",
        ) from exc


def _trading_runtime_query(
    source: TradingRuntimeAcceptanceQueryPort | TradingRunner,
) -> TradingRuntimeAcceptanceQueryPort:
    if isinstance(source, TradingRunner):
        return ReadOnlyTradingRuntimeAcceptanceQuery(source)
    snapshot = getattr(source, "snapshot", None)
    if not callable(snapshot):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Stage-1 Trading Runtime source must expose a read-only acceptance snapshot",
        )
    return source


def _runtime_snapshot(
    query: TradingRuntimeAcceptanceQueryPort,
) -> TradingRuntimeAcceptanceSnapshotPort:
    try:
        snapshot = query.snapshot()
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Stage-1 Trading Runtime acceptance snapshot is unavailable",
        ) from exc
    for name in (
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "execution_mode",
        "base_timeframe",
        "native_state",
    ):
        value = getattr(snapshot, name, None)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                f"Stage-1 Trading Runtime snapshot has invalid {name}",
            )
    digest = getattr(snapshot, "configuration_hash", None)
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Stage-1 Trading Runtime snapshot has invalid configuration_hash",
        )
    for name in ("ready", "live"):
        if not isinstance(getattr(snapshot, name, None), bool):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                f"Stage-1 Trading Runtime snapshot has invalid {name}",
            )
    for name in ("component_count", "critical_failure_count"):
        value = getattr(snapshot, name, None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                f"Stage-1 Trading Runtime snapshot has invalid {name}",
            )
    if snapshot.execution_mode != "paper":
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PAPER_ONLY,
            "Stage-1 Trading Runtime acceptance snapshot must be PAPER-only",
        )
    if snapshot.component_count < snapshot.critical_failure_count:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Stage-1 Trading Runtime health counts are inconsistent",
        )
    return snapshot


def _runtime_configuration_identity(
    snapshot: TradingRuntimeAcceptanceSnapshotPort,
) -> RuntimeConfigurationIdentity:
    return RuntimeConfigurationIdentity(
        config_digest=snapshot.configuration_hash,
        runtime_id=snapshot.runtime_id,
        portfolio_id=snapshot.portfolio_id,
        strategy_id=snapshot.strategy_id,
        execution_mode=snapshot.execution_mode,
    )


def _require_runtime_configuration_identity(
    snapshot: TradingRuntimeAcceptanceSnapshotPort,
    expected: RuntimeConfigurationIdentity,
) -> None:
    if _runtime_configuration_identity(snapshot) != expected:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Trading Runner runtime identity changed after Stage-1 materialization",
        )


def _require_distinct_backtest_outputs(sources: Stage1DeploymentSources) -> None:
    roots = (
        sources.backtest_a_config.output.directory,
        sources.backtest_b_config.output.directory,
        sources.recovery_config.output.directory,
    )
    normalized = tuple(path.expanduser().resolve(strict=False) for path in roots)
    if len(set(normalized)) != len(normalized):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Stage-1 Backtest A/B/recovery outputs must use distinct durable roots",
        )


def _published_backtest_result_directory(output_root: Path) -> Path:
    root = output_root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "authoritative Backtest output root is unavailable",
        )
    try:
        candidates = tuple(
            child
            for child in root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and (child / "artifact-manifest.json").is_file()
            and (child / "application-run.json").is_file()
        )
    except OSError as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "authoritative Backtest output root could not be inspected",
        ) from exc
    if len(candidates) != 1:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "authoritative Backtest output root must contain exactly one published result",
        )
    return candidates[0]


__all__ = [
    "Stage1DeploymentComposition",
    "Stage1DeploymentSources",
    "TradingRuntimeAcceptanceQueryPort",
    "TradingRuntimeAcceptanceSnapshotPort",
    "materialize_stage1_deployment",
]
