from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from strategy_factory.adapters import AggregatedCandidate, ResearchRunnerAdapter
from strategy_factory.audit_envelope import (
    DatasetAuditLineage,
    FactoryAuditEnvelopeBuilder,
    ResultsAuditEvidence,
)
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    FactoryStage,
    ResourceBudget,
    StrategyAuditDecision,
)
from strategy_factory.coordinator import FactoryCoordinator
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.planner import ArchitecturePlanner
from strategy_factory.promotion import (
    CrossExperimentSelection,
    PromotionSelection,
    ResultsAggregatorCandidateSelector,
)
from strategy_factory.ranking import (
    Constraint,
    Objective,
    ObjectiveDirection,
    ParetoRanker,
    RankedCandidate,
)
from strategy_factory.registry import StrategyModuleCatalog
from strategy_factory.run import StrategyFactoryRun
from strategy_factory.stage_handlers import (
    ArchitectureGenerationHandler,
    ExperimentPlanningHandler,
    FinalAuditHandler,
    HypothesisGenerationHandler,
    IndependentHoldoutHandler,
    OptimizationPromotionHandler,
    PaperAdmissionHandler,
    RegimeDiscoveryHandler,
    RobustnessEvidenceHandler,
    SimplicitySelectionHandler,
    StrategyPackageHandler,
    StructuralScreeningHandler,
    WalkForwardValidationHandler,
)
from strategy_factory.validation import CandidateGate, CandidateGatePolicy


def _evaluation(candidate_id: str, profit_factor: str, drawdown: str):
    return CandidateEvaluation(
        evaluation_id=f"evaluation-{candidate_id}",
        candidate_id=candidate_id,
        metrics={
            "profit_factor": Decimal(profit_factor),
            "max_drawdown": Decimal(drawdown),
            "trade_count": 50,
        },
        constraints={"registry_evidence_clean": True},
        pareto_front=0,
        instability_flags=(),
        complexity=Decimal("1"),
        evidence_references=(f"evidence-{candidate_id}",),
    )


def test_results_selector_applies_pareto_and_budget(architecture) -> None:
    first_parameters = {"risk": "0.005"}
    second_parameters = {"risk": "0.010"}
    population = (
        AggregatedCandidate(
            configuration_id="candidate-a",
            configuration_hash=canonical_sha256(first_parameters),
            producer_configuration_hash="a" * 64,
            canonical_configuration=first_parameters,
            run_ids=("run-a",),
            evaluation=_evaluation("candidate-a", "1.4", "0.10"),
        ),
        AggregatedCandidate(
            configuration_id="candidate-b",
            configuration_hash=canonical_sha256(second_parameters),
            producer_configuration_hash="b" * 64,
            canonical_configuration=second_parameters,
            run_ids=("run-b",),
            evaluation=_evaluation("candidate-b", "1.2", "0.12"),
        ),
    )

    class Aggregator:
        def candidates_for_experiment(self, experiment_id, *, generated_at):
            assert experiment_id == "experiment-a"
            assert generated_at.tzinfo is not None
            return population

    selector = ResultsAggregatorCandidateSelector(
        aggregator=Aggregator(),
        ranker=ParetoRanker(
            objectives=(
                Objective("profit_factor", ObjectiveDirection.MAXIMIZE),
                Objective("max_drawdown", ObjectiveDirection.MINIMIZE),
            ),
            constraints=(Constraint("trade_count", minimum=Decimal("30")),),
        ),
    )

    result = selector.select(
        experiment_id="experiment-a",
        architecture=architecture,
        maximum_candidates=1,
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert tuple(item.candidate.candidate_id for item in result.selected) == ("candidate-a",)
    assert result.rejected[0].candidate.candidate_id == "candidate-b"
    assert "PROMOTION_BUDGET_LIMIT" in result.rejected[0].selection_reasons


def test_results_selector_can_retain_zero_pass_without_retry(architecture) -> None:
    parameters = {"risk": "0.005"}
    population = (
        AggregatedCandidate(
            configuration_id="candidate-zero-pass",
            configuration_hash=canonical_sha256(parameters),
            producer_configuration_hash="a" * 64,
            canonical_configuration=parameters,
            run_ids=("run-zero-pass",),
            evaluation=_evaluation("candidate-zero-pass", "1.2", "0.10"),
        ),
    )

    class Aggregator:
        def candidates_for_experiment(self, experiment_id, *, generated_at):
            assert experiment_id == "experiment-zero-pass"
            assert generated_at.tzinfo is not None
            return population

    selector = ResultsAggregatorCandidateSelector(
        aggregator=Aggregator(),
        ranker=ParetoRanker(
            objectives=(Objective("profit_factor", ObjectiveDirection.MAXIMIZE),),
            constraints=(Constraint("trade_count", minimum=Decimal("100")),),
        ),
    )

    result = selector.select(
        experiment_id="experiment-zero-pass",
        architecture=architecture,
        maximum_candidates=1,
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        allow_empty=True,
    )

    assert result.selected == ()
    assert tuple(item.candidate.candidate_id for item in result.rejected) == (
        "candidate-zero-pass",
    )
    assert result.producer_configuration_hashes == {}


def test_results_selector_ranks_finalists_globally_across_experiments(architecture) -> None:
    arch_a = replace(architecture, architecture_id="arch-global-a")
    arch_b = replace(architecture, architecture_id="arch-global-b")
    parameters_a = {"risk": "0.005"}
    parameters_b = {"risk": "0.010"}
    populations = {
        "experiment-a": (
            AggregatedCandidate(
                configuration_id="candidate-a",
                configuration_hash=canonical_sha256(parameters_a),
                producer_configuration_hash="a" * 64,
                canonical_configuration=parameters_a,
                run_ids=("run-a",),
                evaluation=_evaluation("candidate-a", "1.20", "0.12"),
            ),
            AggregatedCandidate(
                configuration_id="candidate-not-refined",
                configuration_hash=canonical_sha256({"risk": "0.020"}),
                producer_configuration_hash="c" * 64,
                canonical_configuration={"risk": "0.020"},
                run_ids=("run-not-refined",),
                evaluation=_evaluation("candidate-not-refined", "9.00", "0.01"),
            ),
        ),
        "experiment-b": (
            AggregatedCandidate(
                configuration_id="candidate-b",
                configuration_hash=canonical_sha256(parameters_b),
                producer_configuration_hash="b" * 64,
                canonical_configuration=parameters_b,
                run_ids=("run-b",),
                evaluation=_evaluation("candidate-b", "1.40", "0.10"),
            ),
        ),
    }

    class Aggregator:
        def candidates_for_experiment(self, experiment_id, *, generated_at):
            assert generated_at.tzinfo is not None
            return populations[experiment_id]

    selector = ResultsAggregatorCandidateSelector(
        aggregator=Aggregator(),
        ranker=ParetoRanker(
            objectives=(
                Objective("profit_factor", ObjectiveDirection.MAXIMIZE),
                Objective("max_drawdown", ObjectiveDirection.MINIMIZE),
            ),
            constraints=(
                Constraint("trade_count", minimum=Decimal("30")),
                Constraint("max_drawdown", maximum=Decimal("0.15")),
            ),
        ),
    )

    result = selector.select_across_experiments(
        experiments=(("experiment-a", arch_a), ("experiment-b", arch_b)),
        allowed_producer_hashes={
            "experiment-a": frozenset({"a" * 64}),
            "experiment-b": frozenset({"b" * 64}),
        },
        maximum_candidates=1,
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert tuple(item.candidate.candidate_id for item in result.selected) == ("candidate-b",)
    assert result.source_experiment_ids == {"candidate-b": "experiment-b"}
    assert result.producer_configuration_hashes == {"candidate-b": "b" * 64}
    rejected = {item.candidate.candidate_id: item for item in result.rejected}
    assert "GLOBAL_FINALIST_BUDGET_LIMIT" in rejected["candidate-a"].selection_reasons


def _catalog(architecture) -> StrategyModuleCatalog:
    catalog = StrategyModuleCatalog()
    for reference in architecture.modules:
        catalog.register(reference)
    return catalog


def _staged_architecture(architecture):
    target = replace(
        architecture.target,
        parameter_search_hints={"reward_r": {"factory_steps": 5}},
    )
    risk = replace(
        architecture.risk,
        parameter_search_hints={"risk_fraction": {"factory_steps": 5}},
    )
    return replace(
        architecture,
        architecture_id="arch-staged",
        target=target,
        risk=risk,
    )


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

    from strategy_factory.adapters import ResearchRunnerExecutionAdapter

    repository = SqliteExperimentRepository(tmp_path / "promotion.sqlite3")

    def run_function(job):
        job.output_directory.mkdir(parents=True, exist_ok=True)
        (job.output_directory / "metrics.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            run_id=f"engine-{job.run_id}",
            output_directory=job.output_directory,
            bars_processed=20,
            restart_verified=True,
        )

    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda job: job, run_function),
        retry_policy=RetryPolicy.from_mapping(
            {
                "max_attempts": 2,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "maximum_backoff_seconds": 30.0,
                "retryable_categories": ("TRANSIENT", "RESOURCE"),
            }
        ),
        resume_policy=ResumePolicy.from_mapping(
            {
                "lease_seconds": 900,
                "heartbeat_seconds": 60,
                "recover_expired_leases": True,
                "verify_success_artifacts": True,
            }
        ),
        maximum_workers=2,
        poll_interval_seconds=0.001,
    )
    runner = ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(tmp_path / "promotion-artifacts"),
        plan_builder=ResearchPlanBuilder(tmp_path / "promotion-runs"),
        scheduler=scheduler,
        backtest_engine_version="backtest-test-v1",
        metric_schema_version="metrics-test-v1",
        module_api_version="module-api-test-v1",
    )
    git = GitProvenance(
        branch="local-promotion-test",
        commit_sha="a" * 40,
        clean=True,
    )
    execution = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=git,
    )
    return execution, repository, runner, git


class FirstFrozenSelector:
    def __init__(self, execution) -> None:
        self.execution = execution

    def select(
        self,
        *,
        experiment_id,
        architecture,
        maximum_candidates,
        generated_at,
        allow_empty=False,
    ) -> PromotionSelection:
        del maximum_candidates, generated_at, allow_empty
        frozen = self.execution.frozen_configurations(experiment_id)[0]
        candidate_id = "candidate-first-frozen"
        candidate = CandidateStrategy(
            candidate_id=candidate_id,
            architecture=architecture,
            parameters=frozen.parameters,
            configuration_hash=frozen.factory_configuration_hash,
            lineage_references=("results-aggregator-evidence",),
            evaluation=_evaluation(candidate_id, "1.2", "0.10"),
        )
        return PromotionSelection(
            selected=(
                RankedCandidate(
                    candidate=candidate,
                    pareto_front=1,
                    constraint_results={"trade_count": True},
                    objective_values={"profit_factor": Decimal("1.2")},
                    selection_reasons=("PARETO_FRONT_1",),
                ),
            ),
            rejected=(),
            producer_configuration_hashes={candidate_id: frozen.native_configuration_hash},
        )

    def select_across_experiments(
        self,
        *,
        experiments,
        allowed_producer_hashes,
        maximum_candidates,
        generated_at,
    ) -> CrossExperimentSelection:
        del generated_at
        selected = []
        sources = {}
        producer_hashes = {}
        eligible = [
            (experiment_id, architecture)
            for experiment_id, architecture in sorted(experiments)
            if allowed_producer_hashes[experiment_id]
        ]
        for experiment_id, architecture in eligible[:maximum_candidates]:
            frozen = self.execution.frozen_configurations(experiment_id)[0]
            assert frozen.native_configuration_hash in allowed_producer_hashes[experiment_id]
            candidate_id = f"finalist-{experiment_id[-12:]}"
            candidate = CandidateStrategy(
                candidate_id=candidate_id,
                architecture=architecture,
                parameters=frozen.parameters,
                configuration_hash=frozen.factory_configuration_hash,
                lineage_references=("results-aggregator-finalist-evidence",),
                evaluation=_evaluation(candidate_id, "1.3", "0.09"),
            )
            selected.append(
                RankedCandidate(
                    candidate=candidate,
                    pareto_front=1,
                    constraint_results={"trade_count": True, "max_drawdown": True},
                    objective_values={
                        "profit_factor": Decimal("1.3"),
                        "max_drawdown": Decimal("0.09"),
                    },
                    selection_reasons=("PARETO_FRONT_1", "GLOBAL_FINALIST_TEST"),
                )
            )
            sources[candidate_id] = experiment_id
            producer_hashes[candidate_id] = frozen.native_configuration_hash
        return CrossExperimentSelection(
            selected=tuple(selected),
            rejected=(),
            source_experiment_ids=sources,
            producer_configuration_hashes=producer_hashes,
        )


class PromotionDatasetAuditSource:
    def __init__(self, request) -> None:
        self.request = request

    def lineage_for(self, dataset):
        assert dataset == self.request.dataset
        return DatasetAuditLineage(
            dataset_id=dataset.dataset_id,
            registry_version="promotion-test-registry-v1",
            instrument=self.request.symbol,
            exchange=self.request.exchange,
            market_type=self.request.market_type.value,
            manifest_sha256="1" * 64,
            dataset_sha256=dataset.sha256,
            timeframes=dataset.available_timeframes,
            immutable=True,
            tamper_verified=True,
            funding_lineage_sha256="2" * 64,
        )


class PromotionResultsAuditSource:
    def evidence_for(self, candidate, *, experiment_id: str):
        return ResultsAuditEvidence(
            candidate_id=candidate.candidate_id,
            configuration_hash=candidate.configuration_hash,
            experiment_ids=(experiment_id,),
            registry_package_hashes=(
                canonical_sha256(
                    {
                        "candidate_id": candidate.candidate_id,
                        "configuration_hash": candidate.configuration_hash,
                        "experiment_id": experiment_id,
                    }
                ),
            ),
        )


def _audit_builder(request):
    return FactoryAuditEnvelopeBuilder(
        dataset_source=PromotionDatasetAuditSource(request),
        results_source=PromotionResultsAuditSource(),
    )


class RunBackedAuditPort:
    contract_version = "strategy-audit-port-v1"

    def __init__(self) -> None:
        self.run = None

    def audit(self, evidence_package_reference: str):
        assert self.run is not None
        prefix = "strategy-factory-artifact:"
        assert evidence_package_reference.startswith(prefix)
        artifact = self.run.load_artifact(evidence_package_reference.removeprefix(prefix))
        candidate_id = artifact["candidate"]["candidate_id"]
        issued_at = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
        evidence_hash = canonical_sha256(artifact)
        return StrategyAuditDecision.issue(
            audit_id=f"audit-{candidate_id}",
            candidate_id=candidate_id,
            status=AdmissionStatus.APPROVED_FOR_PAPER,
            evidence_package_reference=evidence_package_reference,
            evidence_hash=evidence_hash,
            reason_codes=("INDEPENDENT_TEST_AUDIT_PASSED",),
            issued_at=issued_at,
            auditor_id="strategy-audit-integration-test",
            contract_version=self.contract_version,
            signature=f"signature-{evidence_hash[:24]}",
        )


class RichRobustnessAggregator:
    def __init__(self, execution) -> None:
        self.execution = execution
        self.calls: dict[str, int] = {}

    def candidates_for_experiment(self, experiment_id, *, generated_at):
        del generated_at
        self.calls[experiment_id] = self.calls.get(experiment_id, 0) + 1
        frozen = self.execution.frozen_configurations(experiment_id)[0]
        candidate_id = f"aggregated-{experiment_id[:16]}"
        return (
            AggregatedCandidate(
                configuration_id=candidate_id,
                configuration_hash=frozen.factory_configuration_hash,
                producer_configuration_hash=frozen.native_configuration_hash,
                canonical_configuration=frozen.parameters,
                run_ids=(f"registry-run-{experiment_id}",),
                evaluation=CandidateEvaluation(
                    evaluation_id=f"evaluation-{candidate_id}",
                    candidate_id=candidate_id,
                    metrics={
                        "profit_factor": Decimal("1.30"),
                        "walk_forward_efficiency": Decimal("0.75"),
                        "parameter_stability": Decimal("0.80"),
                        "regime_coverage": Decimal("0.70"),
                        "time_coverage": Decimal("0.95"),
                        "cost_stress_profit_factor": Decimal("1.10"),
                        "year_stability": Decimal("0.70"),
                        "max_drawdown": Decimal("0.10"),
                        "trade_count": 80,
                        **(
                            {
                                "final_untouched.profit_factor": Decimal("1.18"),
                                "final_untouched.max_drawdown": Decimal("0.11"),
                                "final_untouched.trade_count": 40,
                            }
                            if self.calls[experiment_id] >= 2
                            else {}
                        ),
                    },
                    constraints={"registry_evidence_clean": True},
                    pareto_front=0,
                    instability_flags=(),
                    complexity=Decimal("1"),
                    evidence_references=(f"registry-evidence-{experiment_id}",),
                ),
            ),
        )


def test_coarse_stage_promotes_real_staged_research_runner(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    staged = _staged_architecture(architecture)
    catalog = _catalog(staged)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    planning = ResearchRunnerAdapter(output_root=tmp_path / "promotion-preview")
    execution, repository, _, _ = _real_execution_adapter(tmp_path)
    handlers = {
        FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
        FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(catalog),
        FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(planner),
        FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
            planner=planner,
            research_adapter=planning,
        ),
        FactoryStage.STRUCTURAL_SCREENING: StructuralScreeningHandler(
            research_adapter=planning,
            execution_adapter=execution,
        ),
    }
    handlers[FactoryStage.COARSE_SEARCH] = OptimizationPromotionHandler(
        factory_stage=FactoryStage.COARSE_SEARCH,
        source_stage=FactoryStage.STRUCTURAL_SCREENING,
        optimization_stage_index=1,
        research_adapter=planning,
        execution_adapter=execution,
        selector=FirstFrozenSelector(execution),
    )
    handlers[FactoryStage.LOCAL_REFINEMENT] = OptimizationPromotionHandler(
        factory_stage=FactoryStage.LOCAL_REFINEMENT,
        source_stage=FactoryStage.COARSE_SEARCH,
        optimization_stage_index=2,
        research_adapter=planning,
        execution_adapter=execution,
        selector=FirstFrozenSelector(execution),
        freeze_finalists=True,
        family_balanced=True,
    )
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path / "promotion-state",
        module_versions={"fvg": "1.0.0"},
    )
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=0,
            )
        ),
        handlers=handlers,
    )

    results = tuple(coordinator.step() for _ in range(8))
    assert all(item.action == "completed" for item in results), [
        (i.stage.value, i.action, i.error_type, i.checkpoint.status.value) for i in results
    ]
    checkpoint = results[-1].checkpoint
    assert checkpoint.stage is FactoryStage.WALK_FORWARD
    artifact = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.COARSE_SEARCH.value])
    experiment = artifact["experiments"][0]
    assert experiment["optimization_stage_index"] == 1
    assert experiment["status"] == "completed"
    assert experiment["run_ids"]
    assert repository.experiment_status(experiment["experiment_id"]).value == "COMPLETED"
    refinement = run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.LOCAL_REFINEMENT.value]
    )
    refined_experiment = refinement["experiments"][0]
    assert refined_experiment["optimization_stage_index"] == 2
    assert refined_experiment["status"] == "completed"
    assert refined_experiment["run_ids"]
    assert refinement["finalist_selection_policy"] == "global-pareto-finalists-v1"
    assert refinement["finalist_count"] == 1
    assert len(refinement["finalist_selection_fingerprint"]) == 64
    assert refinement["finalists"][0]["candidate"]["candidate_id"].startswith("finalist-")
    assert checkpoint.budget_state["finalists_selected"] == 1


def test_coarse_stage_cold_restores_without_repreparing_completed_experiment(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    from strategy_factory.adapters import ResearchRunnerExecutionAdapter

    staged = _staged_architecture(architecture)
    catalog = _catalog(staged)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    planning = ResearchRunnerAdapter(output_root=tmp_path / "cold-preview")
    execution, repository, runner, git = _real_execution_adapter(tmp_path)
    common_handlers = {
        FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
        FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(catalog),
        FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(planner),
        FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
            planner=planner,
            research_adapter=planning,
        ),
        FactoryStage.STRUCTURAL_SCREENING: StructuralScreeningHandler(
            research_adapter=planning,
            execution_adapter=execution,
        ),
    }
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path / "cold-state",
        module_versions={"fvg": "1.0.0"},
    )
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=0,
            )
        ),
        handlers=common_handlers,
    )
    completed = []
    while run.checkpoint.stage is not FactoryStage.COARSE_SEARCH:
        result = coordinator.step()
        completed.append(result)
        assert result.action == "completed"

    structural = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.STRUCTURAL_SCREENING.value]
    )
    experiment_id = structural["experiments"][0]["experiment_id"]
    assert repository.experiment_status(experiment_id).value == "COMPLETED"

    restarted = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=git,
    )

    def forbidden_prepare(*args, **kwargs):
        del args, kwargs
        raise AssertionError("cold restore must not call ResearchRunner.prepare")

    runner.prepare = forbidden_prepare
    restarted_handlers = {
        **common_handlers,
        FactoryStage.COARSE_SEARCH: OptimizationPromotionHandler(
            factory_stage=FactoryStage.COARSE_SEARCH,
            source_stage=FactoryStage.STRUCTURAL_SCREENING,
            optimization_stage_index=1,
            research_adapter=planning,
            execution_adapter=restarted,
            selector=FirstFrozenSelector(restarted),
        ),
    }
    restarted_coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=0,
            )
        ),
        handlers=restarted_handlers,
    )
    result = restarted_coordinator.step()
    assert result.action == "completed"
    assert result.checkpoint.stage is FactoryStage.LOCAL_REFINEMENT
    assert repository.experiment_status(experiment_id).value == "COMPLETED"


def test_walk_forward_runs_frozen_candidate_in_separate_experiment(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    staged = _staged_architecture(architecture)
    catalog = _catalog(staged)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    planning = ResearchRunnerAdapter(output_root=tmp_path / "walk-forward-preview")
    execution, repository, _, _ = _real_execution_adapter(tmp_path)
    budget = ResourceBudget(
        maximum_experiments=110,
        maximum_architectures=20,
        maximum_finalists=3,
        maximum_duration_seconds=3600,
        maximum_memory_bytes=1_000_000_000,
        maximum_cpu_workers=4,
        maximum_parallel_tasks=2,
        stage_experiment_limits={
            FactoryStage.STRUCTURAL_SCREENING: 25,
            FactoryStage.COARSE_SEARCH: 20,
            FactoryStage.LOCAL_REFINEMENT: 15,
            FactoryStage.WALK_FORWARD: 40,
            FactoryStage.HOLDOUT: 10,
        },
    )
    request = replace(factory_request, budget=budget)
    selector = FirstFrozenSelector(execution)
    lifecycle_aggregator = RichRobustnessAggregator(execution)
    gate = CandidateGate(
        CandidateGatePolicy(
            minimum_trade_count=30,
            maximum_drawdown=Decimal("0.15"),
        )
    )
    audit_port = RunBackedAuditPort()
    handlers = {
        FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
        FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(catalog),
        FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(planner),
        FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
            planner=planner,
            research_adapter=planning,
        ),
        FactoryStage.STRUCTURAL_SCREENING: StructuralScreeningHandler(
            research_adapter=planning,
            execution_adapter=execution,
        ),
        FactoryStage.COARSE_SEARCH: OptimizationPromotionHandler(
            factory_stage=FactoryStage.COARSE_SEARCH,
            source_stage=FactoryStage.STRUCTURAL_SCREENING,
            optimization_stage_index=1,
            research_adapter=planning,
            execution_adapter=execution,
            selector=selector,
        ),
        FactoryStage.LOCAL_REFINEMENT: OptimizationPromotionHandler(
            factory_stage=FactoryStage.LOCAL_REFINEMENT,
            source_stage=FactoryStage.COARSE_SEARCH,
            optimization_stage_index=2,
            research_adapter=planning,
            execution_adapter=execution,
            selector=selector,
            family_balanced=True,
            freeze_finalists=True,
        ),
        FactoryStage.WALK_FORWARD: WalkForwardValidationHandler(
            research_adapter=planning,
            execution_adapter=execution,
            selector=selector,
        ),
        FactoryStage.ROBUSTNESS: RobustnessEvidenceHandler(
            aggregator=lifecycle_aggregator,
            gate=gate,
        ),
        FactoryStage.HOLDOUT: IndependentHoldoutHandler(
            research_adapter=planning,
            execution_adapter=execution,
            aggregator=lifecycle_aggregator,
            gate=gate,
        ),
        FactoryStage.SIMPLICITY_SELECTION: SimplicitySelectionHandler(),
        FactoryStage.FINAL_AUDIT: FinalAuditHandler(
            audit_port,
            envelope_builder=_audit_builder(request),
        ),
        FactoryStage.PACKAGE: StrategyPackageHandler(),
        FactoryStage.PAPER_ADMISSION: PaperAdmissionHandler(),
    }
    run = StrategyFactoryRun(
        request=request,
        commit_sha="a" * 40,
        state_root=tmp_path / "walk-forward-state",
        module_versions={"fvg": "1.0.0"},
    )
    audit_port.run = run
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=0,
            )
        ),
        handlers=handlers,
    )
    results = tuple(coordinator.step() for _ in range(15))
    assert all(item.action == "completed" for item in results), [
        (item.stage.value, item.action, item.error_type) for item in results
    ]
    checkpoint = results[-1].checkpoint
    assert checkpoint.stage is FactoryStage.PAPER_ADMISSION
    assert checkpoint.status.value == "COMPLETED"
    artifact = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.WALK_FORWARD.value])
    candidate = artifact["candidates"][0]
    assert candidate["candidate"]["candidate_id"].startswith("finalist-")
    local_refinement = run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.LOCAL_REFINEMENT.value]
    )
    assert (
        candidate["candidate"]["candidate_id"]
        == local_refinement["finalists"][0]["candidate"]["candidate_id"]
    )
    assert candidate["walk_forward_window_count"] >= 2
    assert candidate["validation_status"] == "completed"
    assert candidate["oos_status"] == "completed"
    assert candidate["validation_run_ids"]
    assert candidate["oos_run_ids"]
    assert candidate["factory_plan"]["stage"] == FactoryStage.WALK_FORWARD.value
    assert repository.experiment_status(candidate["experiment_id"]).value == "COMPLETED"
    robustness = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.ROBUSTNESS.value])
    assert robustness["passed_candidate_count"] == 1
    assert robustness["candidates"][0]["robustness_report"]["passed"] is True
    holdout = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.HOLDOUT.value])
    assert holdout["holdout_opened"] is True
    assert holdout["passed_candidate_count"] == 1
    holdout_candidate = holdout["candidates"][0]
    assert holdout_candidate["holdout_report"]["opened_once"] is True
    assert holdout_candidate["holdout_report"]["passed"] is True
    assert holdout_candidate["activation_run_ids"]
    assert holdout_candidate["authorization"]["consumed_at"] is not None
    simplicity = run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.SIMPLICITY_SELECTION.value]
    )
    assert (
        simplicity["selected"]["candidate"]["candidate_id"]
        == holdout_candidate["candidate"]["candidate_id"]
    )
    assert simplicity["comparison_dimensions"]
    final_audit = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.FINAL_AUDIT.value])
    assert final_audit["audit_decision"]["status"] == "APPROVED_FOR_PAPER"
    package_artifact = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.PACKAGE.value])
    package = package_artifact["strategy_package"]
    assert package["admission_status"] == "APPROVED_FOR_PAPER"
    assert package["live_trading_authorized"] is False
    paper = run.load_artifact(checkpoint.completed_artifacts[FactoryStage.PAPER_ADMISSION.value])
    assert paper["status"] == "APPROVED_FOR_PAPER"
    assert paper["paper_configuration"]["execution_mode"] == "paper"
    assert paper["live_trading_authorized"] is False


def _baseline_package(factory_request, architecture):
    from strategy_factory.contracts import HoldoutReport, RobustnessReport, TimeRange
    from strategy_factory.package import StrategyPackageBuilder

    candidate_id = "baseline-package-candidate"
    parameters = {
        "fixed_fractional.risk_fraction": "0.005",
        "fixed_r.reward_r": 4,
    }
    candidate = CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("baseline-run",),
        evaluation=CandidateEvaluation(
            evaluation_id="baseline-evaluation",
            candidate_id=candidate_id,
            metrics={
                "profit_factor": Decimal("1.25"),
                "max_drawdown": Decimal("0.10"),
                "trade_count": 80,
                "parameter_stability": Decimal("0.75"),
            },
            constraints={"registry_clean": True},
            pareto_front=0,
            instability_flags=(),
            complexity=Decimal("1"),
            evidence_references=("baseline-evidence",),
        ),
    )
    period = factory_request.dataset.period
    span = period.end - period.start
    quarter = span / 4
    periods = {
        "train": TimeRange(period.start, period.start + quarter),
        "validation": TimeRange(period.start + quarter, period.start + quarter * 2),
        "test": TimeRange(period.start + quarter * 2, period.start + quarter * 3),
        "holdout": TimeRange(period.start + quarter * 3, period.end),
    }
    robustness = RobustnessReport(
        report_id="baseline-robustness",
        candidate_id=candidate_id,
        passed=True,
        scenario_results={"source": "baseline"},
        failure_reasons=(),
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    holdout = HoldoutReport(
        report_id="baseline-holdout",
        candidate_id=candidate_id,
        dataset_fingerprint=factory_request.dataset.fingerprint,
        period=periods["holdout"],
        passed=True,
        metrics={"profit_factor": Decimal("1.10")},
        opened_once=True,
        generated_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    audit = StrategyAuditDecision.issue(
        audit_id="baseline-audit",
        candidate_id=candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference="baseline-evidence-package",
        evidence_hash="b" * 64,
        reason_codes=("BASELINE_AUDIT_PASSED",),
        issued_at=datetime(2026, 1, 3, tzinfo=UTC),
        auditor_id="baseline-auditor",
        contract_version="strategy-audit-port-v1",
        signature="baseline-signature",
    )
    return StrategyPackageBuilder().build(
        request=factory_request,
        candidate=candidate,
        strategy_id="strategy-existing-v1",
        version="1.0.0",
        commit_sha="a" * 40,
        research_periods=periods,
        robustness=robustness,
        holdout=holdout,
        key_results={"profit_factor": Decimal("1.25")},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=("baseline-evidence",),
        paper_configuration={"symbol": factory_request.symbol},
        risk_policy={"research_risk_fraction": Decimal("0.005")},
        execution_assumptions={"market_type": factory_request.market_type.value},
        audit_decision=audit,
    )


class StaticStrategyPackageSource:
    def __init__(self, package) -> None:
        self.package = package
        self.calls = []

    def load(self, strategy_id):
        self.calls.append(strategy_id)
        return self.package


def test_validate_mode_reuses_exact_package_without_optimization(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    from strategy_factory.contracts import FactoryMode, TimeRange
    from strategy_factory.package import StrategyPackageBuilder
    from strategy_factory.stage_handlers import BaselinePackageValidationHandler

    package = _baseline_package(factory_request, architecture)
    source = StaticStrategyPackageSource(package)
    planning = ResearchRunnerAdapter(output_root=tmp_path / "validate-preview")
    execution, repository, _, _ = _real_execution_adapter(tmp_path)
    lifecycle_aggregator = RichRobustnessAggregator(execution)
    gate = CandidateGate(
        CandidateGatePolicy(
            minimum_trade_count=30,
            maximum_drawdown=Decimal("0.15"),
        )
    )
    period = factory_request.dataset.period
    explicit_holdout = TimeRange(
        period.start + (period.end - period.start) * 9 / 10,
        period.end,
    )
    budget = ResourceBudget(
        maximum_experiments=60,
        maximum_architectures=1,
        maximum_finalists=1,
        maximum_duration_seconds=3600,
        maximum_memory_bytes=1_000_000_000,
        maximum_cpu_workers=4,
        maximum_parallel_tasks=2,
        stage_experiment_limits={
            FactoryStage.WALK_FORWARD: 40,
            FactoryStage.HOLDOUT: 10,
        },
    )
    request = replace(
        factory_request,
        request_id="request-eth-validate",
        mode=FactoryMode.VALIDATE,
        baseline_strategy_id=package.strategy_id,
        explicit_holdout=explicit_holdout,
        budget=budget,
    )
    audit_port = RunBackedAuditPort()
    handlers = {
        FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
        FactoryStage.WALK_FORWARD: BaselinePackageValidationHandler(
            package_source=source,
            research_adapter=planning,
            execution_adapter=execution,
            package_builder=StrategyPackageBuilder(),
        ),
        FactoryStage.ROBUSTNESS: RobustnessEvidenceHandler(
            aggregator=lifecycle_aggregator,
            gate=gate,
        ),
        FactoryStage.HOLDOUT: IndependentHoldoutHandler(
            research_adapter=planning,
            execution_adapter=execution,
            aggregator=lifecycle_aggregator,
            gate=gate,
        ),
        FactoryStage.SIMPLICITY_SELECTION: SimplicitySelectionHandler(),
        FactoryStage.FINAL_AUDIT: FinalAuditHandler(
            audit_port,
            envelope_builder=_audit_builder(request),
        ),
        FactoryStage.PACKAGE: StrategyPackageHandler(),
        FactoryStage.PAPER_ADMISSION: PaperAdmissionHandler(),
    }
    run = StrategyFactoryRun(
        request=request,
        commit_sha="a" * 40,
        state_root=tmp_path / "validate-state",
        module_versions={"fvg": "1.0.0"},
    )
    audit_port.run = run
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=0,
            )
        ),
        handlers=handlers,
    )

    results = coordinator.run_until_stop(maximum_steps=32)

    assert results[-1].checkpoint.status.value == "COMPLETED", [
        (item.stage.value, item.action, item.error_type, item.checkpoint.status.value)
        for item in results
    ]
    skipped = {result.stage for result in results if result.action == "skipped"}
    assert skipped == {
        FactoryStage.HYPOTHESIS_GENERATION,
        FactoryStage.ARCHITECTURE_GENERATION,
        FactoryStage.EXPERIMENT_PLANNING,
        FactoryStage.STRUCTURAL_SCREENING,
        FactoryStage.COARSE_SEARCH,
        FactoryStage.LOCAL_REFINEMENT,
    }
    assert source.calls == [package.strategy_id]
    walk = run.load_artifact(run.checkpoint.completed_artifacts[FactoryStage.WALK_FORWARD.value])
    assert walk["mode"] == "validate"
    assert walk["source_strategy_id"] == package.strategy_id
    assert walk["source_package_hash"] == package.package_hash
    frozen = walk["candidates"][0]
    from strategy_factory.contract_codec import (
        decode_candidate_strategy,
        decode_strategy_package,
    )

    frozen_candidate = decode_candidate_strategy(frozen["candidate"])
    expected_parameters = StrategyPackageBuilder.candidate_parameters(package)
    assert frozen_candidate.architecture == package.architecture
    assert dict(frozen_candidate.parameters) == expected_parameters
    assert frozen_candidate.configuration_hash == canonical_sha256(expected_parameters)
    assert "NO_REOPTIMIZATION_PERFORMED" in frozen["source_selection_reasons"]
    assert frozen["validation_status"] == "completed"
    assert frozen["oos_status"] == "completed"
    assert repository.experiment_status(frozen["experiment_id"]).value == "COMPLETED"
    final_package_raw = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.PACKAGE.value]
    )["strategy_package"]
    final_package = decode_strategy_package(final_package_raw)
    assert final_package.architecture == package.architecture
    assert dict(final_package.parameters) == dict(package.parameters)
    assert final_package.dataset_hashes == (factory_request.dataset.sha256,)
    assert final_package.live_trading_authorized is False
