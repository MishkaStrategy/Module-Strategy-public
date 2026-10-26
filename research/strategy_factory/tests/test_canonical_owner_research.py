from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategy_factory import canonical_owner_research as owner_research
from strategy_factory.canonical import canonical_sha256
from strategy_factory.canonical_owner_research import (
    CanonicalOwnerResearchRunnerAdapter,
    _native_parameter_dimension,
)
from strategy_factory.contracts import CandidateStrategy, FactoryMode, FactoryStage
from strategy_factory.errors import CompatibilityError
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.multi_strategy_owner import (
    MS5A_COST_STRESS_MULTIPLIER,
    MS5A_COST_STRESS_SCENARIO_ID,
    MS5A_DATASET_HASH,
    MS5A_REQUIRED_TIMEFRAMES,
)
from strategy_factory.planner import ArchitecturePlanner
from strategy_factory.registry import StrategyModuleCatalog
from strategy_factory.stage_handlers import default_research_split


def _request(factory_request):
    dataset = replace(
        factory_request.dataset,
        dataset_id="dataset-btcusdt-owner",
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        file_format="parquet",
        sha256=MS5A_DATASET_HASH,
        available_timeframes=("5m", *MS5A_REQUIRED_TIMEFRAMES),
        funding_sha256="f" * 64,
    )
    return replace(
        factory_request,
        request_id="canonical-owner-research-adapter",
        mode=FactoryMode.CREATE,
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        dataset=dataset,
        allowed_timeframes=("5m", *MS5A_REQUIRED_TIMEFRAMES),
        allowed_strategy_families=frozenset({"fvg"}),
        objectives={
            **dict(factory_request.objectives),
            "cost_scenarios": (
                {
                    "scenario_id": MS5A_COST_STRESS_SCENARIO_ID,
                    "roles": ("oos",),
                    "execution_cost_multiplier": MS5A_COST_STRESS_MULTIPLIER,
                },
            ),
        },
    )


def test_owner_research_adapter_requires_positive_verified_size(tmp_path) -> None:
    with pytest.raises(TypeError, match="dataset_file_size must be int"):
        CanonicalOwnerResearchRunnerAdapter(
            output_root=tmp_path,
            dataset_file_size=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="must be positive"):
        CanonicalOwnerResearchRunnerAdapter(output_root=tmp_path, dataset_file_size=0)


def test_owner_research_adapter_normalizes_decimal_dimension_values() -> None:
    import modular_research_runner as research

    decimal_dimension = _native_parameter_dimension(
        research,
        "breakout_return.max_return_depth",
        (0.1, 0.2, 0.3, 0.4, 0.5),
    )
    assert decimal_dimension.kind is research.ParameterKind.DECIMAL
    assert decimal_dimension.values == tuple(
        Decimal(value) for value in ("0.1", "0.2", "0.3", "0.4", "0.5")
    )

    integer_dimension = _native_parameter_dimension(research, "integer_steps", (1, 2, 3))
    assert integer_dimension.kind is research.ParameterKind.INTEGER
    assert integer_dimension.values == (1, 2, 3)


def test_owner_research_adapter_builds_exact_owner_lineage_and_walk_forward(
    tmp_path,
    factory_request,
    architecture,
) -> None:
    request = _request(factory_request)
    catalog = StrategyModuleCatalog()
    for module in architecture.modules:
        catalog.register(module)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    parameter_space = planner.build_parameter_space(architecture, maximum_combinations=1)
    split = default_research_split(request)
    adapter = CanonicalOwnerResearchRunnerAdapter(
        output_root=tmp_path / "research",
        dataset_file_size=123_456,
    )

    prepared = adapter.build_plan(
        request=request,
        factory_run_id="sf-run-canonical-owner",
        manifest_id="sf-manifest-canonical-owner",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=parameter_space,
        split=split,
        search_method="grid",
        experiment_count=1,
    )

    lineage = prepared.native_specification.datasets[0]
    assert lineage.file_size == 123_456
    assert lineage.sha256 == MS5A_DATASET_HASH
    assert lineage.funding_data_sha256 == "f" * 64
    assert prepared.factory_plan.dataset_fingerprint == request.dataset.fingerprint
    assert len(prepared.native_plan.configurations) == 1

    parameters = dict(architecture.parameters)
    candidate = CandidateStrategy(
        candidate_id="candidate-owner-research-fixture",
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("owner-fixture",),
    )
    walk_forward = adapter.build_walk_forward_plan(
        request=request,
        factory_run_id="sf-run-canonical-owner",
        manifest_id="sf-manifest-canonical-owner",
        candidate=candidate,
        split=split,
    )

    walk_lineage = walk_forward.native_specification.datasets[0]
    assert walk_lineage.file_size == 123_456
    assert (
        walk_forward.factory_plan.resource_limits["source_candidate_id"] == candidate.candidate_id
    )
    assert walk_forward.factory_plan.resource_limits["final_job_count"] >= 1
    assert (
        walk_forward.native_specification.execution_cost_scenarios
        == request.objectives["cost_scenarios"]
    )
    cost_scenarios = tuple(
        scenario for scenario in walk_forward.native_plan.scenarios if scenario.category == "cost"
    )
    assert tuple(item.scenario_id for item in cost_scenarios) == (MS5A_COST_STRESS_SCENARIO_ID,)
    assert tuple(sorted(role.value for role in cost_scenarios[0].roles)) == ("oos",)


@pytest.mark.parametrize(
    ("blueprint", "error_type", "match"),
    (
        ("not-a-sequence", TypeError, "must be a sequence"),
        ((1,), TypeError, "must be a mapping"),
        (({},), ValueError, "has no runtime component identity"),
        (
            (
                {"runtime_component_id": "duplicate"},
                {"runtime_component_id": "duplicate"},
            ),
            ValueError,
            "duplicate component IDs",
        ),
    ),
)
def test_owner_research_adapter_validates_runtime_closure_blueprint(
    tmp_path,
    blueprint,
    error_type,
    match: str,
) -> None:
    with pytest.raises(error_type, match=match):
        CanonicalOwnerResearchRunnerAdapter(
            output_root=tmp_path,
            dataset_file_size=1,
            runtime_closure_blueprint=blueprint,
        )


def test_owner_research_adapter_rejects_runtime_component_collisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    adapter = CanonicalOwnerResearchRunnerAdapter(
        output_root=tmp_path,
        dataset_file_size=1,
        runtime_closure_blueprint=(
            {
                "runtime_component_id": "duplicate.runtime",
                "runtime_component_version": "1.0.0",
            },
        ),
    )
    monkeypatch.setattr(
        owner_research,
        "_runtime_blueprint",
        lambda architecture: ({"runtime_component_id": "duplicate.runtime"},),
    )
    with pytest.raises(CompatibilityError, match="duplicates architecture component"):
        adapter._runtime_modules(SimpleNamespace())

    malformed = CanonicalOwnerResearchRunnerAdapter(
        output_root=tmp_path,
        dataset_file_size=1,
        runtime_closure_blueprint=({"runtime_component_id": "runtime.only"},),
    )
    with pytest.raises(CompatibilityError, match="version provenance is malformed"):
        malformed._module_versions(SimpleNamespace(modules=()))

    conflicting = CanonicalOwnerResearchRunnerAdapter(
        output_root=tmp_path,
        dataset_file_size=1,
        runtime_closure_blueprint=(
            {
                "runtime_component_id": "runtime.same",
                "runtime_component_version": "1.0.0",
            },
        ),
    )
    architecture = SimpleNamespace(
        modules=(SimpleNamespace(module_id="runtime.same", version="2.0.0"),)
    )
    with pytest.raises(CompatibilityError, match="version conflicts"):
        conflicting._module_versions(architecture)


def test_owner_research_adapter_rejects_undersized_staged_population(
    tmp_path,
    factory_request,
    architecture,
) -> None:
    request = _request(factory_request)
    catalog = StrategyModuleCatalog()
    for module in architecture.modules:
        catalog.register(module)
    parameter_space = ArchitecturePlanner(StrategyGrammar(catalog)).build_parameter_space(
        architecture,
        maximum_combinations=1,
    )
    split = default_research_split(request)
    adapter = CanonicalOwnerResearchRunnerAdapter(
        output_root=tmp_path / "research",
        dataset_file_size=123,
    )

    with pytest.raises(CompatibilityError, match="at least three configurations"):
        adapter.build_plan(
            request=request,
            factory_run_id="sf-run-staged-too-small",
            manifest_id="sf-manifest-staged-too-small",
            stage=FactoryStage.STRUCTURAL_SCREENING,
            architecture=architecture,
            parameter_space=parameter_space,
            split=split,
            search_method="staged",
            experiment_count=1,
        )


def test_walk_forward_rejects_runtime_parameter_projection_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    factory_request,
    architecture,
) -> None:
    request = _request(factory_request)
    split = default_research_split(request)
    parameters = dict(architecture.parameters)
    candidate = CandidateStrategy(
        candidate_id="candidate-projection-drift",
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("owner-fixture",),
    )
    adapter = CanonicalOwnerResearchRunnerAdapter(
        output_root=tmp_path / "research",
        dataset_file_size=123,
    )
    projections = iter(({"runtime": "native"}, {"runtime": "candidate"}))
    monkeypatch.setattr(
        owner_research,
        "_runtime_parameter_projection",
        lambda architecture, parameters: next(projections),
    )

    with pytest.raises(CompatibilityError, match="parameters differ from frozen candidate"):
        adapter.build_walk_forward_plan(
            request=request,
            factory_run_id="sf-run-projection-drift",
            manifest_id="sf-manifest-projection-drift",
            candidate=candidate,
            split=split,
        )
