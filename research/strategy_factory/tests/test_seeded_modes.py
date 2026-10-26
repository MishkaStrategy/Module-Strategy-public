from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import module

from strategy_factory.adapters import ResearchRunnerAdapter
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contract_codec import decode_experiment_plan, decode_strategy_architecture
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    FactoryMode,
    FactoryStage,
    HoldoutReport,
    ImprovementPolicy,
    ModuleCategory,
    RobustnessReport,
    StrategyAuditDecision,
    TimeRange,
)
from strategy_factory.coordinator import FactoryCoordinator
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.errors import CompatibilityError
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.package import StrategyPackageBuilder
from strategy_factory.planner import ArchitecturePlanner
from strategy_factory.registry import StrategyModuleCatalog
from strategy_factory.run import StrategyFactoryRun
from strategy_factory.stage_handlers import (
    OptimizationPromotionHandler,
    PackageSeededArchitectureHandler,
    PackageSeededExperimentPlanningHandler,
    PackageSeededHypothesisHandler,
    RegimeDiscoveryHandler,
)


class StaticPackageSource:
    def __init__(self, package) -> None:
        self.package = package
        self.calls: list[str] = []

    def load(self, strategy_id: str):
        self.calls.append(strategy_id)
        return self.package


def _staged_architecture(architecture):
    return replace(
        architecture,
        architecture_id="arch-seeded",
        target=replace(
            architecture.target,
            parameter_search_hints={"reward_r": {"factory_steps": 5}},
        ),
        risk=replace(
            architecture.risk,
            parameter_search_hints={"risk_fraction": {"factory_steps": 5}},
        ),
    )


def _catalog(*architectures, extras=()) -> StrategyModuleCatalog:
    catalog = StrategyModuleCatalog()
    seen: set[str] = set()
    for architecture in architectures:
        for reference in architecture.modules:
            if reference.key not in seen:
                catalog.register(reference)
                seen.add(reference.key)
    for reference in extras:
        if reference.key not in seen:
            catalog.register(reference)
            seen.add(reference.key)
    return catalog


def _source_package(request, architecture):
    candidate_id = "source-candidate"
    parameters = {
        "fixed_fractional.risk_fraction": "0.005",
        "fixed_r.reward_r": 4,
    }
    candidate = CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("source-run",),
        evaluation=CandidateEvaluation(
            evaluation_id="source-evaluation",
            candidate_id=candidate_id,
            metrics={
                "profit_factor": Decimal("1.30"),
                "max_drawdown": Decimal("0.10"),
                "trade_count": 80,
                "parameter_stability": Decimal("0.80"),
            },
            constraints={"registry_clean": True},
            pareto_front=0,
            instability_flags=(),
            complexity=Decimal("1"),
            evidence_references=("source-evidence",),
        ),
    )
    period = request.dataset.period
    span = period.end - period.start
    quarter = span / 4
    periods = {
        "train": TimeRange(period.start, period.start + quarter),
        "validation": TimeRange(period.start + quarter, period.start + quarter * 2),
        "test": TimeRange(period.start + quarter * 2, period.start + quarter * 3),
        "holdout": TimeRange(period.start + quarter * 3, period.end),
    }
    robustness = RobustnessReport(
        report_id="source-robustness",
        candidate_id=candidate_id,
        passed=True,
        scenario_results={"source": "verified"},
        failure_reasons=(),
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    holdout = HoldoutReport(
        report_id="source-holdout",
        candidate_id=candidate_id,
        dataset_fingerprint=request.dataset.fingerprint,
        period=periods["holdout"],
        passed=True,
        metrics={"profit_factor": Decimal("1.15")},
        opened_once=True,
        generated_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    audit = StrategyAuditDecision.issue(
        audit_id="source-audit",
        candidate_id=candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference="source-audit-package",
        evidence_hash="b" * 64,
        reason_codes=("SOURCE_AUDIT_PASSED",),
        issued_at=datetime(2026, 1, 3, tzinfo=UTC),
        auditor_id="source-auditor",
        contract_version="strategy-audit-port-v1",
        signature="source-signature",
    )
    return StrategyPackageBuilder().build(
        request=request,
        candidate=candidate,
        strategy_id="strategy-source-v1",
        version="1.0.0",
        commit_sha="a" * 40,
        research_periods=periods,
        robustness=robustness,
        holdout=holdout,
        key_results={"profit_factor": Decimal("1.30")},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=("source-evidence",),
        paper_configuration={"symbol": request.symbol},
        risk_policy={"research_risk_fraction": Decimal("0.005")},
        execution_assumptions={"market_type": request.market_type.value},
        audit_decision=audit,
    )


def _analyzer() -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal("0"),
        )
    )


def _run(request, tmp_path: Path) -> StrategyFactoryRun:
    return StrategyFactoryRun(
        request=request,
        commit_sha="a" * 40,
        state_root=tmp_path,
        module_versions={"fixture": "1.0.0"},
    )


def _advance_seeded(request, package, catalog, tmp_path: Path):
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    run = _run(request, tmp_path)
    source = StaticPackageSource(package)
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=_analyzer(),
        handlers={
            FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
            FactoryStage.HYPOTHESIS_GENERATION: PackageSeededHypothesisHandler(
                package_source=source
            ),
            FactoryStage.ARCHITECTURE_GENERATION: PackageSeededArchitectureHandler(
                grammar=StrategyGrammar(catalog),
                planner=planner,
            ),
            FactoryStage.EXPERIMENT_PLANNING: PackageSeededExperimentPlanningHandler(
                planner=planner,
                research_adapter=ResearchRunnerAdapter(output_root=tmp_path / "seeded-research"),
            ),
        },
    )
    results = tuple(coordinator.step() for _ in range(5))
    return run, source, results


def test_adapt_freezes_architecture_and_includes_exact_source_control(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    source_architecture = _staged_architecture(architecture)
    package = _source_package(factory_request, source_architecture)
    target_dataset = replace(
        factory_request.dataset,
        dataset_id="dataset-btcusdt-5m",
        symbol="BTCUSDT",
    )
    request = replace(
        factory_request,
        request_id="request-btc-adapt",
        mode=FactoryMode.ADAPT,
        symbol="BTCUSDT",
        dataset=target_dataset,
        source_strategy_id=package.strategy_id,
        baseline_strategy_id=None,
        allowed_strategy_families=frozenset({"fvg"}),
    )
    run, source, results = _advance_seeded(
        request,
        package,
        _catalog(source_architecture),
        tmp_path,
    )

    assert all(item.action == "completed" for item in results), [
        (item.stage.value, item.action, item.error_type, item.checkpoint.status.value)
        for item in results
    ]
    assert source.calls == [package.strategy_id]
    architecture_artifact = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.ARCHITECTURE_GENERATION.value]
    )
    architectures = tuple(
        decode_strategy_architecture(item) for item in architecture_artifact["architectures"]
    )
    assert len(architectures) == 1
    assert architectures[0].fingerprint == package.architecture.fingerprint
    planning = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.EXPERIMENT_PLANNING.value]
    )
    assert planning["control_arm_count"] == 1
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
    assert planning["plans"][0]["strategy_family"] == "fvg"
    assert planning["plans"][0]["family_capacity"] == 30
    assert planning["plans"][0]["architecture_capacity"] == 30
    plan = decode_experiment_plan(planning["plans"][0]["factory_plan"])
    assert "fixed_r.reward_r" in plan.parameter_space.dimensions
    assert 4 in plan.parameter_space.dimensions["fixed_r.reward_r"]
    assert "0.005" in plan.parameter_space.dimensions["fixed_fractional.risk_fraction"]


def test_improve_default_policy_keeps_structural_baseline_control(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    source_architecture = _staged_architecture(architecture)
    package = _source_package(factory_request, source_architecture)
    request = replace(
        factory_request,
        request_id="request-eth-improve",
        mode=FactoryMode.IMPROVE,
        baseline_strategy_id=package.strategy_id,
        source_strategy_id=None,
        allowed_strategy_families=frozenset({"fvg"}),
    )
    run, _, results = _advance_seeded(
        request,
        package,
        _catalog(source_architecture),
        tmp_path,
    )

    assert all(item.action == "completed" for item in results), [
        (item.stage.value, item.action, item.error_type, item.checkpoint.status.value)
        for item in results
    ]
    architecture_artifact = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.ARCHITECTURE_GENERATION.value]
    )
    assert len(architecture_artifact["architectures"]) == 1
    assert architecture_artifact["baseline_architecture_fingerprint"] == (
        package.architecture.fingerprint
    )
    planning = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.EXPERIMENT_PLANNING.value]
    )
    assert planning["control_arm_count"] == 1
    plan = decode_experiment_plan(planning["plans"][0]["factory_plan"])
    assert len(plan.parameter_space.dimensions["fixed_r.reward_r"]) <= 3
    assert 4 in plan.parameter_space.dimensions["fixed_r.reward_r"]


def test_improve_allows_only_explicit_context_filter_mutation(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    source_architecture = _staged_architecture(architecture)
    package = _source_package(factory_request, source_architecture)
    context_filter = module(
        "ema_filter",
        ModuleCategory.CONTEXT_FILTER,
        compatibility_tags=frozenset({"trend"}),
    )
    alternate_target = module(
        "alternate_target",
        ModuleCategory.TARGET,
        parameter_schema={"reward_r": {"minimum": 1, "maximum": 8}},
        parameter_search_hints={"reward_r": {"factory_steps": 3}},
    )
    request = replace(
        factory_request,
        request_id="request-eth-improve-context",
        mode=FactoryMode.IMPROVE,
        baseline_strategy_id=package.strategy_id,
        source_strategy_id=None,
        allowed_strategy_families=frozenset({"fvg"}),
        improvement_policy=ImprovementPolicy(
            allowed_mutations=frozenset({"parameters", "context_filters"}),
            maximum_runtime_module_changes=1,
            maximum_architectures=4,
            maximum_complexity_increase=Decimal("1"),
        ),
    )
    run, _, results = _advance_seeded(
        request,
        package,
        _catalog(source_architecture, extras=(context_filter, alternate_target)),
        tmp_path,
    )

    assert all(item.action == "completed" for item in results), [
        (item.stage.value, item.action, item.error_type, item.checkpoint.status.value)
        for item in results
    ]
    artifact = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.ARCHITECTURE_GENERATION.value]
    )
    architectures = tuple(decode_strategy_architecture(item) for item in artifact["architectures"])
    assert len(architectures) == 2
    assert any(item.context_filters for item in architectures)
    assert all(
        item.target.module_id == package.architecture.target.module_id for item in architectures
    )
    rejected_reasons = {
        reason for rejection in artifact["rejections"] for reason in rejection["reason_codes"]
    }
    assert "MUTATION_TYPE_FORBIDDEN" in rejected_reasons


def test_adapt_promotion_rejects_non_baseline_aware_selector(
    factory_request,
    architecture,
    tmp_path: Path,
) -> None:
    source_architecture = _staged_architecture(architecture)
    package = _source_package(factory_request, source_architecture)
    request = replace(
        factory_request,
        request_id="request-adapt-selector-boundary",
        mode=FactoryMode.ADAPT,
        source_strategy_id=package.strategy_id,
        baseline_strategy_id=None,
        allowed_strategy_families=frozenset({"fvg"}),
    )
    run, _, results = _advance_seeded(request, package, _catalog(source_architecture), tmp_path)
    assert all(item.action == "completed" for item in results)
    planning = run.load_artifact(
        run.checkpoint.completed_artifacts[FactoryStage.EXPERIMENT_PLANNING.value]
    )
    plan = decode_experiment_plan(planning["plans"][0]["factory_plan"])
    structural = {
        "schema_version": 1,
        "experiments": (
            {
                "factory_plan_id": plan.plan_id,
                "experiment_id": "experiment-control",
            },
        ),
    }

    class Context:
        def __init__(self) -> None:
            self.request = request
            self.checkpoint = SimpleNamespace(
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
                run_id="sf-run-selector-boundary",
            )

        @staticmethod
        def load_artifact(stage: str):
            if stage == FactoryStage.EXPERIMENT_PLANNING.value:
                return planning
            if stage == FactoryStage.STRUCTURAL_SCREENING.value:
                return structural
            raise AssertionError(stage)

    class OrdinarySelector:
        def select(self, **kwargs):
            del kwargs
            raise AssertionError("ordinary selector must not be called")

    handler = OptimizationPromotionHandler(
        factory_stage=FactoryStage.COARSE_SEARCH,
        source_stage=FactoryStage.STRUCTURAL_SCREENING,
        optimization_stage_index=1,
        research_adapter=object(),
        execution_adapter=object(),
        selector=OrdinarySelector(),
    )
    with pytest.raises(CompatibilityError, match="baseline-aware selector"):
        handler.execute(Context())
