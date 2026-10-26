from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .canonical import canonical_sha256
from .engine import BacktestJob
from .identity import derive_seed, run_id
from .models import (
    ResearchSpecification,
    SearchMethod,
    SpecificationError,
    TimeRange,
    freeze,
)
from .search import GeneratedConfiguration, GridSearchPlanner, RandomSearchPlanner
from .splits import PlannedSplit, plan_chronological_split, plan_walk_forward


class StageRole(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    OOS = "oos"
    FINAL_UNTOUCHED = "final_untouched"


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    category: str
    overrides: Mapping[str, Any]
    roles: frozenset[StageRole]

    def __post_init__(self) -> None:
        if not self.scenario_id.strip() or not self.category.strip():
            raise SpecificationError("scenario identifiers must be non-empty")
        object.__setattr__(self, "overrides", MappingProxyType(dict(freeze(self.overrides))))
        if not self.roles:
            raise SpecificationError("scenario must apply to at least one stage role")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class PlannedStage:
    stage_id: str
    role: StageRole
    split_id: str
    dataset_id: str
    period: TimeRange
    window_id: str | None = None

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class PlannedOptimizationStage:
    stage_id: str
    role: str
    split_id: str
    dataset_id: str
    period: TimeRange
    stage_index: int
    maximum_candidates: int

    def __post_init__(self) -> None:
        if not self.stage_id.strip() or not self.role.startswith("optimization_"):
            raise SpecificationError("optimization stage identifiers are invalid")
        if (
            not isinstance(self.stage_index, int)
            or isinstance(self.stage_index, bool)
            or self.stage_index < 0
            or self.role != f"optimization_{self.stage_index:03d}"
        ):
            raise SpecificationError("optimization stage role/index mismatch")
        if (
            not isinstance(self.maximum_candidates, int)
            or isinstance(self.maximum_candidates, bool)
            or self.maximum_candidates < 1
        ):
            raise SpecificationError("optimization maximum_candidates must be positive")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    experiment_id: str
    split: PlannedSplit
    configurations: tuple[GeneratedConfiguration, ...]
    scenarios: tuple[ScenarioDefinition, ...]
    stages: tuple[PlannedStage, ...]
    final_stages: tuple[PlannedStage, ...]
    jobs: tuple[BacktestJob, ...]
    optimization_stages: tuple[PlannedOptimizationStage, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise SpecificationError("unsupported experiment plan schema")
        identifier_groups = {
            "configuration": tuple(item.sha256 for item in self.configurations),
            "scenario": tuple(item.scenario_id for item in self.scenarios),
            "stage": tuple(item.stage_id for item in self.stages)
            + tuple(item.stage_id for item in self.final_stages)
            + tuple(item.stage_id for item in self.optimization_stages),
            "job": tuple(item.run_id for item in self.jobs),
        }
        for name, identifiers in identifier_groups.items():
            if len(set(identifiers)) != len(identifiers):
                raise SpecificationError(f"duplicate {name} identifiers in experiment plan")
        if any(stage.role is StageRole.FINAL_UNTOUCHED for stage in self.stages):
            raise SpecificationError("final untouched stages must not be in the ordinary plan")
        if any(stage.role is not StageRole.FINAL_UNTOUCHED for stage in self.final_stages):
            raise SpecificationError("final stage templates must be final untouched")
        if self.optimization_stages:
            indices = sorted({stage.stage_index for stage in self.optimization_stages})
            if indices != list(range(len(indices))):
                raise SpecificationError("optimization stage indices must be contiguous")
            first_role = "optimization_000"
            if any(job.stage_role != first_role for job in self.jobs):
                raise SpecificationError(
                    "initial staged-search jobs must belong only to optimization_000"
                )
            if any(stage.role is StageRole.TRAIN for stage in self.stages):
                raise SpecificationError(
                    "staged-search plans must not expose an unused ordinary train stage"
                )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


class ResearchPlanBuilder:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        if output_root.is_symlink():
            raise SpecificationError("research output root must not be a symlink")

    def build(
        self,
        *,
        experiment_id: str,
        specification: ResearchSpecification,
    ) -> ExperimentPlan:
        split = plan_chronological_split(specification.split_policy, specification.date_range)
        configurations = self._configurations(specification)
        scenarios = self._scenarios(specification)
        stages, final_stages = self._stages(specification, split)
        optimization_stages = self._optimization_stages(
            specification,
            split,
            configurations,
        )
        if optimization_stages:
            stages = tuple(stage for stage in stages if stage.role is not StageRole.TRAIN)
            jobs = self._optimization_jobs(
                experiment_id=experiment_id,
                specification=specification,
                configurations=configurations,
                stages=tuple(stage for stage in optimization_stages if stage.stage_index == 0),
            )
        else:
            jobs = self._jobs(
                experiment_id=experiment_id,
                specification=specification,
                configurations=configurations,
                scenarios=scenarios,
                stages=tuple(stage for stage in stages if stage.role is StageRole.TRAIN),
            )
        return ExperimentPlan(
            experiment_id=experiment_id,
            split=split,
            configurations=configurations,
            scenarios=scenarios,
            stages=stages,
            final_stages=final_stages,
            jobs=jobs,
            optimization_stages=optimization_stages,
        )

    def build_stage_jobs(
        self,
        *,
        plan: ExperimentPlan,
        specification: ResearchSpecification,
        role: StageRole,
        candidate_configuration_hashes: Sequence[str],
    ) -> tuple[BacktestJob, ...]:
        if role in {StageRole.TRAIN, StageRole.FINAL_UNTOUCHED}:
            raise SpecificationError("use the initial plan or final-test gate for this role")
        selected_stages = tuple(stage for stage in plan.stages if stage.role is role)
        if not selected_stages:
            raise SpecificationError(f"experiment plan has no {role.value} stages")
        requested = tuple(sorted(set(candidate_configuration_hashes)))
        if not requested:
            raise SpecificationError("stage activation requires frozen candidates")
        by_hash = {item.sha256: item for item in plan.configurations}
        unknown = set(requested) - set(by_hash)
        if unknown:
            raise SpecificationError("stage activation references unknown configurations")
        return self._jobs(
            experiment_id=plan.experiment_id,
            specification=specification,
            configurations=tuple(by_hash[item] for item in requested),
            scenarios=tuple(scenario for scenario in plan.scenarios if role in scenario.roles),
            stages=selected_stages,
        )

    def build_optimization_stage_jobs(
        self,
        *,
        plan: ExperimentPlan,
        specification: ResearchSpecification,
        stage_index: int,
        candidate_configuration_hashes: Sequence[str],
    ) -> tuple[BacktestJob, ...]:
        if not plan.optimization_stages:
            raise SpecificationError("experiment plan is not staged search")
        if not isinstance(stage_index, int) or isinstance(stage_index, bool):
            raise SpecificationError("optimization stage_index must be an integer")
        available_indices = {stage.stage_index for stage in plan.optimization_stages}
        if stage_index not in available_indices or stage_index == 0:
            raise SpecificationError("optimization stage_index is not activatable")
        selected_stages = tuple(
            stage for stage in plan.optimization_stages if stage.stage_index == stage_index
        )
        requested = tuple(sorted(set(candidate_configuration_hashes)))
        if not requested:
            raise SpecificationError("optimization activation requires frozen candidates")
        maximum_candidates = selected_stages[0].maximum_candidates
        if len(requested) > maximum_candidates:
            raise SpecificationError(
                f"optimization stage {stage_index} accepts at most {maximum_candidates} candidates"
            )
        by_hash = {item.sha256: item for item in plan.configurations}
        unknown = set(requested) - set(by_hash)
        if unknown:
            raise SpecificationError("optimization activation references unknown configurations")
        return self._optimization_jobs(
            experiment_id=plan.experiment_id,
            specification=specification,
            configurations=tuple(by_hash[item] for item in requested),
            stages=selected_stages,
        )

    def build_final_jobs(
        self,
        *,
        plan: ExperimentPlan,
        specification: ResearchSpecification,
        candidate_configuration_hashes: Sequence[str],
    ) -> tuple[BacktestJob, ...]:
        if not plan.final_stages:
            raise SpecificationError("research specification has no final untouched test range")
        requested = tuple(sorted(set(candidate_configuration_hashes)))
        if not requested:
            raise SpecificationError("final test requires at least one frozen candidate")
        by_hash = {item.sha256: item for item in plan.configurations}
        unknown = set(requested) - set(by_hash)
        if unknown:
            raise SpecificationError("final test references unknown configuration hashes")
        return self._jobs(
            experiment_id=plan.experiment_id,
            specification=specification,
            configurations=tuple(by_hash[item] for item in requested),
            scenarios=tuple(
                scenario
                for scenario in plan.scenarios
                if StageRole.FINAL_UNTOUCHED in scenario.roles
            ),
            stages=plan.final_stages,
        )

    @staticmethod
    def _configurations(
        specification: ResearchSpecification,
    ) -> tuple[GeneratedConfiguration, ...]:
        raw: tuple[GeneratedConfiguration, ...]
        if specification.search_method is SearchMethod.SINGLE:
            raw = (GeneratedConfiguration(0, {}, canonical_sha256({})),)
        elif specification.search_method in {SearchMethod.GRID, SearchMethod.STAGED}:
            raw = GridSearchPlanner().generate(specification.search_space)
        elif specification.search_method is SearchMethod.RANDOM:
            sample_count = specification.resource_limits.get("random_sample_count")
            if (
                not isinstance(sample_count, int)
                or isinstance(sample_count, bool)
                or sample_count < 1
            ):
                raise SpecificationError(
                    "random search requires resource_limits.random_sample_count"
                )
            raw = RandomSearchPlanner().generate(
                specification.search_space,
                sample_count=sample_count,
                seed=derive_seed(specification.seeds[0], "random-search"),
            )
        else:  # pragma: no cover - exhaustive enum guard
            raise SpecificationError("unsupported search method")
        merged: list[GeneratedConfiguration] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            parameters = dict(specification.base_configuration)
            parameters.update(item.parameters)
            digest = canonical_sha256(parameters)
            if digest in seen:
                raise SpecificationError("base configuration produced duplicate candidates")
            seen.add(digest)
            merged.append(GeneratedConfiguration(index, parameters, digest))
        return tuple(merged)

    @staticmethod
    def _scenario(
        value: Mapping[str, Any],
        *,
        category: str,
        index: int,
    ) -> ScenarioDefinition:
        raw = dict(value)
        explicit_id = raw.pop("scenario_id", None)
        roles_raw = raw.pop("roles", None)
        if roles_raw is None:
            roles = frozenset(
                {
                    StageRole.VALIDATION,
                    StageRole.TEST,
                    StageRole.OOS,
                    StageRole.FINAL_UNTOUCHED,
                }
            )
        else:
            if not isinstance(roles_raw, tuple | list | set | frozenset):
                raise SpecificationError("scenario roles must be a sequence")
            try:
                roles = frozenset(StageRole(str(item)) for item in roles_raw)
            except ValueError as exc:
                raise SpecificationError("scenario contains an unknown stage role") from exc
        digest = canonical_sha256({"category": category, "overrides": raw, "roles": roles})
        scenario_id = (
            str(explicit_id) if explicit_id is not None else f"{category}-{index:03d}-{digest[:12]}"
        )
        return ScenarioDefinition(scenario_id, category, raw, roles)

    def _scenarios(self, specification: ResearchSpecification) -> tuple[ScenarioDefinition, ...]:
        scenarios = [
            ScenarioDefinition(
                "base",
                "base",
                {},
                frozenset(StageRole),
            )
        ]
        scenarios.extend(
            self._scenario(value, category="cost", index=index)
            for index, value in enumerate(specification.execution_cost_scenarios)
        )
        scenarios.extend(
            self._scenario(value, category="robustness", index=index)
            for index, value in enumerate(specification.robustness_scenarios)
        )
        ordered = tuple(sorted(scenarios, key=lambda item: item.scenario_id))
        if len({item.scenario_id for item in ordered}) != len(ordered):
            raise SpecificationError("scenario IDs must be unique")
        return ordered

    @staticmethod
    def _optimization_stages(
        specification: ResearchSpecification,
        split: PlannedSplit,
        configurations: Sequence[GeneratedConfiguration],
    ) -> tuple[PlannedOptimizationStage, ...]:
        if specification.search_method is not SearchMethod.STAGED:
            return ()
        policy = specification.staged_search_policy
        if policy is None:
            raise SpecificationError("staged search requires staged_search_policy")
        if policy.promotion_counts[0] >= len(configurations):
            raise SpecificationError(
                "the first staged promotion_count must be smaller than the grid population"
            )
        duration_microseconds = int(
            (split.train.end - split.train.start).total_seconds() * 1_000_000
        )
        stages: list[PlannedOptimizationStage] = []
        previous_end = split.train.start
        for dataset in specification.datasets:
            previous_end = split.train.start
            for stage_index, fraction in enumerate(policy.train_fractions):
                if fraction == 1.0:
                    period_end = split.train.end
                else:
                    period_end = split.train.start + timedelta(
                        microseconds=int(duration_microseconds * fraction)
                    )
                if period_end <= previous_end:
                    raise SpecificationError(
                        "staged train fractions collapse to a duplicate time boundary"
                    )
                period = TimeRange(split.train.start, period_end)
                role = f"optimization_{stage_index:03d}"
                maximum_candidates = (
                    len(configurations)
                    if stage_index == 0
                    else policy.promotion_counts[stage_index - 1]
                )
                digest = canonical_sha256(
                    {
                        "split_id": split.split_id,
                        "dataset_id": dataset.dataset_id,
                        "role": role,
                        "period": period,
                        "maximum_candidates": maximum_candidates,
                    }
                )
                stages.append(
                    PlannedOptimizationStage(
                        stage_id=f"stage-opt-{stage_index:03d}-{digest[:20]}",
                        role=role,
                        split_id=split.split_id,
                        dataset_id=dataset.dataset_id,
                        period=period,
                        stage_index=stage_index,
                        maximum_candidates=maximum_candidates,
                    )
                )
                previous_end = period_end
        return tuple(sorted(stages, key=lambda item: (item.stage_index, item.stage_id)))

    @staticmethod
    def _stages(
        specification: ResearchSpecification,
        split: PlannedSplit,
    ) -> tuple[tuple[PlannedStage, ...], tuple[PlannedStage, ...]]:
        stages: list[PlannedStage] = []
        final_stages: list[PlannedStage] = []
        if specification.walk_forward_policy is not None:
            windows = plan_walk_forward(
                specification.walk_forward_policy,
                TimeRange(split.train.start, split.test.end),
            )
            ranges: tuple[tuple[str | None, StageRole, TimeRange], ...] = tuple(
                (window.window_id, role, period)
                for window in windows
                for role, period in (
                    (StageRole.TRAIN, window.train),
                    (StageRole.VALIDATION, window.validation),
                    (StageRole.OOS, window.oos),
                )
            )
        else:
            ranges = (
                (None, StageRole.TRAIN, split.train),
                (None, StageRole.VALIDATION, split.validation),
                (None, StageRole.TEST, split.test),
            )
        for dataset in specification.datasets:
            for window_id, role, period in ranges:
                raw = {
                    "split_id": split.split_id,
                    "dataset_id": dataset.dataset_id,
                    "window_id": window_id,
                    "role": role,
                    "period": period,
                }
                digest = canonical_sha256(raw)
                prefix = window_id or split.split_id
                stages.append(
                    PlannedStage(
                        stage_id=f"stage-{prefix}-{role.value}-{digest[:12]}",
                        role=role,
                        split_id=split.split_id,
                        dataset_id=dataset.dataset_id,
                        period=period,
                        window_id=window_id,
                    )
                )
            if split.final_untouched_test is not None:
                digest = canonical_sha256(
                    {
                        "split_id": split.split_id,
                        "dataset_id": dataset.dataset_id,
                        "role": StageRole.FINAL_UNTOUCHED,
                        "period": split.final_untouched_test,
                    }
                )
                final_stages.append(
                    PlannedStage(
                        stage_id=f"stage-final-{digest[:16]}",
                        role=StageRole.FINAL_UNTOUCHED,
                        split_id=split.split_id,
                        dataset_id=dataset.dataset_id,
                        period=split.final_untouched_test,
                    )
                )
        return (
            tuple(sorted(stages, key=lambda item: item.stage_id)),
            tuple(sorted(final_stages, key=lambda item: item.stage_id)),
        )

    def _optimization_jobs(
        self,
        *,
        experiment_id: str,
        specification: ResearchSpecification,
        configurations: Sequence[GeneratedConfiguration],
        stages: Sequence[PlannedOptimizationStage],
    ) -> tuple[BacktestJob, ...]:
        repetitions = specification.resource_limits.get("repetitions", 1)
        if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
            raise SpecificationError("resource_limits.repetitions must be positive")
        jobs: list[BacktestJob] = []
        for stage in stages:
            for configuration in configurations:
                for root_seed in specification.seeds:
                    for repetition_index in range(repetitions):
                        seed = derive_seed(
                            root_seed,
                            experiment_id,
                            stage.stage_id,
                            "base",
                            configuration.sha256,
                            str(repetition_index),
                        )
                        identifier = run_id(
                            experiment_id=experiment_id,
                            stage_id=stage.stage_id,
                            split_id=stage.split_id,
                            configuration_sha256=configuration.sha256,
                            scenario_id="base",
                            seed=seed,
                            repetition_index=repetition_index,
                        )
                        jobs.append(
                            BacktestJob(
                                run_id=identifier,
                                experiment_id=experiment_id,
                                stage_id=stage.stage_id,
                                split_id=stage.split_id,
                                dataset_id=stage.dataset_id,
                                scenario_id="base",
                                seed=seed,
                                parameters=configuration.parameters,
                                output_directory=(
                                    self.output_root / experiment_id / "runs" / identifier
                                ).resolve(),
                                stage_role=stage.role,
                                period=stage.period,
                                repetition_index=repetition_index,
                                metadata={
                                    "configuration_sha256": configuration.sha256,
                                    "scenario_category": "base",
                                    "scenario_overrides": {},
                                    "root_seed": root_seed,
                                    "window_id": None,
                                    "optimization_stage_index": stage.stage_index,
                                    "maximum_candidates": stage.maximum_candidates,
                                },
                            )
                        )
        return self._ordered_limited_jobs(jobs, specification)

    def _jobs(
        self,
        *,
        experiment_id: str,
        specification: ResearchSpecification,
        configurations: Sequence[GeneratedConfiguration],
        scenarios: Sequence[ScenarioDefinition],
        stages: Sequence[PlannedStage],
    ) -> tuple[BacktestJob, ...]:
        repetitions = specification.resource_limits.get("repetitions", 1)
        if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
            raise SpecificationError("resource_limits.repetitions must be positive")
        jobs: list[BacktestJob] = []
        for stage in stages:
            for scenario in scenarios:
                if stage.role not in scenario.roles:
                    continue
                for configuration in configurations:
                    for root_seed in specification.seeds:
                        for repetition_index in range(repetitions):
                            seed = derive_seed(
                                root_seed,
                                experiment_id,
                                stage.stage_id,
                                scenario.scenario_id,
                                configuration.sha256,
                                str(repetition_index),
                            )
                            identifier = run_id(
                                experiment_id=experiment_id,
                                stage_id=stage.stage_id,
                                split_id=stage.split_id,
                                configuration_sha256=configuration.sha256,
                                scenario_id=scenario.scenario_id,
                                seed=seed,
                                repetition_index=repetition_index,
                            )
                            jobs.append(
                                BacktestJob(
                                    run_id=identifier,
                                    experiment_id=experiment_id,
                                    stage_id=stage.stage_id,
                                    split_id=stage.split_id,
                                    dataset_id=stage.dataset_id,
                                    scenario_id=scenario.scenario_id,
                                    seed=seed,
                                    parameters=configuration.parameters,
                                    output_directory=(
                                        self.output_root / experiment_id / "runs" / identifier
                                    ).resolve(),
                                    stage_role=stage.role.value,
                                    period=stage.period,
                                    repetition_index=repetition_index,
                                    metadata={
                                        "configuration_sha256": configuration.sha256,
                                        "scenario_category": scenario.category,
                                        "scenario_overrides": scenario.overrides,
                                        "root_seed": root_seed,
                                        "window_id": stage.window_id,
                                    },
                                )
                            )
        return self._ordered_limited_jobs(jobs, specification)

    @staticmethod
    def _ordered_limited_jobs(
        jobs: Sequence[BacktestJob],
        specification: ResearchSpecification,
    ) -> tuple[BacktestJob, ...]:
        ordered = tuple(sorted(jobs, key=lambda item: item.run_id))
        if len({item.run_id for item in ordered}) != len(ordered):
            raise SpecificationError("experiment plan produced duplicate run IDs")
        maximum_runs = specification.resource_limits.get("maximum_runs_per_activation", 1_000_000)
        if not isinstance(maximum_runs, int) or isinstance(maximum_runs, bool) or maximum_runs < 1:
            raise SpecificationError("resource_limits.maximum_runs_per_activation must be positive")
        if len(ordered) > maximum_runs:
            raise SpecificationError(
                f"stage activation contains {len(ordered)} runs; limit is {maximum_runs}"
            )
        return ordered
