"""Owner-backed Research Runner adapter for canonical Variant 2 PAPER admission."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters import (
    PreparedResearchPlan,
    ResearchRunnerAdapter,
    ResearchSplitPlan,
    _parameter_kind,
    _research_constraint,
    _runtime_blueprint,
    _runtime_parameter_projection,
    _walk_forward_policy,
)
from .canonical import canonical_json_bytes, canonical_sha256
from .contracts import (
    CandidateStrategy,
    ExperimentPlan,
    FactoryStage,
    ParameterSpace,
    StrategyArchitecture,
    StrategyFactoryRequest,
)
from .errors import CompatibilityError, ExternalComponentUnavailableError


def _native_parameter_dimension(research: Any, name: str, values: tuple[Any, ...]) -> Any:
    kind = _parameter_kind(research, values)
    native_values = values
    if kind is research.ParameterKind.DECIMAL:
        native_values = tuple(
            value if isinstance(value, Decimal) else Decimal(str(value)) for value in values
        )
    return research.ParameterDimension(name=name, kind=kind, values=native_values)


class CanonicalOwnerResearchRunnerAdapter(ResearchRunnerAdapter):
    """Build Research lineage from a deep-verified registered owner byte count."""

    def __init__(
        self,
        *,
        output_root: str | Path,
        dataset_file_size: int,
        runtime_closure_blueprint: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(output_root=output_root)
        if isinstance(dataset_file_size, bool) or not isinstance(dataset_file_size, int):
            raise TypeError("dataset_file_size must be int")
        if dataset_file_size < 1:
            raise ValueError("dataset_file_size must be positive")
        if isinstance(runtime_closure_blueprint, (str, bytes)) or not isinstance(
            runtime_closure_blueprint, Sequence
        ):
            raise TypeError("runtime_closure_blueprint must be a sequence")
        closure = tuple(runtime_closure_blueprint)
        component_ids: list[str] = []
        for index, row in enumerate(closure):
            if not isinstance(row, Mapping):
                raise TypeError(f"runtime_closure_blueprint[{index}] must be a mapping")
            component_id = row.get("runtime_component_id")
            if not isinstance(component_id, str) or not component_id:
                raise ValueError(
                    f"runtime_closure_blueprint[{index}] has no runtime component identity"
                )
            component_ids.append(component_id)
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("runtime_closure_blueprint contains duplicate component IDs")
        self._canonical_output_root = Path(output_root)
        self._dataset_file_size = dataset_file_size
        self._runtime_closure_blueprint = closure

    def _lineage(self, research: Any, request: StrategyFactoryRequest) -> Any:
        return research.DatasetLineage(
            dataset_id=request.dataset.dataset_id,
            source=request.dataset.source,
            market_type=request.dataset.market_type.value,
            instrument=request.dataset.symbol,
            timeframe=request.dataset.base_timeframe,
            period=research.TimeRange(request.dataset.period.start, request.dataset.period.end),
            row_count=request.dataset.row_count,
            timezone=request.dataset.timezone,
            file_format=request.dataset.file_format,
            file_size=self._dataset_file_size,
            sha256=request.dataset.sha256,
            preprocessing_version="strategy-factory-v1",
            aggregation_version="source-declared-v1",
            metadata_version="dataset-descriptor-v1",
            funding_data_sha256=request.dataset.funding_sha256,
            quality_status=research.DatasetQualityStatus.VALID,
        )

    def _runtime_modules(self, architecture: StrategyArchitecture) -> tuple[Mapping[str, Any], ...]:
        base = _runtime_blueprint(architecture)
        component_ids = {
            str(row["runtime_component_id"]) for row in base if "runtime_component_id" in row
        }
        for row in self._runtime_closure_blueprint:
            component_id = row.get("runtime_component_id")
            if component_id in component_ids:
                raise CompatibilityError(
                    f"runtime closure duplicates architecture component: {component_id}"
                )
            component_ids.add(str(component_id))
        return (*base, *self._runtime_closure_blueprint)

    def _module_versions(self, architecture: StrategyArchitecture) -> dict[str, str]:
        versions = {item.module_id: item.version for item in architecture.modules}
        for row in self._runtime_closure_blueprint:
            component_id = row.get("runtime_component_id")
            version = row.get("runtime_component_version")
            if not isinstance(component_id, str) or not isinstance(version, str):
                raise CompatibilityError("runtime closure version provenance is malformed")
            prior = versions.get(component_id)
            if prior is not None and prior != version:
                raise CompatibilityError("runtime closure version conflicts with architecture")
            versions[component_id] = version
        return versions

    def build_plan(
        self,
        *,
        request: StrategyFactoryRequest,
        factory_run_id: str,
        manifest_id: str,
        stage: FactoryStage,
        architecture: StrategyArchitecture,
        parameter_space: ParameterSpace,
        split: ResearchSplitPlan,
        search_method: str = "random",
        experiment_count: int | None = None,
    ) -> PreparedResearchPlan:
        try:
            import modular_research_runner as research
        except ImportError as exc:
            raise ExternalComponentUnavailableError("Research Runner is unavailable") from exc

        dimensions = tuple(
            _native_parameter_dimension(research, name, values)
            for name, values in parameter_space.dimensions.items()
        )
        constraints = tuple(
            _research_constraint(research, item) for item in parameter_space.constraints
        )
        native_space = research.SearchSpace(
            dimensions=dimensions,
            constraints=constraints,
            maximum_combinations=min(
                parameter_space.maximum_combinations,
                experiment_count or parameter_space.maximum_combinations,
            ),
        )
        method = research.SearchMethod(search_method)
        native_split = research.ChronologicalSplitPolicy(
            mode=research.SplitMode.FIXED,
            train=research.TimeRange(split.train.start, split.train.end),
            validation=research.TimeRange(split.validation.start, split.validation.end),
            test=research.TimeRange(split.test.start, split.test.end),
            final_untouched_test=research.TimeRange(split.holdout.start, split.holdout.end),
            gap_seconds=split.gap_seconds,
        )
        staged = None
        if method is research.SearchMethod.STAGED:
            population = native_space.maximum_combinations
            if population < 3:
                raise CompatibilityError("staged search requires at least three configurations")
            first = min(64, population - 1)
            second = max(1, min(16, first - 1))
            staged = research.StagedSearchPolicy(
                train_fractions=(0.25, 0.5, 1.0),
                promotion_counts=(first, second),
            )
        module_versions = self._module_versions(architecture)
        specification = research.ResearchSpecification(
            research_id=f"research-{factory_run_id}-{stage.value}",
            title=f"Strategy Factory {stage.value} for {request.symbol}",
            description="Generated by Strategy Factory from a declarative architecture.",
            strategy_id=architecture.architecture_id,
            strategy_version=architecture.grammar_version,
            module_versions=module_versions,
            base_configuration={
                "architecture_id": architecture.architecture_id,
                "architecture_fingerprint": architecture.fingerprint,
                "modules": [item.key for item in architecture.modules],
                "runtime_modules": self._runtime_modules(architecture),
                "parameters": dict(architecture.parameters),
                "factory_run_id": factory_run_id,
                "factory_manifest_id": manifest_id,
            },
            search_method=method,
            search_space=native_space,
            datasets=(self._lineage(research, request),),
            instruments=(request.symbol,),
            market_type=request.market_type.value,
            timeframes=request.allowed_timeframes,
            date_range=research.TimeRange(split.train.start, split.holdout.end),
            split_policy=native_split,
            staged_search_policy=staged,
            validation_policy={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
                "holdout_isolation": "one_time_final_untouched_grant",
            },
            execution_cost_scenarios=tuple(request.objectives.get("cost_scenarios", ())),
            robustness_scenarios=tuple(request.objectives.get("robustness_scenarios", ())),
            seeds=(request.random_seed,),
            resource_limits={
                "maximum_memory_bytes": request.budget.maximum_memory_bytes,
                "maximum_duration_seconds": request.budget.maximum_duration_seconds,
                "maximum_runs": experiment_count or parameter_space.maximum_combinations,
                "maximum_workers": request.budget.maximum_cpu_workers,
            },
            parallelism=request.budget.maximum_parallel_tasks,
            retry_policy={
                "max_attempts": 2,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "maximum_backoff_seconds": 30.0,
                "retryable_categories": ("TRANSIENT", "RESOURCE"),
            },
            resume_policy={
                "lease_seconds": 900,
                "heartbeat_seconds": 60,
                "recover_expired_leases": True,
                "verify_success_artifacts": True,
            },
            ranking_objective=dict(request.objectives),
            promotion_criteria={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
            },
            artifact_retention={"retain_failures": True, "retain_all_manifests": True},
        )
        preview_id = (
            "exp-" + canonical_sha256((factory_run_id, stage.value, specification.fingerprint))[:24]
        )
        native_plan = research.ResearchPlanBuilder(self._canonical_output_root).build(
            experiment_id=preview_id,
            specification=specification,
        )
        factory_plan = ExperimentPlan(
            plan_id=f"sf-plan-{native_plan.fingerprint[:24]}",
            factory_run_id=factory_run_id,
            stage=stage,
            architecture=architecture,
            parameter_space=parameter_space,
            dataset_fingerprint=request.dataset.fingerprint,
            research_specification=json.loads(canonical_json_bytes(specification)),
            resource_limits={
                "native_experiment_id": native_plan.experiment_id,
                "job_count": len(native_plan.jobs),
                "parallelism": request.budget.maximum_parallel_tasks,
            },
            deterministic_seed=request.random_seed,
            manifest_id=manifest_id,
        )
        return PreparedResearchPlan(factory_plan, specification, native_plan, request.created_at)

    def build_walk_forward_plan(
        self,
        *,
        request: StrategyFactoryRequest,
        factory_run_id: str,
        manifest_id: str,
        candidate: CandidateStrategy,
        split: ResearchSplitPlan,
    ) -> PreparedResearchPlan:
        try:
            import modular_research_runner as research
        except ImportError as exc:
            raise ExternalComponentUnavailableError("Research Runner is unavailable") from exc
        architecture = candidate.architecture
        native_split = research.ChronologicalSplitPolicy(
            mode=research.SplitMode.FIXED,
            train=research.TimeRange(split.train.start, split.train.end),
            validation=research.TimeRange(split.validation.start, split.validation.end),
            test=research.TimeRange(split.test.start, split.test.end),
            final_untouched_test=research.TimeRange(split.holdout.start, split.holdout.end),
            gap_seconds=split.gap_seconds,
        )
        walk_forward = _walk_forward_policy(research, request, split)
        native_space = research.SearchSpace(
            dimensions=(
                research.ParameterDimension(
                    name="factory_candidate_hash",
                    kind=research.ParameterKind.CATEGORICAL,
                    values=(candidate.configuration_hash,),
                ),
            ),
            maximum_combinations=1,
        )
        module_versions = self._module_versions(architecture)
        specification = research.ResearchSpecification(
            research_id=(
                f"research-{factory_run_id}-{FactoryStage.WALK_FORWARD.value}-"
                f"{candidate.candidate_id}"
            ),
            title=f"Strategy Factory walk-forward for {candidate.candidate_id}",
            description=(
                "Single frozen candidate walk-forward validation generated by Strategy Factory. "
                "Final untouched holdout remains gated."
            ),
            strategy_id=architecture.architecture_id,
            strategy_version=architecture.grammar_version,
            module_versions=module_versions,
            base_configuration={
                "architecture_id": architecture.architecture_id,
                "architecture_fingerprint": architecture.fingerprint,
                "modules": [item.key for item in architecture.modules],
                "runtime_modules": self._runtime_modules(architecture),
                "factory_run_id": factory_run_id,
                "factory_manifest_id": manifest_id,
                **dict(candidate.parameters),
            },
            search_method=research.SearchMethod.SINGLE,
            search_space=native_space,
            datasets=(self._lineage(research, request),),
            instruments=(request.symbol,),
            market_type=request.market_type.value,
            timeframes=request.allowed_timeframes,
            date_range=research.TimeRange(split.train.start, split.holdout.end),
            split_policy=native_split,
            walk_forward_policy=walk_forward,
            validation_policy={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
                "candidate_configuration_hash": candidate.configuration_hash,
                "holdout_isolation": "one_time_final_untouched_grant",
            },
            execution_cost_scenarios=tuple(request.objectives.get("cost_scenarios", ())),
            seeds=(request.random_seed,),
            resource_limits={
                "maximum_memory_bytes": request.budget.maximum_memory_bytes,
                "maximum_duration_seconds": request.budget.maximum_duration_seconds,
                "maximum_runs": request.budget.stage_experiment_limits.get(
                    FactoryStage.WALK_FORWARD.value,
                    request.budget.maximum_experiments,
                ),
                "maximum_workers": request.budget.maximum_cpu_workers,
            },
            parallelism=request.budget.maximum_parallel_tasks,
            retry_policy={
                "max_attempts": 2,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "maximum_backoff_seconds": 30.0,
                "retryable_categories": ("TRANSIENT", "RESOURCE"),
            },
            resume_policy={
                "lease_seconds": 900,
                "heartbeat_seconds": 60,
                "recover_expired_leases": True,
                "verify_success_artifacts": True,
            },
            ranking_objective=dict(request.objectives),
            promotion_criteria={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
            },
            artifact_retention={"retain_failures": True, "retain_all_manifests": True},
        )
        preview_id = (
            "exp-"
            + canonical_sha256(
                (
                    factory_run_id,
                    FactoryStage.WALK_FORWARD.value,
                    candidate.candidate_id,
                    specification.fingerprint,
                )
            )[:24]
        )
        builder = research.ResearchPlanBuilder(self._canonical_output_root)
        native_plan = builder.build(experiment_id=preview_id, specification=specification)
        native_configuration = native_plan.configurations[0]
        native_hash = native_configuration.sha256
        if _runtime_parameter_projection(architecture, native_configuration.parameters) != (
            _runtime_parameter_projection(architecture, candidate.parameters)
        ):
            raise CompatibilityError(
                "walk-forward native strategy parameters differ from frozen candidate"
            )
        activation_jobs: list[Any] = []
        roles: list[str] = []
        for role in (research.StageRole.VALIDATION, research.StageRole.OOS):
            if any(item.role is role for item in native_plan.stages):
                roles.append(role.value)
                activation_jobs.extend(
                    builder.build_stage_jobs(
                        plan=native_plan,
                        specification=specification,
                        role=role,
                        candidate_configuration_hashes=(native_hash,),
                    )
                )
        if roles != ["validation", "oos"]:
            raise CompatibilityError("walk-forward plan must expose validation and OOS stages")
        final_jobs = builder.build_final_jobs(
            plan=native_plan,
            specification=specification,
            candidate_configuration_hashes=(native_hash,),
        )
        parameter_space = ParameterSpace(
            space_id=f"sf-wf-space-{candidate.configuration_hash[:24]}",
            architecture_id=architecture.architecture_id,
            dimensions={"factory_candidate_hash": (candidate.configuration_hash,)},
            maximum_combinations=1,
        )
        factory_plan = ExperimentPlan(
            plan_id=f"sf-plan-{native_plan.fingerprint[:24]}",
            factory_run_id=factory_run_id,
            stage=FactoryStage.WALK_FORWARD,
            architecture=architecture,
            parameter_space=parameter_space,
            dataset_fingerprint=request.dataset.fingerprint,
            research_specification=json.loads(canonical_json_bytes(specification)),
            resource_limits={
                "native_experiment_id": native_plan.experiment_id,
                "initial_job_count": len(native_plan.jobs),
                "activation_job_count": len(activation_jobs),
                "planned_total_jobs": len(native_plan.jobs) + len(activation_jobs),
                "final_job_count": len(final_jobs),
                "activation_roles": tuple(roles),
                "parallelism": request.budget.maximum_parallel_tasks,
                "source_candidate_id": candidate.candidate_id,
            },
            deterministic_seed=request.random_seed,
            manifest_id=manifest_id,
        )
        return PreparedResearchPlan(factory_plan, specification, native_plan, request.created_at)


__all__ = ["CanonicalOwnerResearchRunnerAdapter"]
