from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from modular_research_runner.access import FinalTestAccessError
from modular_research_runner.artifacts import AtomicJsonArtifactStore
from modular_research_runner.cli import main
from modular_research_runner.engine import BacktestJob, ModularBacktestEngineAdapter
from modular_research_runner.manifest import EnvironmentManifest, GitProvenance
from modular_research_runner.models import (
    ChronologicalSplitPolicy,
    ParameterDimension,
    ParameterKind,
    SearchMethod,
    SearchSpace,
    SpecificationError,
    SplitMode,
    StagedSearchPolicy,
    TimeRange,
)
from modular_research_runner.planning import ResearchPlanBuilder, StageRole
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.registry_package import LocalResultsAggregatorPackageSink
from modular_research_runner.repository import (
    SqliteExperimentRepository,
    StateRepositoryError,
)
from modular_research_runner.results import LocalResultSink
from modular_research_runner.runner import ResearchRunner
from modular_research_runner.scheduler import LocalResearchScheduler

NOW = datetime(2026, 6, 1, tzinfo=UTC)


_CORE_RESULT_FILES = (
    "run.json",
    "metrics.json",
    "journal.json",
    "trades.csv",
    "equity.csv",
    "events.jsonl",
)
_ENGINE_RESULT_FILES = (
    "application-run.json",
    "metrics-extended.json",
    "data-validation.json",
    "restart-evidence.json",
)


def _write_engine_result_directory(root: Path, *, engine_run_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in (*_CORE_RESULT_FILES, *_ENGINE_RESULT_FILES):
        if name.endswith(".csv"):
            payload = "column\nvalue\n"
        elif name.endswith(".jsonl"):
            payload = '{"event":"done"}\n'
        else:
            payload = json.dumps({"artifact": name}, sort_keys=True) + "\n"
        (root / name).write_text(payload, encoding="utf-8")
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    (root / "artifact-manifest.json").write_text(
        json.dumps(
            {"schema_version": "1", "run_id": engine_run_id, "files": files},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _small_final_spec(specification):
    policy = ChronologicalSplitPolicy(
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
    return replace(
        specification,
        search_space=SearchSpace(
            dimensions=(ParameterDimension("ttl", ParameterKind.INTEGER, (36,)),),
            maximum_combinations=2,
        ),
        split_policy=policy,
        seeds=(101,),
        resource_limits={},
        parallelism=2,
        retry_policy={"max_attempts": 2},
        resume_policy={"lease_seconds": 30, "heartbeat_seconds": 5},
    )


def _staged_spec(specification):
    return replace(
        specification,
        search_method=SearchMethod.STAGED,
        staged_search_policy=StagedSearchPolicy(
            train_fractions=(0.25, 0.5, 1.0),
            promotion_counts=(2, 1),
        ),
        seeds=(101,),
        resource_limits={},
        parallelism=2,
        retry_policy={"max_attempts": 2},
        resume_policy={"lease_seconds": 30, "heartbeat_seconds": 5},
    )


def _runner(tmp_path: Path, calls: list[str]) -> ResearchRunner:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    artifacts = AtomicJsonArtifactStore(tmp_path / "artifacts")

    def run_function(job: BacktestJob) -> SimpleNamespace:
        calls.append(job.run_id)
        engine_run_id = f"engine-{job.run_id}"
        _write_engine_result_directory(
            job.output_directory,
            engine_run_id=engine_run_id,
        )
        return SimpleNamespace(
            run_id=engine_run_id,
            output_directory=job.output_directory,
            bars_processed=100,
            restart_verified=True,
        )

    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda job: job, run_function),
        retry_policy=RetryPolicy(max_attempts=2),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=2,
    )
    return ResearchRunner(
        repository=repository,
        artifact_store=artifacts,
        plan_builder=ResearchPlanBuilder(tmp_path / "outputs"),
        scheduler=scheduler,
        backtest_engine_version="engine-0.1",
        metric_schema_version="metrics-v1",
        module_api_version="module-api-v1-draft",
    )


def test_runner_end_to_end_with_one_time_final_gate(specification, tmp_path: Path) -> None:
    calls: list[str] = []
    runner = _runner(tmp_path, calls)
    spec = _small_final_spec(specification)
    prepared = runner.prepare(
        spec,
        git=GitProvenance(
            branch="research/experiment-runner",
            commit_sha="a" * 40,
            clean=True,
        ),
        environment=EnvironmentManifest(
            python_version="3.12.0",
            platform="test-platform",
            dependency_lock_sha256="d" * 64,
        ),
        created_at=NOW,
    )
    assert len(prepared.plan.jobs) == 1
    assert len(prepared.plan.final_stages) == 1
    assert runner.artifact_store.read_json(prepared.manifest_artifact)["experiment_id"] == (
        prepared.manifest.experiment_id
    )
    with pytest.raises(FinalTestAccessError, match="must finish"):
        runner.authorize_final_test(
            prepared,
            candidate_configuration_hashes=[prepared.plan.configurations[0].sha256],
            issued_at=NOW,
        )

    train = runner.execute(prepared.manifest.experiment_id)
    assert train.status.value == "COMPLETED"
    assert len(calls) == 1
    candidate = prepared.plan.configurations[0].sha256
    runner.activate_stage(
        prepared,
        role=StageRole.VALIDATION,
        candidate_configuration_hashes=[candidate],
        activated_at=NOW,
    )
    validation = runner.execute(prepared.manifest.experiment_id)
    assert validation.status.value == "COMPLETED"
    assert len(calls) == 2
    runner.activate_stage(
        prepared,
        role=StageRole.TEST,
        candidate_configuration_hashes=[candidate],
        activated_at=NOW,
    )
    ordinary = runner.execute(prepared.manifest.experiment_id)
    assert ordinary.status.value == "COMPLETED"
    assert len(calls) == 3
    grant = runner.authorize_final_test(
        prepared,
        candidate_configuration_hashes=[candidate],
        issued_at=NOW,
    )
    assert (
        runner.authorize_final_test(
            prepared,
            candidate_configuration_hashes=[candidate],
            issued_at=NOW.replace(day=2),
        ).grant_id
        == grant.grant_id
    )
    final_run_ids = runner.activate_final_test(
        prepared,
        grant_id=grant.grant_id,
        activated_at=NOW,
    )
    assert len(final_run_ids) == 1
    with pytest.raises(FinalTestAccessError, match="already"):
        runner.activate_final_test(prepared, grant_id=grant.grant_id, activated_at=NOW)
    final = runner.execute(prepared.manifest.experiment_id)
    assert final.status.value == "COMPLETED"
    assert len(calls) == 4
    assert set(final_run_ids) <= set(calls)

    sink = LocalResultSink(tmp_path / "result-sink")
    envelopes = runner.publish_result_envelopes(
        prepared,
        sink=sink,
        created_at=NOW,
    )
    assert len(envelopes) == 4
    roles = {sink.store.read_json(reference)["stage_role"] for reference in envelopes}
    assert roles == {"train", "validation", "test", "final_untouched"}

    packages = runner.publish_registry_packages(
        prepared,
        sink=LocalResultsAggregatorPackageSink(
            tmp_path / "registry-packages",
            producer_module_version="0.3.0",
        ),
        created_at=NOW,
    )
    assert len(packages) == 4
    assert all(
        (reference.package_directory / "artifact-package.json").is_file() for reference in packages
    )


def test_resume_verifies_existing_success_artifacts(specification, tmp_path: Path) -> None:
    calls: list[str] = []
    runner = _runner(tmp_path, calls)
    prepared = runner.prepare(
        replace(_small_final_spec(specification), split_policy=specification.split_policy),
        git=GitProvenance("branch", "b" * 40, True),
        created_at=NOW,
    )
    runner.execute(prepared.manifest.experiment_id)
    state = runner.repository.list_runs(prepared.manifest.experiment_id)[0]
    assert state.result is not None
    for child in state.result.output_directory.iterdir():
        child.unlink()
    state.result.output_directory.rmdir()
    resumed = runner.execute(prepared.manifest.experiment_id)
    assert resumed.status.value == "CORRUPTED"


def test_runner_staged_search_requires_successive_frozen_promotions(
    specification, tmp_path: Path
) -> None:
    calls: list[str] = []
    runner = _runner(tmp_path, calls)
    prepared = runner.prepare(
        _staged_spec(specification),
        git=GitProvenance("research/experiment-runner", "f" * 40, True),
        created_at=NOW,
    )
    assert len(prepared.plan.configurations) == 4
    assert {job.stage_role for job in prepared.plan.jobs} == {"optimization_000"}
    assert len(prepared.plan.jobs) == 4

    runner.execute(prepared.manifest.experiment_id)
    candidates = [item.sha256 for item in prepared.plan.configurations]
    with pytest.raises(SpecificationError, match="prerequisite is incomplete"):
        runner.activate_optimization_stage(
            prepared,
            stage_index=2,
            candidate_configuration_hashes=candidates[:1],
            activated_at=NOW,
        )

    first_promotion = runner.activate_optimization_stage(
        prepared,
        stage_index=1,
        candidate_configuration_hashes=candidates[:2],
        activated_at=NOW,
    )
    assert len(first_promotion) == 2
    runner.execute(prepared.manifest.experiment_id)
    assert (
        runner.activate_optimization_stage(
            prepared,
            stage_index=1,
            candidate_configuration_hashes=reversed(candidates[:2]),
            activated_at=NOW,
        )
        == first_promotion
    )
    assert runner.repository.experiment_status(prepared.manifest.experiment_id).value == "COMPLETED"
    with pytest.raises(StateRepositoryError, match="different frozen candidate set"):
        runner.activate_optimization_stage(
            prepared,
            stage_index=1,
            candidate_configuration_hashes=[candidates[0]],
            activated_at=NOW,
        )

    with pytest.raises(SpecificationError, match="prerequisite is incomplete"):
        runner.activate_optimization_stage(
            prepared,
            stage_index=2,
            candidate_configuration_hashes=[candidates[2]],
            activated_at=NOW,
        )

    final_candidate = candidates[0]
    second_promotion = runner.activate_optimization_stage(
        prepared,
        stage_index=2,
        candidate_configuration_hashes=[final_candidate],
        activated_at=NOW,
    )
    assert len(second_promotion) == 1
    with pytest.raises(SpecificationError, match="preceding research stage"):
        runner.activate_stage(
            prepared,
            role=StageRole.VALIDATION,
            candidate_configuration_hashes=[final_candidate],
            activated_at=NOW,
        )
    runner.execute(prepared.manifest.experiment_id)

    validation = runner.activate_stage(
        prepared,
        role=StageRole.VALIDATION,
        candidate_configuration_hashes=[final_candidate],
        activated_at=NOW,
    )
    assert len(validation) == 1
    runner.execute(prepared.manifest.experiment_id)
    test_runs = runner.activate_stage(
        prepared,
        role=StageRole.TEST,
        candidate_configuration_hashes=[final_candidate],
        activated_at=NOW,
    )
    assert len(test_runs) == 1
    runner.execute(prepared.manifest.experiment_id)
    assert len(calls) == 9
    roles = {
        runner.repository.get_job(state.run_id).stage_role
        for state in runner.repository.list_runs(prepared.manifest.experiment_id)
    }
    assert roles == {
        "optimization_000",
        "optimization_001",
        "optimization_002",
        "validation",
        "test",
    }


def test_cli_validate_plan_and_status(specification_mapping, tmp_path: Path, capsys) -> None:
    specification_path = tmp_path / "research.yaml"
    specification_path.write_text(yaml.safe_dump(specification_mapping), encoding="utf-8")
    assert main(["validate", str(specification_path)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["valid"] is True

    assert (
        main(
            [
                "plan",
                str(specification_path),
                "--output-root",
                str(tmp_path / "out"),
                "--engine-version",
                "engine-v1",
                "--metric-schema-version",
                "metrics-v1",
                "--module-api-version",
                "api-v1",
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["ordinary_run_count"] > 0
    assert planned["optimization_stage_count"] == 0
    assert planned["initial_stage_roles"] == ["train"]

    repository = SqliteExperimentRepository(tmp_path / "status.sqlite3")
    repository.register_experiment(
        experiment_id="exp-cli",
        specification_sha256="a" * 64,
        manifest={"v": 1},
        created_at=NOW,
    )
    assert main(["status", str(repository.path), "exp-cli"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"counts": {}, "experiment_id": "exp-cli", "status": "CREATED"}


def test_cli_plan_reports_staged_search_lifecycle(
    specification_mapping, tmp_path: Path, capsys
) -> None:
    staged_mapping = json.loads(json.dumps(specification_mapping))
    staged_mapping["search_method"] = "staged"
    staged_mapping["staged_search_policy"] = {
        "train_fractions": [0.25, 0.5, 1.0],
        "promotion_counts": [2, 1],
    }
    path = tmp_path / "staged.yaml"
    path.write_text(yaml.safe_dump(staged_mapping), encoding="utf-8")
    assert (
        main(
            [
                "plan",
                str(path),
                "--output-root",
                str(tmp_path / "out"),
                "--engine-version",
                "engine-v1",
                "--metric-schema-version",
                "metrics-v1",
                "--module-api-version",
                "api-v1",
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["optimization_stage_count"] == 3
    assert planned["initial_stage_roles"] == ["optimization_000"]
    assert planned["ordinary_run_count"] == 8


def test_prepare_rejects_scheduler_above_specified_worker_limit(
    specification, tmp_path: Path
) -> None:
    runner = _runner(tmp_path, [])
    restricted = replace(
        _small_final_spec(specification),
        resource_limits={"maximum_workers": 1},
        parallelism=1,
    )
    with pytest.raises(SpecificationError, match="scheduler maximum_workers exceeds"):
        runner.prepare(
            restricted,
            git=GitProvenance("branch", "c" * 40, True),
            created_at=NOW,
        )


def test_prepare_rejects_runtime_policy_drift(specification, tmp_path: Path) -> None:
    runner = _runner(tmp_path, [])
    base = _small_final_spec(specification)
    with pytest.raises(SpecificationError, match="retry_policy differs"):
        runner.prepare(
            replace(base, retry_policy={"max_attempts": 1}),
            git=GitProvenance("branch", "d" * 40, True),
            created_at=NOW,
        )
    with pytest.raises(SpecificationError, match="resume_policy differs"):
        runner.prepare(
            replace(
                base,
                resume_policy={"lease_seconds": 60, "heartbeat_seconds": 5},
            ),
            git=GitProvenance("branch", "e" * 40, True),
            created_at=NOW,
        )
