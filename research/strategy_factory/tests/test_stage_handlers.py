from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from strategy_factory.adapters import ResearchRunnerAdapter
from strategy_factory.contract_codec import decode_experiment_plan
from strategy_factory.contracts import FactoryStage, FactoryStatus
from strategy_factory.coordinator import FactoryCoordinator
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.planner import ArchitecturePlanner
from strategy_factory.registry import StrategyModuleCatalog
from strategy_factory.run import StrategyFactoryRun
from strategy_factory.stage_handlers import (
    ArchitectureGenerationHandler,
    ExperimentPlanningHandler,
    HypothesisGenerationHandler,
    RegimeDiscoveryHandler,
    StructuralScreeningHandler,
    default_research_split,
)


def _analyzer() -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=0,
        )
    )


def _catalog(architecture) -> StrategyModuleCatalog:
    catalog = StrategyModuleCatalog()
    for reference in architecture.modules:
        catalog.register(reference)
    return catalog


def _run(factory_request, tmp_path: Path) -> StrategyFactoryRun:
    return StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path / "state",
        module_versions={"fvg": "1.0.0"},
    )


def test_real_early_stage_pipeline_builds_canonical_research_plan(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    catalog = _catalog(architecture)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    run = _run(factory_request, tmp_path)
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=_analyzer(),
        handlers={
            FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
            FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(catalog),
            FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(planner),
            FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
                planner=planner,
                research_adapter=ResearchRunnerAdapter(output_root=tmp_path / "research"),
            ),
        },
    )

    results = tuple(coordinator.step() for _ in range(5))

    assert all(item.action == "completed" for item in results)
    checkpoint = results[-1].checkpoint
    assert checkpoint.status is FactoryStatus.RUNNING
    assert checkpoint.stage is FactoryStage.STRUCTURAL_SCREENING
    assert checkpoint.budget_state["architectures_generated"] == 1

    planning = run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.EXPERIMENT_PLANNING.value]
    )
    assert planning["architecture_count"] == 1
    assert planning["family_budget_policy"] == "equal-family-capacity-v1"
    assert planning["family_allocations"] == [
        {
            "family_id": "fvg",
            "family_capacity": 30,
            "architecture_count": 1,
            "architecture_capacity": 30,
            "allocated_capacity": 30,
            "unused_capacity": 0,
        }
    ]
    assert planning["unused_stage_capacity"] == 0
    assert planning["planned_job_count"] > 0
    record = planning["plans"][0]
    assert record["strategy_family"] == "fvg"
    assert record["family_capacity"] == 30
    assert record["architecture_capacity"] == 30
    plan = decode_experiment_plan(record["factory_plan"])
    assert plan.stage is FactoryStage.STRUCTURAL_SCREENING
    assert plan.factory_run_id == checkpoint.run_id
    assert plan.dataset_fingerprint == factory_request.dataset.fingerprint
    assert record["search_method"] == "grid"
    assert plan.resource_limits["job_count"] == planning["planned_job_count"]


def test_default_split_preserves_explicit_holdout(factory_request) -> None:
    period = factory_request.dataset.period
    holdout_start = period.start + (period.end - period.start) * 3 / 4
    request = replace(
        factory_request,
        explicit_holdout=type(period)(holdout_start, period.end),
    )

    split = default_research_split(request, gap_seconds=300)

    assert split.holdout == request.explicit_holdout
    assert split.train.end == split.validation.start
    assert split.validation.end == split.test.start
    assert split.test.end == split.holdout.start
    assert split.gap_seconds == 300


def test_no_registered_allowed_family_fails_closed(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    request = replace(
        factory_request,
        allowed_strategy_families=frozenset({"mean_reversion"}),
    )
    catalog = _catalog(architecture)
    coordinator = FactoryCoordinator(
        run=_run(request, tmp_path),
        dataset_analyzer=_analyzer(),
        handlers={
            FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
            FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(catalog),
        },
    )

    assert coordinator.step().action == "completed"
    assert coordinator.step().action == "completed"
    failed = coordinator.step()

    assert failed.action == "failed"
    assert failed.checkpoint.status is FactoryStatus.FAILED
    assert failed.error_type == "CompatibilityError"


def _early_handlers(catalog, planner, planning_adapter):
    return {
        FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
        FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(catalog),
        FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(planner),
        FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
            planner=planner,
            research_adapter=planning_adapter,
        ),
    }


def _real_execution_adapter(tmp_path: Path):
    from types import SimpleNamespace

    from modular_research_runner import (
        AtomicJsonArtifactStore,
        GitProvenance,
        LocalResearchScheduler,
        ModularBacktestEngineAdapter,
        ResearchPlanBuilder,
        ResearchRunner,
        ResumePolicy,
        RetryPolicy,
        SqliteExperimentRepository,
    )

    repository = SqliteExperimentRepository(tmp_path / "research.sqlite3")

    def run_function(job):
        job.output_directory.mkdir(parents=True, exist_ok=True)
        (job.output_directory / "metrics.json").write_text(
            '{"profit_factor":1.1,"trade_count":40}',
            encoding="utf-8",
        )
        return SimpleNamespace(
            run_id=f"engine-{job.run_id}",
            output_directory=job.output_directory,
            bars_processed=20,
            restart_verified=True,
        )

    engine = ModularBacktestEngineAdapter(lambda job: job, run_function=run_function)
    retry = RetryPolicy.from_mapping(
        {
            "max_attempts": 2,
            "initial_backoff_seconds": 1.0,
            "backoff_multiplier": 2.0,
            "maximum_backoff_seconds": 30.0,
            "retryable_categories": ("TRANSIENT", "RESOURCE"),
        }
    )
    resume = ResumePolicy.from_mapping(
        {
            "lease_seconds": 900,
            "heartbeat_seconds": 60,
            "recover_expired_leases": True,
            "verify_success_artifacts": True,
        }
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=engine,
        retry_policy=retry,
        resume_policy=resume,
        maximum_workers=2,
        poll_interval_seconds=0.001,
    )
    runner = ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(tmp_path / "runner-artifacts"),
        plan_builder=ResearchPlanBuilder(tmp_path / "runner-output"),
        scheduler=scheduler,
        backtest_engine_version="backtest-test-v1",
        metric_schema_version="metrics-v1",
        module_api_version="module-api-v1",
    )
    from strategy_factory.adapters import ResearchRunnerExecutionAdapter

    execution = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=GitProvenance(
            branch="local-test",
            commit_sha="a" * 40,
            clean=True,
        ),
    )
    return execution, repository


def test_structural_screening_executes_real_research_runner(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    catalog = _catalog(architecture)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    planning_adapter = ResearchRunnerAdapter(output_root=tmp_path / "preview-output")
    execution, repository = _real_execution_adapter(tmp_path)
    handlers = _early_handlers(catalog, planner, planning_adapter)
    handlers[FactoryStage.STRUCTURAL_SCREENING] = StructuralScreeningHandler(
        research_adapter=planning_adapter,
        execution_adapter=execution,
    )
    run = _run(factory_request, tmp_path)
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=_analyzer(),
        handlers=handlers,
    )

    results = tuple(coordinator.step() for _ in range(6))

    assert all(item.action == "completed" for item in results)
    checkpoint = results[-1].checkpoint
    assert checkpoint.stage is FactoryStage.COARSE_SEARCH
    assert checkpoint.budget_state["experiments_reserved"] == 1
    artifact = run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.STRUCTURAL_SCREENING.value]
    )
    assert artifact["partial_results"] is False
    experiment = artifact["experiments"][0]
    assert experiment["status"] == "completed"
    assert experiment["run_status_counts"] == {"succeeded": 1}
    assert repository.experiment_status(experiment["experiment_id"]).value == "COMPLETED"


def test_restore_plan_rejects_changed_frozen_identity(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    from strategy_factory.adapters import ResearchSplitPlan
    from strategy_factory.errors import CompatibilityError

    start = factory_request.dataset.period.start
    span = factory_request.dataset.period.end - start
    split = ResearchSplitPlan(
        train=type(factory_request.dataset.period)(start, start + span * 6 / 10),
        validation=type(factory_request.dataset.period)(
            start + span * 6 / 10, start + span * 8 / 10
        ),
        test=type(factory_request.dataset.period)(start + span * 8 / 10, start + span * 9 / 10),
        holdout=type(factory_request.dataset.period)(
            start + span * 9 / 10, factory_request.dataset.period.end
        ),
    )
    planner = ArchitecturePlanner(StrategyGrammar(_catalog(architecture)))
    space = planner.build_parameter_space(architecture, maximum_combinations=1)
    adapter = ResearchRunnerAdapter(output_root=tmp_path / "preview")
    prepared = adapter.build_plan(
        request=factory_request,
        factory_run_id="sf-restore-plan",
        manifest_id="manifest-restore-plan",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=space,
        split=split,
        search_method="grid",
        experiment_count=1,
    )
    changed = replace(prepared.factory_plan, deterministic_seed=43)

    import pytest

    with pytest.raises(CompatibilityError, match="differs"):
        adapter.restore_plan(
            request=factory_request,
            frozen_plan=changed,
            split=split,
            search_method="grid",
            experiment_count=1,
        )
