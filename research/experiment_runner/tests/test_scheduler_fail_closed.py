from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from effective_config_fixture import prepared_effective_config

from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_FILENAME,
    EffectiveConfigArtifactReference,
    canonical_json_bytes,
    projection_sha256,
    render_effective_config_document,
    sidecar_path_for_job,
    write_sidecar_for_job,
)
from modular_research_runner.engine import (
    BacktestJob,
    BacktestRunReference,
    EngineContractError,
    ModularBacktestEngineAdapter,
)
from modular_research_runner.models import ExperimentStatus, SpecificationError, TimeRange
from modular_research_runner.output_manifest import write_output_manifest
from modular_research_runner.policies import ResumePolicy
from modular_research_runner.scheduler import (
    ExecutorMode,
    LocalResearchScheduler,
    SchedulerError,
    SchedulerSummary,
    default_failure_classifier,
    verify_output_directory,
)
from modular_research_runner.state import FailureCategory, RunStatus

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _engine(tmp_path: Path) -> ModularBacktestEngineAdapter:
    def run_function(job: BacktestJob) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=f"engine-{job.run_id}",
            output_directory=tmp_path,
            bars_processed=0,
            restart_verified=True,
        )

    return ModularBacktestEngineAdapter(lambda job: job, run_function)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (MemoryError("memory"), FailureCategory.RESOURCE),
        (TimeoutError("timeout"), FailureCategory.TRANSIENT),
        (ConnectionError("connection"), FailureCategory.TRANSIENT),
        (InterruptedError("interrupted"), FailureCategory.TRANSIENT),
        (EngineContractError("engine"), FailureCategory.ENGINE),
        (ValueError("value"), FailureCategory.INPUT),
        (TypeError("type"), FailureCategory.INPUT),
        (FileNotFoundError("missing"), FailureCategory.INPUT),
        (PermissionError("permission"), FailureCategory.INPUT),
        (OSError("resource"), FailureCategory.RESOURCE),
        (RuntimeError("unknown"), FailureCategory.UNKNOWN),
    ],
)
def test_default_failure_classifier_covers_all_categories(
    error: BaseException,
    category: FailureCategory,
) -> None:
    assert default_failure_classifier(error) is category


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"maximum_workers": 0}, "maximum_workers must be positive"),
        ({"maximum_workers": True}, "maximum_workers must be positive"),
        ({"maximum_workers": 1.5}, "maximum_workers must be positive"),
        ({"poll_interval_seconds": 0.0}, "poll_interval_seconds must be positive"),
        ({"poll_interval_seconds": True}, "poll_interval_seconds must be positive"),
        ({"poll_interval_seconds": math.nan}, "poll_interval_seconds must be positive"),
        ({"poll_interval_seconds": math.inf}, "poll_interval_seconds must be positive"),
        ({"poll_interval_seconds": "0.1"}, "poll_interval_seconds must be positive"),
        ({"executor_mode": "invalid"}, "executor_mode must be thread or process"),
        ({"executor_mode": []}, "executor_mode must be thread or process"),
        ({"retry_policy": {}}, "retry_policy must be a RetryPolicy"),
        ({"resume_policy": {}}, "resume_policy must be a ResumePolicy"),
        ({"clock": None}, "clock must be callable"),
        ({"sleep": None}, "sleep must be callable"),
        ({"failure_classifier": None}, "failure_classifier must be callable"),
        ({"result_verifier": None}, "result_verifier must be callable"),
        ({"session_id_factory": None}, "session_id_factory must be callable"),
        ({"lease_token_factory": None}, "lease_token_factory must be callable"),
    ],
)
def test_scheduler_constructor_rejects_invalid_runtime_configuration(
    tmp_path: Path,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        LocalResearchScheduler(
            repository=SimpleNamespace(),
            engine=_engine(tmp_path),
            **kwargs,
        )


def test_scheduler_summary_counts_terminal_states() -> None:
    runs = (
        SimpleNamespace(status=RunStatus.SUCCEEDED),
        SimpleNamespace(status=RunStatus.SUCCEEDED),
        SimpleNamespace(status=RunStatus.FAILED),
        SimpleNamespace(status=RunStatus.CORRUPTED),
    )
    summary = SchedulerSummary(
        experiment_id="exp-summary",
        status=ExperimentStatus.COMPLETED_WITH_FAILURES,
        runs=runs,
    )
    assert summary.counts == {
        RunStatus.SUCCEEDED: 2,
        RunStatus.FAILED: 1,
        RunStatus.CORRUPTED: 1,
    }


@dataclass
class _RepositoryDouble:
    final_statuses: tuple[RunStatus, ...]
    successful_resume_states: tuple[SimpleNamespace, ...] = ()
    jobs: dict[str, BacktestJob] | None = None

    def __post_init__(self) -> None:
        self.status_updates: list[ExperimentStatus] = []
        self.corruption_updates: list[tuple[str, str]] = []
        self.jobs = {} if self.jobs is None else self.jobs

    def get_job(self, run_id: str) -> BacktestJob:
        return self.jobs[run_id]

    def list_runs(
        self,
        experiment_id: str,
        statuses: tuple[RunStatus, ...] | None = None,
    ) -> tuple[SimpleNamespace, ...]:
        if statuses == (RunStatus.SUCCEEDED,):
            return self.successful_resume_states
        return tuple(
            SimpleNamespace(run_id=f"run-{index}", status=status)
            for index, status in enumerate(self.final_statuses)
        )

    def recover_expired_leases(self, *, experiment_id: str, at: datetime) -> int:
        return 0

    def queue_planned(self, experiment_id: str, *, at: datetime) -> int:
        return 0

    def set_experiment_status(
        self,
        experiment_id: str,
        status: ExperimentStatus,
        *,
        at: datetime,
    ) -> None:
        self.status_updates.append(status)

    def claim_next(
        self,
        *,
        experiment_id: str,
        worker_id: str,
        session_id: str,
        lease_token: str,
        at: datetime,
        lease_seconds: int,
    ) -> None:
        return None

    def has_nonterminal_runs(self, experiment_id: str) -> bool:
        return False

    def cancel_pending(self, experiment_id: str, *, at: datetime) -> int:
        return 0

    def mark_corrupted(self, run_id: str, *, detail: str, at: datetime) -> None:
        self.corruption_updates.append((run_id, detail))


@pytest.mark.parametrize(
    ("run_statuses", "expected"),
    [
        ((RunStatus.CORRUPTED,), ExperimentStatus.CORRUPTED),
        ((RunStatus.BLOCKED,), ExperimentStatus.BLOCKED),
        (
            (RunStatus.SUCCEEDED, RunStatus.FAILED),
            ExperimentStatus.COMPLETED_WITH_FAILURES,
        ),
        ((), ExperimentStatus.COMPLETED),
    ],
)
def test_scheduler_classifies_terminal_repository_state(
    tmp_path: Path,
    run_statuses: tuple[RunStatus, ...],
    expected: ExperimentStatus,
) -> None:
    repository = _RepositoryDouble(run_statuses)
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        resume_policy=ResumePolicy(
            lease_seconds=30,
            heartbeat_seconds=5,
            recover_expired_leases=False,
            verify_success_artifacts=False,
        ),
        executor_mode=ExecutorMode.THREAD,
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )
    summary = scheduler.run("exp-terminal")
    assert summary.status is expected
    assert repository.status_updates == [ExperimentStatus.RUNNING, expected]


def test_scheduler_rejects_success_state_without_result_reference(tmp_path: Path) -> None:
    repository = _RepositoryDouble(
        (),
        successful_resume_states=(SimpleNamespace(run_id="run-corrupt-success", result=None),),
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )
    with pytest.raises(SchedulerError, match="missing its result reference"):
        scheduler.run("exp-corrupt-success")


@pytest.mark.parametrize("sidecar_damage", ["missing", "tampered"])
def test_scheduler_marks_success_with_invalid_sidecar_corrupted(
    tmp_path: Path,
    sidecar_damage: str,
) -> None:
    requested_run_id = "run-corrupt-sidecar"
    engine_run_id = "engine-run-corrupt-sidecar"
    job_root = tmp_path / "job-root"
    output = job_root / "engine-results" / engine_run_id
    output.mkdir(parents=True)
    (output / "synthetic-result.json").write_text("{}", encoding="utf-8")
    prepared = prepared_effective_config(
        tmp_path,
        run_id=requested_run_id,
    )
    reference = write_sidecar_for_job(
        job_root,
        prepared,
        engine_run_id=engine_run_id,
        result_output_directory=output,
    )
    result = BacktestRunReference(
        requested_run_id=requested_run_id,
        engine_run_id=engine_run_id,
        output_directory=output,
        bars_processed=0,
        restart_verified=False,
        effective_config_artifact=reference,
    )
    write_output_manifest(result)
    sidecar_path = sidecar_path_for_job(job_root)
    if sidecar_damage == "missing":
        sidecar_path.unlink()
    else:
        sidecar_path.write_text("tampered", encoding="utf-8")
    repository = _RepositoryDouble(
        (RunStatus.CORRUPTED,),
        successful_resume_states=(SimpleNamespace(run_id=result.requested_run_id, result=result),),
        jobs={requested_run_id: _job_for_prepared(prepared, job_root)},
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )

    summary = scheduler.run("exp-corrupt-sidecar")

    assert repository.corruption_updates == [
        ("run-corrupt-sidecar", "successful run artifact verification failed")
    ]
    assert summary.status is ExperimentStatus.CORRUPTED


def test_scheduler_recovers_success_with_nested_output_and_job_sidecar(tmp_path: Path) -> None:
    requested_run_id = "run-valid-nested"
    engine_run_id = "engine-run-valid-nested"
    job_root = tmp_path / "job-root"
    output = job_root / "engine-results" / engine_run_id
    output.mkdir(parents=True)
    (output / "synthetic-result.json").write_text("{}", encoding="utf-8")
    prepared = prepared_effective_config(
        tmp_path,
        run_id=requested_run_id,
    )
    reference = write_sidecar_for_job(
        job_root,
        prepared,
        engine_run_id=engine_run_id,
        result_output_directory=output,
    )
    result = BacktestRunReference(
        requested_run_id=requested_run_id,
        engine_run_id=engine_run_id,
        output_directory=output,
        bars_processed=0,
        restart_verified=False,
        effective_config_artifact=reference,
    )
    write_output_manifest(result)
    repository = _RepositoryDouble(
        (RunStatus.SUCCEEDED,),
        successful_resume_states=(SimpleNamespace(run_id=requested_run_id, result=result),),
        jobs={requested_run_id: _job_for_prepared(prepared, job_root)},
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )

    summary = scheduler.run("exp-valid-nested")

    assert repository.corruption_updates == []
    assert summary.status is ExperimentStatus.COMPLETED
    assert summary.runs[0].status is RunStatus.SUCCEEDED


def _persisted_v2_fixture(
    tmp_path: Path,
    *,
    damage: str | None = None,
    outside_job_root: bool = False,
) -> tuple[BacktestJob, BacktestRunReference]:
    run_id = "legacy-v2-run"
    engine_run_id = "legacy-v2-engine"
    job_root = tmp_path / "jobs" / run_id
    job_root.mkdir(parents=True)
    output = (
        tmp_path / "outside" / engine_run_id
        if outside_job_root
        else job_root / "engine-results" / engine_run_id
    )
    output.mkdir(parents=True)
    (output / "synthetic-result.json").write_text("{}", encoding="utf-8")
    prepared = prepared_effective_config(tmp_path, run_id=run_id)
    document = json.loads(render_effective_config_document(prepared, engine_run_id=engine_run_id))
    document["schema_version"] = "2"
    app = document["effective_configuration"]["app"]
    app.pop("max_base_history")
    app.pop("max_timeframe_history")
    app.pop("performance")
    if damage == "invalid":
        app["unexpected-v3-field"] = True
    elif damage == "context":
        document["context"]["stage_id"] = "different-stage"
    document["projection_sha256"] = projection_sha256(document)
    payload = canonical_json_bytes(document)
    sidecar_path = sidecar_path_for_job(job_root)
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_bytes(payload)
    reference = EffectiveConfigArtifactReference(
        relative_path=EFFECTIVE_CONFIG_FILENAME,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    if damage == "tampered":
        sidecar_path.write_bytes(payload + b" ")
    result = BacktestRunReference(
        requested_run_id=run_id,
        engine_run_id=engine_run_id,
        output_directory=output,
        bars_processed=1,
        restart_verified=False,
        effective_config_artifact=reference,
    )
    if damage == "result-identity":
        result = replace(result, requested_run_id="different-requested-run")
    write_output_manifest(result)
    context = document["context"]
    job = BacktestJob(
        run_id=run_id,
        experiment_id=context["experiment_id"],
        stage_id=prepared.context["stage_id"],
        split_id=context["split_id"],
        dataset_id=context["dataset_id"],
        scenario_id=context["scenario_id"],
        seed=context["seed"],
        parameters={},
        output_directory=job_root,
        stage_role=context["stage_role"],
        period=TimeRange(
            datetime.fromisoformat(context["period_start"].replace("Z", "+00:00")),
            datetime.fromisoformat(context["period_end"].replace("Z", "+00:00")),
        ),
        repetition_index=context["repetition_index"],
        metadata={"configuration_sha256": context["configuration_sha256"]},
    )
    return job, result


def test_scheduler_resumes_persisted_v2_success_using_job_root(tmp_path: Path) -> None:
    job, result = _persisted_v2_fixture(tmp_path)
    repository = _RepositoryDouble(
        (RunStatus.SUCCEEDED,),
        successful_resume_states=(SimpleNamespace(run_id=job.run_id, result=result),),
        jobs={job.run_id: job},
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )

    summary = scheduler.run(job.experiment_id)

    assert repository.corruption_updates == []
    assert summary.status is ExperimentStatus.COMPLETED
    assert summary.runs[0].status is RunStatus.SUCCEEDED


@pytest.mark.parametrize(
    "damage",
    [
        "tampered",
        "invalid",
        "context",
        "configuration-hash",
        "missing-configuration-hash",
        "period",
        "result-identity",
        "symlink-root",
        "outside",
    ],
)
def test_scheduler_marks_invalid_persisted_v2_success_corrupted(
    tmp_path: Path, damage: str
) -> None:
    job, result = _persisted_v2_fixture(
        tmp_path,
        damage=None if damage == "outside" else damage,
        outside_job_root=damage == "outside",
    )
    if damage == "configuration-hash":
        job = replace(job, metadata={"configuration_sha256": "f" * 64})
    elif damage == "missing-configuration-hash":
        job = replace(job, metadata={})
    elif damage == "period":
        assert job.period is not None
        job = replace(
            job,
            period=TimeRange(
                job.period.start.replace(minute=job.period.start.minute + 1),
                job.period.end,
            ),
        )
    elif damage == "symlink-root":
        symlink_root = tmp_path / "job-root-link"
        symlink_root.symlink_to(job.output_directory, target_is_directory=True)
        job = replace(job, output_directory=symlink_root)
    repository = _RepositoryDouble(
        (RunStatus.CORRUPTED,),
        successful_resume_states=(SimpleNamespace(run_id=job.run_id, result=result),),
        jobs={job.run_id: job},
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )

    summary = scheduler.run(job.experiment_id)

    assert repository.corruption_updates == [
        (job.run_id, "successful run artifact verification failed")
    ]
    assert summary.status is ExperimentStatus.CORRUPTED


def test_scheduler_keeps_custom_unary_result_verifier_contract(tmp_path: Path) -> None:
    result = BacktestRunReference(
        requested_run_id="custom-verifier-run",
        engine_run_id="custom-verifier-engine",
        output_directory=tmp_path,
        bars_processed=0,
        restart_verified=False,
    )
    repository = _RepositoryDouble(
        (RunStatus.SUCCEEDED,),
        successful_resume_states=(SimpleNamespace(run_id=result.requested_run_id, result=result),),
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=_engine(tmp_path),
        result_verifier=lambda verified_result: verified_result is result,
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )

    summary = scheduler.run("custom-verifier-experiment")

    assert repository.corruption_updates == []
    assert summary.status is ExperimentStatus.COMPLETED


def _job_for_prepared(prepared, job_root: Path) -> BacktestJob:
    context = prepared.context
    return BacktestJob(
        run_id=context["run_id"], experiment_id=context["experiment_id"],
        stage_id=context["stage_id"], stage_role=context["stage_role"],
        split_id=context["split_id"], dataset_id=context["dataset_id"],
        scenario_id=context["scenario_id"], seed=context["seed"],
        repetition_index=context["repetition_index"], parameters={},
        output_directory=job_root,
        period=TimeRange(
            datetime.fromisoformat(context["period_start"].replace("Z", "+00:00")),
            datetime.fromisoformat(context["period_end"].replace("Z", "+00:00")),
        ),
        metadata={"configuration_sha256": context["configuration_sha256"]},
    )


@pytest.mark.parametrize("field,value", [
    ("experiment_id", "wrong-experiment"), ("stage_id", "wrong-stage"),
    ("stage_role", "search"), ("split_id", "wrong-split"),
    ("dataset_id", "wrong-dataset"), ("scenario_id", "wrong-scenario"),
    ("seed", 99), ("repetition_index", 9),
    ("configuration_sha256", "e" * 64),
    ("period_start", "2025-12-31T00:00:00.000000Z"),
    ("period_end", "2026-01-03T00:00:00.000000Z"),
])
def test_v3_resume_rejects_rehashed_wrong_job_context(tmp_path: Path, field, value) -> None:
    job_root = tmp_path / "job"
    output = job_root / "results"
    output.mkdir(parents=True)
    (output / "synthetic.json").write_text("{}")
    prepared = prepared_effective_config(tmp_path, run_id="v3-resume")
    job = _job_for_prepared(prepared, job_root)
    reference = write_sidecar_for_job(job_root, prepared, engine_run_id="engine-v3",
                                     result_output_directory=output)
    result = BacktestRunReference("v3-resume", "engine-v3", output, 0, False, reference)
    write_output_manifest(result)
    assert verify_output_directory(result, job=job)
    path = sidecar_path_for_job(job_root)
    document = json.loads(path.read_bytes())
    document["context"][field] = value
    document["projection_sha256"] = projection_sha256(document)
    payload = canonical_json_bytes(document)
    path.write_bytes(payload)
    reference = replace(reference, sha256=hashlib.sha256(payload).hexdigest(),
                        size_bytes=len(payload))
    result = replace(result, effective_config_artifact=reference)
    # Even a caller that can recompute both checksums cannot change durable job identity.
    assert not verify_output_directory(result, job=job)
