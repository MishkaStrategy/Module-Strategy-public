from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import modular_research_runner.runner as runner_module
from modular_research_runner.access import FinalTestAccessError, FinalTestGrant
from modular_research_runner.artifacts import AtomicJsonArtifactStore
from modular_research_runner.engine import (
    BacktestJob,
    BacktestRunReference,
    ModularBacktestEngineAdapter,
)
from modular_research_runner.manifest import GitProvenance
from modular_research_runner.models import (
    ChronologicalSplitPolicy,
    ExperimentStatus,
    ParameterDimension,
    ParameterKind,
    ResearchSpecification,
    SearchMethod,
    SearchSpace,
    SpecificationError,
    SplitMode,
    StagedSearchPolicy,
    TimeRange,
)
from modular_research_runner.planning import ResearchPlanBuilder, StageRole
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.repository import SqliteExperimentRepository, StateRepositoryError
from modular_research_runner.runner import PreparedExperiment, ResearchRunner
from modular_research_runner.scheduler import LocalResearchScheduler
from modular_research_runner.state import RunStatus

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _final_spec(specification: ResearchSpecification) -> ResearchSpecification:
    return replace(
        specification,
        search_method=SearchMethod.GRID,
        search_space=SearchSpace(
            dimensions=(ParameterDimension("ttl", ParameterKind.INTEGER, (36,)),),
            maximum_combinations=2,
        ),
        split_policy=ChronologicalSplitPolicy(
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
        ),
        seeds=(101,),
        resource_limits={"maximum_workers": 1},
        parallelism=1,
        retry_policy={"max_attempts": 2},
        resume_policy={"lease_seconds": 30, "heartbeat_seconds": 5},
    )


def _staged_spec(specification: ResearchSpecification) -> ResearchSpecification:
    return replace(
        _final_spec(specification),
        search_method=SearchMethod.STAGED,
        search_space=SearchSpace(
            dimensions=(ParameterDimension("ttl", ParameterKind.INTEGER, (24, 36)),),
            maximum_combinations=2,
        ),
        staged_search_policy=StagedSearchPolicy(
            train_fractions=(0.5, 1.0),
            promotion_counts=(1,),
        ),
    )


def _runner(
    tmp_path: Path,
    specification: ResearchSpecification,
    *,
    backtest_engine_version: str = "engine-0.1",
    metric_schema_version: str = "metrics-v1",
    module_api_version: str = "module-api-v1",
) -> ResearchRunner:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")

    def run_function(job: BacktestJob) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=f"engine-{job.run_id}",
            output_directory=job.output_directory,
            bars_processed=0,
            restart_verified=True,
        )

    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda job: job, run_function),
        retry_policy=RetryPolicy.from_mapping(specification.retry_policy),
        resume_policy=ResumePolicy.from_mapping(specification.resume_policy),
        maximum_workers=1,
    )
    return ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(tmp_path / "artifacts"),
        plan_builder=ResearchPlanBuilder(tmp_path / "outputs"),
        scheduler=scheduler,
        backtest_engine_version=backtest_engine_version,
        metric_schema_version=metric_schema_version,
        module_api_version=module_api_version,
    )


def _prepare(
    runner: ResearchRunner,
    specification: ResearchSpecification,
) -> PreparedExperiment:
    return runner.prepare(
        specification,
        git=GitProvenance(
            branch="research/experiment-runner",
            commit_sha="a" * 40,
            clean=True,
        ),
        created_at=NOW,
    )


@pytest.mark.parametrize(
    "field",
    ["backtest_engine_version", "metric_schema_version", "module_api_version"],
)
def test_runner_rejects_blank_contract_versions(
    specification: ResearchSpecification,
    tmp_path: Path,
    field: str,
) -> None:
    values = {
        "backtest_engine_version": "engine-0.1",
        "metric_schema_version": "metrics-v1",
        "module_api_version": "module-api-v1",
    }
    values[field] = " "
    with pytest.raises(SpecificationError, match=f"{field} must be non-empty"):
        _runner(tmp_path, _final_spec(specification), **values)


@pytest.mark.parametrize("worker_limit", [0, True, "1"])
def test_prepare_rejects_invalid_worker_limit(
    specification: ResearchSpecification,
    tmp_path: Path,
    worker_limit: object,
) -> None:
    base = _final_spec(specification)
    invalid = replace(base, resource_limits={"maximum_workers": worker_limit})
    runner = _runner(tmp_path, invalid)
    with pytest.raises(SpecificationError, match="maximum_workers must be positive"):
        _prepare(runner, invalid)


def test_prepare_rejects_non_object_canonical_manifest(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _final_spec(specification)
    runner = _runner(tmp_path, spec)
    monkeypatch.setattr(runner_module, "canonical_value", lambda value: ())
    with pytest.raises(SpecificationError, match="manifest must canonicalize to an object"):
        _prepare(runner, spec)


def test_stage_activation_rejects_invalid_role_state_and_candidates(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _final_spec(specification)
    runner = _runner(tmp_path, spec)
    prepared = _prepare(runner, spec)
    candidate = prepared.plan.configurations[0].sha256

    with pytest.raises(SpecificationError, match="only validation, test, or OOS"):
        runner.activate_stage(
            prepared,
            role=StageRole.FINAL_UNTOUCHED,
            candidate_configuration_hashes=[candidate],
            activated_at=NOW,
        )
    with pytest.raises(SpecificationError, match="preceding research stage has not finished"):
        runner.activate_stage(
            prepared,
            role=StageRole.VALIDATION,
            candidate_configuration_hashes=[candidate],
            activated_at=NOW,
        )

    monkeypatch.setattr(
        SqliteExperimentRepository,
        "experiment_status",
        lambda self, experiment_id: ExperimentStatus.COMPLETED,
    )
    for candidates in ([], ["f" * 64]):
        with pytest.raises(SpecificationError, match="outside the frozen plan"):
            runner.activate_stage(
                prepared,
                role=StageRole.VALIDATION,
                candidate_configuration_hashes=candidates,
                activated_at=NOW,
            )


def test_optimization_activation_rejects_invalid_configuration(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_spec = _final_spec(specification)
    ordinary_runner = _runner(tmp_path / "ordinary", ordinary_spec)
    ordinary = _prepare(ordinary_runner, ordinary_spec)
    with pytest.raises(SpecificationError, match="not configured for staged search"):
        ordinary_runner.activate_optimization_stage(
            ordinary,
            stage_index=1,
            candidate_configuration_hashes=[ordinary.plan.configurations[0].sha256],
            activated_at=NOW,
        )

    staged_spec = _staged_spec(specification)
    runner = _runner(tmp_path / "staged", staged_spec)
    prepared = _prepare(runner, staged_spec)
    candidate = prepared.plan.configurations[0].sha256

    with pytest.raises(SpecificationError, match="stage_index must be an integer"):
        runner.activate_optimization_stage(
            prepared,
            stage_index=True,
            candidate_configuration_hashes=[candidate],
            activated_at=NOW,
        )
    for stage_index in (0, 2):
        with pytest.raises(SpecificationError, match="stage_index is not activatable"):
            runner.activate_optimization_stage(
                prepared,
                stage_index=stage_index,
                candidate_configuration_hashes=[candidate],
                activated_at=NOW,
            )
    with pytest.raises(SpecificationError, match="preceding optimization stage has not finished"):
        runner.activate_optimization_stage(
            prepared,
            stage_index=1,
            candidate_configuration_hashes=[candidate],
            activated_at=NOW,
        )

    monkeypatch.setattr(
        SqliteExperimentRepository,
        "experiment_status",
        lambda self, experiment_id: ExperimentStatus.COMPLETED,
    )
    for candidates in ([], ["f" * 64]):
        with pytest.raises(SpecificationError, match="outside the frozen plan"):
            runner.activate_optimization_stage(
                prepared,
                stage_index=1,
                candidate_configuration_hashes=candidates,
                activated_at=NOW,
            )


def test_prerequisite_validation_rejects_blank_role_and_missing_fingerprint(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _final_spec(specification)
    runner = _runner(tmp_path, spec)
    prepared = _prepare(runner, spec)
    candidate = prepared.plan.configurations[0].sha256

    with pytest.raises(SpecificationError, match="prerequisite role must be non-empty"):
        runner._require_successful_candidates(
            prepared.manifest.experiment_id,
            role=" ",
            candidate_configuration_hashes=[candidate],
        )

    state = SimpleNamespace(run_id="run-corrupt", status=RunStatus.SUCCEEDED)
    job = BacktestJob(
        run_id=state.run_id,
        experiment_id=prepared.manifest.experiment_id,
        stage_id="stage-train",
        split_id="split-train",
        dataset_id=prepared.specification.datasets[0].dataset_id,
        scenario_id="base",
        seed=1,
        parameters={},
        output_directory=tmp_path / "corrupt-output",
        stage_role=StageRole.TRAIN.value,
        metadata={},
    )
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "list_runs",
        lambda self, experiment_id, statuses=None: (state,),
    )
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "get_job",
        lambda self, run_id: job,
    )
    with pytest.raises(SpecificationError, match="missing its configuration fingerprint"):
        runner._require_successful_candidates(
            prepared.manifest.experiment_id,
            role=StageRole.TRAIN,
            candidate_configuration_hashes=[candidate],
        )


def test_final_authorization_rejects_unconfigured_unfrozen_and_failed_candidates(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_final_spec = replace(
        _final_spec(specification),
        split_policy=replace(_final_spec(specification).split_policy, final_untouched_test=None),
    )
    no_final_runner = _runner(tmp_path / "no-final", no_final_spec)
    no_final = _prepare(no_final_runner, no_final_spec)
    with pytest.raises(FinalTestAccessError, match="no final untouched test range"):
        no_final_runner.authorize_final_test(
            no_final,
            candidate_configuration_hashes=[no_final.plan.configurations[0].sha256],
            issued_at=NOW,
        )

    spec = _final_spec(specification)
    runner = _runner(tmp_path / "final", spec)
    prepared = _prepare(runner, spec)
    candidate = prepared.plan.configurations[0].sha256
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "experiment_status",
        lambda self, experiment_id: ExperimentStatus.COMPLETED,
    )

    for candidates in ([], ["f" * 64]):
        with pytest.raises(FinalTestAccessError, match="empty or not in the frozen plan"):
            runner.authorize_final_test(
                prepared,
                candidate_configuration_hashes=candidates,
                issued_at=NOW,
            )
    with pytest.raises(FinalTestAccessError, match="were not frozen"):
        runner.authorize_final_test(
            prepared,
            candidate_configuration_hashes=[candidate],
            issued_at=NOW,
        )

    monkeypatch.setattr(
        SqliteExperimentRepository,
        "stage_activation_candidates",
        lambda self, experiment_id, stage_role: (candidate,),
    )

    def fail_prerequisite(*args: object, **kwargs: object) -> None:
        raise SpecificationError("corrupt prerequisite")

    monkeypatch.setattr(runner, "_require_successful_candidates", fail_prerequisite)
    with pytest.raises(FinalTestAccessError, match="did not complete"):
        runner.authorize_final_test(
            prepared,
            candidate_configuration_hashes=[candidate],
            issued_at=NOW,
        )


@pytest.mark.parametrize("mismatch", ["experiment", "specification", "split"])
def test_final_activation_rejects_grant_binding_mismatch(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    spec = _final_spec(specification)
    runner = _runner(tmp_path, spec)
    prepared = _prepare(runner, spec)
    values = {
        "experiment_id": prepared.manifest.experiment_id,
        "specification_sha256": prepared.specification.fingerprint,
        "split_fingerprint": prepared.plan.split.fingerprint,
    }
    if mismatch == "experiment":
        values["experiment_id"] = "exp-other"
        message = "another experiment"
    elif mismatch == "specification":
        values["specification_sha256"] = "b" * 64
        message = "specification fingerprint has changed"
    else:
        values["split_fingerprint"] = "c" * 64
        message = "split fingerprint has changed"
    grant = FinalTestGrant.issue(
        **values,
        candidate_configuration_hashes=[prepared.plan.configurations[0].sha256],
        issued_at=NOW,
    )
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "get_final_test_grant",
        lambda self, grant_id: grant,
    )
    with pytest.raises(FinalTestAccessError, match=message):
        runner.activate_final_test(prepared, grant_id=grant.grant_id, activated_at=NOW)


def test_result_envelopes_reject_corrupt_success_state_and_job_metadata(
    specification: ResearchSpecification,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _final_spec(specification)
    runner = _runner(tmp_path, spec)
    prepared = _prepare(runner, spec)
    missing_result = SimpleNamespace(run_id="run-missing-result", result=None)
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "list_runs",
        lambda self, experiment_id, statuses=None: (missing_result,),
    )
    with pytest.raises(StateRepositoryError, match="missing its result reference"):
        runner._successful_result_envelopes(prepared, created_at=NOW)

    result = BacktestRunReference(
        requested_run_id="run-corrupt-metadata",
        engine_run_id="engine-run-corrupt-metadata",
        output_directory=tmp_path / "engine-output",
        bars_processed=1,
        restart_verified=True,
    )
    successful = SimpleNamespace(run_id=result.requested_run_id, result=result)
    job = BacktestJob(
        run_id=successful.run_id,
        experiment_id=prepared.manifest.experiment_id,
        stage_id="stage-train",
        split_id="split-train",
        dataset_id=prepared.specification.datasets[0].dataset_id,
        scenario_id="base",
        seed=1,
        parameters={},
        output_directory=tmp_path / "engine-output",
        stage_role=StageRole.TRAIN.value,
        metadata={},
    )
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "list_runs",
        lambda self, experiment_id, statuses=None: (successful,),
    )
    monkeypatch.setattr(
        SqliteExperimentRepository,
        "get_job",
        lambda self, run_id: job,
    )
    with pytest.raises(SpecificationError, match="missing its configuration fingerprint"):
        runner._successful_result_envelopes(prepared, created_at=NOW)
