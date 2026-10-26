from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_research_runner.access import FinalTestAccessError, FinalTestGrant
from modular_research_runner.engine import BacktestJob
from modular_research_runner.models import (
    ChronologicalSplitPolicy,
    SearchMethod,
    SpecificationError,
    SplitMode,
    StagedSearchPolicy,
    TimeRange,
    WalkForwardMode,
    WalkForwardPolicy,
)
from modular_research_runner.planning import (
    PlannedOptimizationStage,
    ResearchPlanBuilder,
    StageRole,
)
from modular_research_runner.repository import SqliteExperimentRepository

NOW = datetime(2026, 4, 1, tzinfo=UTC)


def _final_policy() -> ChronologicalSplitPolicy:
    return ChronologicalSplitPolicy(
        mode=SplitMode.FIXED,
        train=TimeRange(
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2021, 1, 1, tzinfo=UTC),
        ),
        validation=TimeRange(
            datetime(2021, 1, 2, tzinfo=UTC),
            datetime(2022, 1, 1, tzinfo=UTC),
        ),
        test=TimeRange(
            datetime(2022, 1, 2, tzinfo=UTC),
            datetime(2023, 1, 1, tzinfo=UTC),
        ),
        final_untouched_test=TimeRange(
            datetime(2023, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        ),
        gap_seconds=86_400,
    )


def test_grid_plan_is_deterministic_and_excludes_final_test(specification, tmp_path: Path) -> None:
    builder = ResearchPlanBuilder(tmp_path / "outputs")
    first = builder.build(experiment_id="exp-a", specification=specification)
    second = builder.build(experiment_id="exp-a", specification=specification)
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert len(first.configurations) == 4
    assert len(first.scenarios) == 1
    assert {stage.role for stage in first.stages} == {
        StageRole.TRAIN,
        StageRole.VALIDATION,
        StageRole.TEST,
    }
    assert len(first.jobs) == 8
    assert first.final_stages == ()
    assert all(job.period is not None for job in first.jobs)
    assert [job.run_id for job in first.jobs] == sorted(job.run_id for job in first.jobs)


def test_scenarios_apply_only_to_evaluation_roles_by_default(specification, tmp_path: Path) -> None:
    extended = replace(
        specification,
        execution_cost_scenarios=({"scenario_id": "severe-cost", "fee_bps": 12},),
    )
    plan = ResearchPlanBuilder(tmp_path).build(experiment_id="exp-cost", specification=extended)
    assert len(plan.jobs) == 8
    assert all(job.stage_role == "train" for job in plan.jobs)
    validation = ResearchPlanBuilder(tmp_path).build_stage_jobs(
        plan=plan,
        specification=extended,
        role=StageRole.VALIDATION,
        candidate_configuration_hashes=[item.sha256 for item in plan.configurations],
    )
    test = ResearchPlanBuilder(tmp_path).build_stage_jobs(
        plan=plan,
        specification=extended,
        role=StageRole.TEST,
        candidate_configuration_hashes=[item.sha256 for item in plan.configurations],
    )
    severe = [job for job in validation + test if job.scenario_id == "severe-cost"]
    assert len(validation) == 16
    assert len(test) == 16
    assert {job.stage_role for job in severe} == {"validation", "test"}
    assert all(job.metadata["scenario_overrides"] == {"fee_bps": 12} for job in severe)


def test_final_jobs_require_frozen_known_candidates(specification, tmp_path: Path) -> None:
    final_spec = replace(specification, split_policy=_final_policy())
    builder = ResearchPlanBuilder(tmp_path)
    plan = builder.build(experiment_id="exp-final", specification=final_spec)
    assert len(plan.final_stages) == 1
    assert all(job.stage_role != "final_untouched" for job in plan.jobs)
    candidate = plan.configurations[0].sha256
    jobs = builder.build_final_jobs(
        plan=plan,
        specification=final_spec,
        candidate_configuration_hashes=[candidate, candidate],
    )
    assert len(jobs) == 2  # two root seeds
    assert all(job.stage_role == "final_untouched" for job in jobs)
    assert all(job.metadata["configuration_sha256"] == candidate for job in jobs)
    with pytest.raises(SpecificationError, match="unknown"):
        builder.build_final_jobs(
            plan=plan,
            specification=final_spec,
            candidate_configuration_hashes=["missing"],
        )


def test_random_single_and_staged_search_modes(specification, tmp_path: Path) -> None:
    builder = ResearchPlanBuilder(tmp_path)
    random_spec = replace(
        specification,
        search_method=SearchMethod.RANDOM,
        resource_limits={"random_sample_count": 2},
    )
    random_plan = builder.build(experiment_id="exp-random", specification=random_spec)
    assert len(random_plan.configurations) == 2
    with pytest.raises(SpecificationError, match="random_sample_count"):
        builder.build(
            experiment_id="exp-random-bad",
            specification=replace(
                specification,
                search_method=SearchMethod.RANDOM,
                resource_limits={},
            ),
        )
    single = builder.build(
        experiment_id="exp-single",
        specification=replace(specification, search_method=SearchMethod.SINGLE),
    )
    assert len(single.configurations) == 1
    assert dict(single.configurations[0].parameters) == dict(specification.base_configuration)
    staged = builder.build(
        experiment_id="exp-staged",
        specification=replace(
            specification,
            search_method=SearchMethod.STAGED,
            staged_search_policy=StagedSearchPolicy(
                train_fractions=(0.25, 0.5, 1.0),
                promotion_counts=(2, 1),
            ),
        ),
    )
    assert len(staged.configurations) == 4
    assert staged.stages == tuple(
        stage for stage in staged.stages if stage.role is not StageRole.TRAIN
    )
    assert {job.stage_role for job in staged.jobs} == {"optimization_000"}
    assert [stage.stage_index for stage in staged.optimization_stages] == [0, 1, 2]
    assert [stage.maximum_candidates for stage in staged.optimization_stages] == [4, 2, 1]
    assert [stage.period.end for stage in staged.optimization_stages] == sorted(
        stage.period.end for stage in staged.optimization_stages
    )
    candidate_hashes = [item.sha256 for item in staged.configurations]
    stage_one = builder.build_optimization_stage_jobs(
        plan=staged,
        specification=replace(
            specification,
            search_method=SearchMethod.STAGED,
            staged_search_policy=StagedSearchPolicy(
                train_fractions=(0.25, 0.5, 1.0),
                promotion_counts=(2, 1),
            ),
        ),
        stage_index=1,
        candidate_configuration_hashes=candidate_hashes[:2],
    )
    assert {job.stage_role for job in stage_one} == {"optimization_001"}
    with pytest.raises(SpecificationError, match="at most 1"):
        builder.build_optimization_stage_jobs(
            plan=staged,
            specification=replace(
                specification,
                search_method=SearchMethod.STAGED,
                staged_search_policy=StagedSearchPolicy(
                    train_fractions=(0.25, 0.5, 1.0),
                    promotion_counts=(2, 1),
                ),
            ),
            stage_index=2,
            candidate_configuration_hashes=candidate_hashes[:2],
        )


def test_walk_forward_plan_uses_oos_windows(specification, tmp_path: Path) -> None:
    policy = WalkForwardPolicy(
        mode=WalkForwardMode.ROLLING,
        train_seconds=365 * 86_400,
        validation_seconds=90 * 86_400,
        oos_seconds=90 * 86_400,
        step_seconds=180 * 86_400,
        purge_seconds=86_400,
        embargo_seconds=86_400,
    )
    plan = ResearchPlanBuilder(tmp_path).build(
        experiment_id="exp-wf",
        specification=replace(specification, walk_forward_policy=policy),
    )
    assert StageRole.OOS in {stage.role for stage in plan.stages}
    assert StageRole.TEST not in {stage.role for stage in plan.stages}
    assert all(stage.window_id is not None for stage in plan.stages)


def test_staged_search_fails_closed_for_population_and_time_resolution(
    specification, tmp_path: Path
) -> None:
    builder = ResearchPlanBuilder(tmp_path)
    with pytest.raises(SpecificationError, match="smaller than the grid population"):
        builder.build(
            experiment_id="exp-staged-population",
            specification=replace(
                specification,
                search_method=SearchMethod.STAGED,
                staged_search_policy=StagedSearchPolicy(
                    train_fractions=(0.5, 1.0),
                    promotion_counts=(4,),
                ),
            ),
        )

    start = datetime(2020, 1, 1, tzinfo=UTC)
    tiny_split = ChronologicalSplitPolicy(
        mode=SplitMode.FIXED,
        train=TimeRange(start, start + timedelta(microseconds=2)),
        validation=TimeRange(start + timedelta(seconds=1), start + timedelta(seconds=2)),
        test=TimeRange(start + timedelta(seconds=3), start + timedelta(seconds=4)),
    )
    with pytest.raises(SpecificationError, match="duplicate time boundary"):
        builder.build(
            experiment_id="exp-staged-resolution",
            specification=replace(
                specification,
                search_method=SearchMethod.STAGED,
                split_policy=tiny_split,
                staged_search_policy=StagedSearchPolicy(
                    train_fractions=(0.1, 0.2, 1.0),
                    promotion_counts=(2, 1),
                ),
            ),
        )


def test_staged_job_builder_rejects_invalid_activation_requests(
    specification, tmp_path: Path
) -> None:
    staged_spec = replace(
        specification,
        search_method=SearchMethod.STAGED,
        staged_search_policy=StagedSearchPolicy(
            train_fractions=(0.25, 0.5, 1.0),
            promotion_counts=(2, 1),
        ),
    )
    builder = ResearchPlanBuilder(tmp_path)
    plan = builder.build(experiment_id="exp-staged-invalid", specification=staged_spec)
    with pytest.raises(SpecificationError, match="not activatable"):
        builder.build_optimization_stage_jobs(
            plan=plan,
            specification=staged_spec,
            stage_index=0,
            candidate_configuration_hashes=[plan.configurations[0].sha256],
        )
    with pytest.raises(SpecificationError, match="must be an integer"):
        builder.build_optimization_stage_jobs(
            plan=plan,
            specification=staged_spec,
            stage_index=True,
            candidate_configuration_hashes=[plan.configurations[0].sha256],
        )
    with pytest.raises(SpecificationError, match="frozen candidates"):
        builder.build_optimization_stage_jobs(
            plan=plan,
            specification=staged_spec,
            stage_index=1,
            candidate_configuration_hashes=[],
        )
    with pytest.raises(SpecificationError, match="unknown configurations"):
        builder.build_optimization_stage_jobs(
            plan=plan,
            specification=staged_spec,
            stage_index=1,
            candidate_configuration_hashes=["f" * 64],
        )


def test_planned_optimization_stage_rejects_boolean_integer_fields() -> None:
    period = TimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(SpecificationError, match="role/index mismatch"):
        PlannedOptimizationStage(
            stage_id="stage-opt",
            role="optimization_001",
            split_id="split",
            dataset_id="dataset",
            period=period,
            stage_index=True,
            maximum_candidates=1,
        )
    with pytest.raises(SpecificationError, match="maximum_candidates"):
        PlannedOptimizationStage(
            stage_id="stage-opt",
            role="optimization_001",
            split_id="split",
            dataset_id="dataset",
            period=period,
            stage_index=1,
            maximum_candidates=True,
        )


def test_scenario_validation_rejects_unknown_roles_and_duplicates(
    specification, tmp_path: Path
) -> None:
    builder = ResearchPlanBuilder(tmp_path)
    with pytest.raises(SpecificationError, match="unknown stage role"):
        builder.build(
            experiment_id="exp-bad-role",
            specification=replace(
                specification,
                robustness_scenarios=({"roles": ["future"], "x": 1},),
            ),
        )
    with pytest.raises(SpecificationError, match="unique"):
        builder.build(
            experiment_id="exp-duplicate",
            specification=replace(
                specification,
                robustness_scenarios=(
                    {"scenario_id": "same", "x": 1},
                    {"scenario_id": "same", "x": 2},
                ),
            ),
        )


def test_final_grant_repository_is_one_time_and_atomic(specification, tmp_path: Path) -> None:
    final_spec = replace(specification, split_policy=_final_policy(), seeds=(1,))
    plan = ResearchPlanBuilder(tmp_path / "outputs").build(
        experiment_id="exp-grant", specification=final_spec
    )
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    repository.register_experiment(
        experiment_id="exp-grant",
        specification_sha256=final_spec.fingerprint,
        manifest={"v": 1},
        created_at=NOW,
    )
    candidate = plan.configurations[0].sha256
    grant = FinalTestGrant.issue(
        experiment_id="exp-grant",
        specification_sha256=final_spec.fingerprint,
        split_fingerprint=plan.split.fingerprint,
        candidate_configuration_hashes=[candidate],
        issued_at=NOW,
    )
    assert repository.issue_final_test_grant(grant) == grant
    assert repository.issue_final_test_grant(grant) == grant
    jobs = ResearchPlanBuilder(tmp_path / "outputs").build_final_jobs(
        plan=plan,
        specification=final_spec,
        candidate_configuration_hashes=[candidate],
    )
    consumed, inserted = repository.activate_final_test_grant(
        grant.grant_id,
        jobs=jobs,
        max_attempts=2,
        activated_at=NOW,
    )
    assert consumed.consumed_at == NOW
    assert inserted == len(jobs)
    with pytest.raises(FinalTestAccessError, match="already"):
        repository.activate_final_test_grant(
            grant.grant_id,
            jobs=jobs,
            max_attempts=2,
            activated_at=NOW,
        )


def test_invalid_final_job_does_not_consume_grant(specification, tmp_path: Path) -> None:
    final_spec = replace(specification, split_policy=_final_policy(), seeds=(1,))
    builder = ResearchPlanBuilder(tmp_path)
    plan = builder.build(experiment_id="exp-atomic", specification=final_spec)
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    repository.register_experiment(
        experiment_id="exp-atomic",
        specification_sha256=final_spec.fingerprint,
        manifest={"v": 1},
        created_at=NOW,
    )
    candidate = plan.configurations[0].sha256
    grant = repository.issue_final_test_grant(
        FinalTestGrant.issue(
            experiment_id="exp-atomic",
            specification_sha256=final_spec.fingerprint,
            split_fingerprint=plan.split.fingerprint,
            candidate_configuration_hashes=[candidate],
            issued_at=NOW,
        )
    )
    ordinary = plan.jobs[0]
    invalid = BacktestJob(
        run_id="invalid-final",
        experiment_id=ordinary.experiment_id,
        stage_id=ordinary.stage_id,
        split_id=ordinary.split_id,
        dataset_id=ordinary.dataset_id,
        scenario_id=ordinary.scenario_id,
        seed=ordinary.seed,
        parameters=ordinary.parameters,
        output_directory=ordinary.output_directory,
        stage_role="train",
        period=ordinary.period,
        metadata={"configuration_sha256": candidate},
    )
    with pytest.raises(FinalTestAccessError, match="only final"):
        repository.activate_final_test_grant(
            grant.grant_id,
            jobs=[invalid],
            max_attempts=1,
            activated_at=NOW,
        )
    assert repository.get_final_test_grant(grant.grant_id).consumed_at is None


def test_plan_builder_and_stage_activation_validation_edges(specification, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    target = tmp_path / "target"
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(SpecificationError, match="symlink"):
        ResearchPlanBuilder(linked)

    builder = ResearchPlanBuilder(tmp_path / "real")
    plan = builder.build(experiment_id="exp-edge", specification=specification)
    candidate = plan.configurations[0].sha256
    with pytest.raises(SpecificationError, match="initial plan"):
        builder.build_stage_jobs(
            plan=plan,
            specification=specification,
            role=StageRole.TRAIN,
            candidate_configuration_hashes=[candidate],
        )
    with pytest.raises(SpecificationError, match="final-test"):
        builder.build_stage_jobs(
            plan=plan,
            specification=specification,
            role=StageRole.FINAL_UNTOUCHED,
            candidate_configuration_hashes=[candidate],
        )
    with pytest.raises(SpecificationError, match="requires frozen"):
        builder.build_stage_jobs(
            plan=plan,
            specification=specification,
            role=StageRole.VALIDATION,
            candidate_configuration_hashes=[],
        )
    with pytest.raises(SpecificationError, match="unknown"):
        builder.build_stage_jobs(
            plan=plan,
            specification=specification,
            role=StageRole.VALIDATION,
            candidate_configuration_hashes=["unknown"],
        )
    with pytest.raises(SpecificationError, match="no final"):
        builder.build_final_jobs(
            plan=plan,
            specification=specification,
            candidate_configuration_hashes=[candidate],
        )
    with pytest.raises(SpecificationError, match="roles must be a sequence"):
        builder.build(
            experiment_id="exp-role-sequence",
            specification=replace(
                specification,
                execution_cost_scenarios=({"roles": "test", "x": 1},),
            ),
        )
    with pytest.raises(SpecificationError, match="repetitions"):
        builder.build(
            experiment_id="exp-repetition",
            specification=replace(specification, resource_limits={"repetitions": 0}),
        )


def test_plan_enforces_per_activation_run_limit(specification, tmp_path: Path) -> None:
    builder = ResearchPlanBuilder(tmp_path)
    with pytest.raises(SpecificationError, match="contains 8 runs; limit is 7"):
        builder.build(
            experiment_id="exp-run-limit",
            specification=replace(
                specification,
                resource_limits={"maximum_runs_per_activation": 7},
            ),
        )
    with pytest.raises(SpecificationError, match="must be positive"):
        builder.build(
            experiment_id="exp-invalid-run-limit",
            specification=replace(
                specification,
                resource_limits={"maximum_runs_per_activation": 0},
            ),
        )
