from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.dataset_reader import RegisteredKlineSelection
from market_data_ingestion.enums import MarketType as DatasetMarketType
from market_data_ingestion.external_corpus import ExternalCorpusFile, ExternalCorpusSpec
from market_data_ingestion.external_registration import ExternalCorpusRegistrar
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.timeframes import timeframe_delta
from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.scalability import PerformanceConfig
from modular_research_runner import (
    ChronologicalSplitPolicy,
    DatasetLineage,
    ParameterDimension,
    ParameterKind,
    ResearchSpecification,
    SearchMethod,
    SearchSpace,
    SplitMode,
    TimeRange as ResearchTimeRange,
)
from modular_research_runner.artifacts import AtomicJsonArtifactStore
from modular_research_runner.engine import BacktestJob, ModularBacktestEngineAdapter
from modular_research_runner.manifest import GitProvenance
from modular_research_runner.planning import ResearchPlanBuilder
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.repository import SqliteExperimentRepository
from modular_research_runner.runner import PreparedExperiment, ResearchRunner
from modular_research_runner.scheduler import LocalResearchScheduler
from modular_system_acceptance import (
    AcceptanceOwnerError,
    SystemAcceptanceProductionBindings,
    VerifiedBacktestDeterminismBinding,
    VerifiedBacktestRunBinding,
)
from modular_system_acceptance.contracts import (
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceRunSpec,
    TrustedOperator,
)
from modular_system_acceptance.production_verification import VerificationProof
from modular_trading_runner import (
    HealthStatus,
    RuntimeHealthRegistry,
    SupervisionPolicy,
    TradingRunner,
    TradingRunnerConfig,
)
from scripts.system_acceptance_stage1_deployment import (
    Stage1DeploymentSources,
    _published_backtest_result_directory,
    materialize_stage1_deployment,
)
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    DatasetDescriptor,
    Direction,
    FactoryMode,
    FactoryStage,
    HoldoutReport,
    MarketType as StrategyMarketType,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
    ResourceBudget,
    RobustnessReport,
    StrategyArchitecture,
    StrategyAuditDecision,
    StrategyFactoryRequest,
    TimeRange as StrategyTimeRange,
)
from strategy_factory.package import StrategyPackageBuilder

_FULL_RC_SHA = "f" * 40
_NOW = datetime(2026, 8, 18, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return _NOW


class _StateStore:
    def load(self, key: str) -> dict[str, object] | None:
        del key
        return None

    def save(self, key: str, value: object) -> None:
        del key, value


class _Planner:
    def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
        assert tuple(modules) == ()  # type: ignore[arg-type]
        assert base_timeframe == "5m"
        return SimpleNamespace(modules=())


class _Journal:
    def append(self, **kwargs: object) -> None:
        del kwargs


class _Toggle:
    def recover(self) -> bool:
        return True

    def reconcile(self) -> bool:
        return True


class _MarketDataRuntime:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _PlanExecution:
    def accept(self, envelope: object, **kwargs: object) -> object:
        del envelope, kwargs
        return {"accepted": True}

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        del kwargs
        return True


class _Checkpoint:
    def capture(self) -> None:
        return None


def _unused_verification() -> VerificationProof:
    raise AssertionError("verification callbacks are execution-time boundaries")


def _registered_dataset(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "BTCUSDT_5m.parquet"
    step = timeframe_delta("5m")
    open_times = [_NOW + index * step for index in range(4)]
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [100.0, 101.0, 102.0, 103.0],
                "high": [101.0, 102.0, 103.0, 104.0],
                "low": [99.0, 100.0, 101.0, 102.0],
                "close": [100.5, 101.5, 102.5, 103.5],
                "volume": [10.0, 11.0, 12.0, 13.0],
                "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
                "quote_volume": [1000.0, 1100.0, 1200.0, 1300.0],
                "trades": [10, 11, 12, 13],
                "taker_buy_base": [5.0, 5.5, 6.0, 6.5],
                "taker_buy_quote": [500.0, 550.0, 600.0, 650.0],
            }
        ),
        source,
    )
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=DatasetMarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(
            ExternalCorpusFile(
                symbol="BTCUSDT",
                timeframe="5m",
                path=source,
                source_uri="local-corpus://stage1/btcusdt/5m",
                expected_rows=len(open_times),
                expected_first_open_time=open_times[0],
                expected_last_open_time=open_times[-1],
                expected_sha256=sha256_file(source),
            ),
        ),
    )
    storage = LocalStorage(tmp_path / "market-data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = ExternalCorpusRegistrar(storage=storage, registry=registry).register(spec)
    return storage.root, result.datasets[0].descriptor.dataset_id


def _module(module_id: str, category: ModuleCategory, **kwargs: object) -> ModuleReference:
    parameter_schema = kwargs.pop("parameter_schema", {})
    assert not kwargs
    return ModuleReference(
        module_id=module_id,
        version="1.0.0",
        category=category,
        import_path=f"fixture:{module_id}",
        api_version="1.0",
        state_schema_version="1",
        required_timeframes=("5m",),
        required_features=(),
        produced_features=(),
        required_data=frozenset(),
        supported_markets=frozenset({StrategyMarketType.LINEAR_FUTURES}),
        supported_directions=frozenset({Direction.LONG, Direction.SHORT}),
        deterministic=True,
        test_coverage=Decimal("95"),
        maturity=ModuleMaturity.PAPER_READY,
        conflict_tags=frozenset(),
        compatibility_tags=frozenset(),
        parameter_schema=parameter_schema,  # type: ignore[arg-type]
        parameter_search_hints={},
    )


def _strategy_package():
    period = StrategyTimeRange(_NOW, _NOW + timedelta(days=40))
    descriptor = DatasetDescriptor(
        dataset_id="stage1-btcusdt-5m",
        source="fixture",
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        market_type=StrategyMarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", "15m"),
        period=period,
        timezone="UTC",
        file_format="parquet",
        uri="local-corpus://stage1/btcusdt/5m",
        sha256="a" * 64,
        row_count=1000,
        received_at=_NOW,
    )
    budget = ResourceBudget(
        maximum_experiments=10,
        maximum_architectures=5,
        maximum_finalists=1,
        maximum_duration_seconds=60,
        maximum_memory_bytes=1_000_000,
        maximum_cpu_workers=1,
        maximum_parallel_tasks=1,
        stage_experiment_limits={
            FactoryStage.STRUCTURAL_SCREENING: 3,
            FactoryStage.COARSE_SEARCH: 4,
            FactoryStage.LOCAL_REFINEMENT: 3,
        },
    )
    request = StrategyFactoryRequest(
        request_id="stage1-request",
        mode=FactoryMode.CREATE,
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        market_type=StrategyMarketType.LINEAR_FUTURES,
        dataset=descriptor,
        allowed_timeframes=("5m", "15m"),
        initial_capital=Decimal("10000"),
        research_risk_fraction=Decimal("0.005"),
        maximum_drawdown_fraction=Decimal("0.15"),
        minimum_trade_count=30,
        objectives={"profit_factor": "maximize"},
        budget=budget,
        random_seed=42,
        allowed_strategy_families=frozenset({"stage1"}),
        created_at=_NOW,
    )
    architecture = StrategyArchitecture(
        architecture_id="stage1-architecture",
        market_type=descriptor.market_type,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
        signal=_module("signal", ModuleCategory.SIGNAL),
        context_filters=(),
        entry=_module("entry", ModuleCategory.ENTRY),
        protection=_module("protection", ModuleCategory.PROTECTION),
        target=_module(
            "target",
            ModuleCategory.TARGET,
            parameter_schema={"reward_r": {"minimum": 1, "maximum": 10}},
        ),
        risk=_module(
            "risk",
            ModuleCategory.RISK,
            parameter_schema={"risk_fraction": {"minimum": "0.0001", "maximum": "0.02"}},
        ),
    )
    parameters = {"risk.risk_fraction": "0.005", "target.reward_r": 4}
    candidate = CandidateStrategy(
        candidate_id="stage1-candidate",
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("stage1-run",),
        evaluation=CandidateEvaluation(
            evaluation_id="stage1-evaluation",
            candidate_id="stage1-candidate",
            metrics={"net_return": "0.20"},
            constraints={"registry_clean": True},
            pareto_front=0,
            instability_flags=(),
            complexity=Decimal("1"),
            evidence_references=("stage1-evidence",),
        ),
    )
    span = period.end - period.start
    quarter = span / 4
    periods = {
        "train": StrategyTimeRange(period.start, period.start + quarter),
        "validation": StrategyTimeRange(period.start + quarter, period.start + 2 * quarter),
        "test": StrategyTimeRange(period.start + 2 * quarter, period.start + 3 * quarter),
        "holdout": StrategyTimeRange(period.start + 3 * quarter, period.end),
    }
    robustness = RobustnessReport(
        report_id="stage1-robustness",
        candidate_id=candidate.candidate_id,
        passed=True,
        scenario_results={"cost_stress": "pass"},
        failure_reasons=(),
        generated_at=_NOW,
    )
    holdout = HoldoutReport(
        report_id="stage1-holdout",
        candidate_id=candidate.candidate_id,
        period=periods["holdout"],
        dataset_fingerprint=request.dataset.fingerprint,
        metrics={"profit_factor": "1.25"},
        passed=True,
        opened_once=True,
        generated_at=_NOW,
    )
    audit = StrategyAuditDecision.issue(
        audit_id="stage1-audit",
        candidate_id=candidate.candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference="evidence:stage1",
        evidence_hash="b" * 64,
        reason_codes=("INDEPENDENT_AUDIT_PASSED",),
        issued_at=_NOW + timedelta(days=1),
        auditor_id="stage1-auditor",
        contract_version="strategy-audit-port-v1",
        signature="test-signature",
    )
    return StrategyPackageBuilder().build(
        request=request,
        candidate=candidate,
        strategy_id="stage1-strategy",
        version="1.0.0",
        commit_sha=_FULL_RC_SHA,
        research_periods=periods,
        robustness=robustness,
        holdout=holdout,
        key_results={"net_return": "0.20"},
        known_weaknesses=("fixture",),
        disabled_regimes=(),
        evidence_links=("evidence:stage1",),
        paper_configuration={"symbol": "BTCUSDT"},
        risk_policy={"risk_fraction": "0.005"},
        execution_assumptions={"maker_fee": "0.0002"},
        audit_decision=audit,
    )


def _research_specification(strategy_id: str) -> ResearchSpecification:
    overall = ResearchTimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    )
    return ResearchSpecification(
        research_id="stage1-research",
        title="Stage-1 prepared research",
        description="Prepared before System Acceptance composition.",
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        module_versions={"stage1": "1.0.0"},
        base_configuration={"risk_fraction": "0.005"},
        search_method=SearchMethod.GRID,
        search_space=SearchSpace(
            dimensions=(ParameterDimension("ttl", ParameterKind.INTEGER, (24,)),),
            maximum_combinations=2,
        ),
        datasets=(
            DatasetLineage(
                dataset_id="stage1-research-data",
                source="binance",
                market_type="binance-usdt-m-perpetual",
                instrument="BTCUSDT",
                timeframe="5m",
                period=overall,
                row_count=1000,
                timezone="UTC",
                file_format="parquet",
                file_size=1000,
                sha256="c" * 64,
                preprocessing_version="raw-v1",
                aggregation_version="utc-v1",
                metadata_version="v1",
            ),
        ),
        instruments=("BTCUSDT",),
        market_type="binance-usdt-m-perpetual",
        timeframes=("5m",),
        date_range=overall,
        split_policy=ChronologicalSplitPolicy(
            mode=SplitMode.FIXED,
            train=ResearchTimeRange(
                datetime(2020, 1, 1, tzinfo=UTC),
                datetime(2022, 1, 1, tzinfo=UTC),
            ),
            validation=ResearchTimeRange(
                datetime(2022, 1, 2, tzinfo=UTC),
                datetime(2023, 1, 1, tzinfo=UTC),
            ),
            test=ResearchTimeRange(
                datetime(2023, 1, 2, tzinfo=UTC),
                datetime(2024, 1, 1, tzinfo=UTC),
            ),
            gap_seconds=86_400,
        ),
        seeds=(7,),
        resource_limits={"maximum_workers": 1},
        parallelism=1,
        retry_policy={"max_attempts": 1},
        resume_policy={"lease_seconds": 30, "heartbeat_seconds": 5},
    )


def _prepared_research(
    tmp_path: Path, strategy_id: str
) -> tuple[ResearchRunner, PreparedExperiment]:
    specification = _research_specification(strategy_id)
    repository = SqliteExperimentRepository(tmp_path / "research.sqlite3")

    def run_function(job: BacktestJob) -> object:
        return SimpleNamespace(
            run_id=f"engine-{job.run_id}",
            output_directory=job.output_directory,
            bars_processed=0,
            restart_verified=True,
        )

    runner = ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(tmp_path / "research-artifacts"),
        plan_builder=ResearchPlanBuilder(tmp_path / "research-outputs"),
        scheduler=LocalResearchScheduler(
            repository=repository,
            engine=ModularBacktestEngineAdapter(lambda job: job, run_function),
            retry_policy=RetryPolicy.from_mapping(specification.retry_policy),
            resume_policy=ResumePolicy.from_mapping(specification.resume_policy),
            maximum_workers=1,
        ),
        backtest_engine_version="engine-v1",
        metric_schema_version="metrics-v1",
        module_api_version="module-api-v1",
    )
    prepared = runner.prepare(
        specification,
        git=GitProvenance(branch="stage1", commit_sha=_FULL_RC_SHA, clean=True),
        created_at=_NOW,
    )
    return runner, prepared


def _trading_runner(strategy_id: str) -> TradingRunner:
    clock = _Clock()
    health = RuntimeHealthRegistry(policy=SupervisionPolicy(), clock=clock)
    health.register("market-data", critical=True)
    health.heartbeat("market-data", status=HealthStatus.HEALTHY, timestamp=_NOW)
    return TradingRunner(
        config=TradingRunnerConfig.from_mapping(
            {
                "schema_version": "1",
                "runtime_id": "stage1-runner",
                "portfolio_id": "stage1-portfolio",
                "strategy_id": strategy_id,
                "execution_mode": "paper",
                "base_timeframe": "5m",
                "market": "usdt_perpetual",
                "instruments": ("BTCUSDT",),
                "market_data_sources": ("binance-usdtm",),
                "modules": (),
            }
        ),
        modules=(),
        planner=_Planner(),
        clock=clock,
        journal=_Journal(),
        state_store=_StateStore(),
        recovery=_Toggle(),
        reconciliation=_Toggle(),
        health=health,
        market_data=_MarketDataRuntime(),
        plan_execution=_PlanExecution(),
        checkpoint=_Checkpoint(),
    )


def _backtests(
    tmp_path: Path,
    storage_root: Path,
    dataset_id: str,
) -> tuple[BacktestAppConfig, BacktestAppConfig, BacktestAppConfig, PerformanceConfig]:
    modules = tmp_path / "modules.yaml"
    modules.write_text("modules: []\n", encoding="utf-8")
    data = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=1,
    )

    def config(name: str, checkpoint: CheckpointConfig) -> BacktestAppConfig:
        return BacktestAppConfig(
            data=data,
            modules=modules,
            requested_timeframes=("15m",),
            output=OutputConfig(tmp_path / name),
            seed=7,
            checkpoint=checkpoint,
        )

    return (
        config("backtest-a", CheckpointConfig()),
        config("backtest-b", CheckpointConfig()),
        config("backtest-recovery", CheckpointConfig(mode="replay", restart_at_bar=1)),
        PerformanceConfig(),
    )


def test_materializes_real_cross_owner_stage1_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", raising=False)
    monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", raising=False)
    storage_root, dataset_id = _registered_dataset(tmp_path)
    package = _strategy_package()
    research_runner, prepared = _prepared_research(tmp_path, package.strategy_id)
    trading_runner = _trading_runner(package.strategy_id)
    backtest_a, backtest_b, recovery, performance = _backtests(
        tmp_path,
        storage_root,
        dataset_id,
    )

    def forbidden_prepare(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("Stage-1 composition must not prepare Research")

    monkeypatch.setattr(research_runner, "prepare", forbidden_prepare)
    sources = Stage1DeploymentSources(
        full_rc_sha=_FULL_RC_SHA,
        dataset_id=dataset_id,
        dataset_version=1,
        dataset_timeframe="5m",
        dataset_storage_root=storage_root,
        strategy_package=package,
        backtest_a_config=backtest_a,
        backtest_b_config=backtest_b,
        recovery_config=recovery,
        performance=performance,
        research_runner=research_runner,
        research_prepared=prepared,
        trading_runner=trading_runner,
        environment_verify=_unused_verification,
        final_verify=_unused_verification,
    )

    composition = materialize_stage1_deployment(sources)

    assert composition.material.full_rc_sha == _FULL_RC_SHA
    assert composition.material.dataset_id == dataset_id
    assert composition.material.strategy_id == package.strategy_id
    assert composition.material.strategy_package_hash == package.package_hash
    assert (
        composition.material.research_specification_fingerprint
        == prepared.specification.fingerprint
    )
    assert composition.material.trading_runner_config_digest == trading_runner.config.canonical_hash
    assert isinstance(composition.bindings, SystemAcceptanceProductionBindings)
    assert isinstance(composition.bindings.dataset_resolver(), RegisteredKlineSelection)
    assert isinstance(composition.bindings.backtest_a, VerifiedBacktestRunBinding)
    assert isinstance(composition.bindings.backtest_b, VerifiedBacktestRunBinding)
    assert isinstance(composition.bindings.determinism_compare, VerifiedBacktestDeterminismBinding)
    assert isinstance(composition.bindings.recovery_run, VerifiedBacktestRunBinding)
    assert composition.bindings.research_runner is research_runner
    assert composition.bindings.research_prepared is prepared
    runtime = composition.bindings.runtime_probe()
    assert runtime.ready and runtime.live
    assert runtime.critical_failure_count == 0
    assert runtime.config_digest == composition.material.config_digest

    rendered = repr(sources)
    assert str(storage_root) not in rendered
    assert str(tmp_path / "backtest-a") not in rendered

    process = composition.build_process(
        state_path=tmp_path / "acceptance.sqlite3",
        reader_token="r" * 32,
        operator_token="o" * 32,
        port=0,
    )
    process.close()


def test_composition_fails_closed_on_runtime_strategy_drift(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered_dataset(tmp_path)
    package = _strategy_package()
    research_runner, prepared = _prepared_research(tmp_path, package.strategy_id)
    runner = _trading_runner("different-strategy")
    backtest_a, backtest_b, recovery, performance = _backtests(
        tmp_path,
        storage_root,
        dataset_id,
    )

    with pytest.raises(AcceptanceOwnerError, match="strategy identities differ"):
        materialize_stage1_deployment(
            Stage1DeploymentSources(
                full_rc_sha=_FULL_RC_SHA,
                dataset_id=dataset_id,
                dataset_version=1,
                dataset_timeframe="5m",
                dataset_storage_root=storage_root,
                strategy_package=package,
                backtest_a_config=backtest_a,
                backtest_b_config=backtest_b,
                recovery_config=recovery,
                performance=performance,
                research_runner=research_runner,
                research_prepared=prepared,
                trading_runner=runner,
                environment_verify=_unused_verification,
                final_verify=_unused_verification,
            )
        )


def test_published_backtest_result_directory_is_restart_resolvable(tmp_path: Path) -> None:
    output_root = tmp_path / "backtest-a"
    published = output_root / "application-run-1"
    published.mkdir(parents=True)
    (published / "artifact-manifest.json").write_text("{}\n", encoding="utf-8")
    (published / "application-run.json").write_text("{}\n", encoding="utf-8")

    assert _published_backtest_result_directory(output_root) == published

    ambiguous = output_root / "application-run-2"
    ambiguous.mkdir()
    (ambiguous / "artifact-manifest.json").write_text("{}\n", encoding="utf-8")
    (ambiguous / "application-run.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AcceptanceOwnerError, match="exactly one published result"):
        _published_backtest_result_directory(output_root)


def test_restart_reconstructs_same_identity_and_durable_owner_state(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered_dataset(tmp_path)
    package = _strategy_package()
    research_runner, prepared = _prepared_research(tmp_path, package.strategy_id)
    trading_runner = _trading_runner(package.strategy_id)
    backtest_a, backtest_b, recovery, performance = _backtests(
        tmp_path,
        storage_root,
        dataset_id,
    )
    sources = Stage1DeploymentSources(
        full_rc_sha=_FULL_RC_SHA,
        dataset_id=dataset_id,
        dataset_version=1,
        dataset_timeframe="5m",
        dataset_storage_root=storage_root,
        strategy_package=package,
        backtest_a_config=backtest_a,
        backtest_b_config=backtest_b,
        recovery_config=recovery,
        performance=performance,
        research_runner=research_runner,
        research_prepared=prepared,
        trading_runner=trading_runner,
        environment_verify=_unused_verification,
        final_verify=_unused_verification,
    )
    state_path = tmp_path / "acceptance-restart.sqlite3"
    first = materialize_stage1_deployment(sources)
    first_process = first.build_process(
        state_path=state_path,
        reader_token="r" * 32,
        operator_token="o" * 32,
        port=0,
    )
    try:
        start_result = first_process.owner.mutate(
            AcceptanceMutationRequest(
                request_id="stage1-start",
                operator=TrustedOperator("test:operator"),
                mutation=AcceptanceMutation.START,
                spec=AcceptanceRunSpec(
                    full_rc_sha=first.material.full_rc_sha,
                    dataset_id=first.material.dataset_id,
                    dataset_digest=first.material.dataset_digest,
                    strategy_id=first.material.strategy_id,
                    strategy_digest=first.material.strategy_package_hash,
                    config_digest=first.material.config_digest,
                ),
            )
        )
        before = first_process.owner.snapshot(start_result.acceptance_run_id)
        assert before is not None
    finally:
        first_process.close()

    second = materialize_stage1_deployment(sources)
    assert second.material.config_digest == first.material.config_digest
    assert second.material.trusted_identity() == first.material.trusted_identity()
    second_process = second.build_process(
        state_path=state_path,
        reader_token="r" * 32,
        operator_token="o" * 32,
        port=0,
    )
    try:
        recovered = second_process.owner.recover_after_restart()
        assert recovered is not None
        assert recovered.acceptance_run_id == start_result.acceptance_run_id
        assert recovered.spec == before.spec
        assert recovered.restart_count == before.restart_count + 1
    finally:
        second_process.close()
