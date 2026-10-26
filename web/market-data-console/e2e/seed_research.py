from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from modular_research_runner import (
    BacktestJob,
    BacktestRunReference,
    ExperimentStatus,
    FailureCategory,
    FailureRecord,
    ReadOnlyExperimentRepository,
    RunStatus,
    SqliteExperimentRepository,
)

EXPERIMENT_ID = "browser-research-experiment"
SUCCESS_RUN_ID = "browser-run-a-success"
FAILED_RUN_ID = "browser-run-b-failed"
NOW = datetime(2026, 8, 7, 10, 45, tzinfo=UTC)


def _job(run_id: str, output_root: Path, *, scenario_id: str) -> BacktestJob:
    return BacktestJob(
        run_id=run_id,
        experiment_id=EXPERIMENT_ID,
        stage_id="walk-forward",
        split_id="split-1",
        dataset_id="registered-dataset-1",
        scenario_id=scenario_id,
        seed=7,
        parameters={"private_parameter": "never-project-this"},
        output_directory=output_root / run_id,
        stage_role="validation",
        repetition_index=0,
        metadata={"private_metadata": "never-project-this"},
    )


def seed(path: Path) -> None:
    repository = SqliteExperimentRepository(path)
    repository.register_experiment(
        experiment_id=EXPERIMENT_ID,
        specification_sha256="a" * 64,
        manifest={"private_manifest": "never-project-this"},
        created_at=NOW,
    )
    jobs = (
        _job(SUCCESS_RUN_ID, path.parent / "private-output", scenario_id="baseline"),
        _job(FAILED_RUN_ID, path.parent / "private-output", scenario_id="severe-costs"),
    )
    if repository.register_jobs(jobs, max_attempts=1, created_at=NOW) != 2:
        raise RuntimeError("Research E2E jobs were not registered")
    if repository.queue_planned(EXPERIMENT_ID, at=NOW) != 2:
        raise RuntimeError("Research E2E jobs were not queued")

    success_claim = repository.claim_next(
        experiment_id=EXPERIMENT_ID,
        worker_id="private-worker",
        session_id="private-session-success",
        lease_token="private-lease-success",
        at=NOW,
        lease_seconds=60,
    )
    if success_claim is None:
        raise RuntimeError("Research success run was not claimed")
    success_job, success_state, success_lease = success_claim
    if success_job.run_id != SUCCESS_RUN_ID or success_state.status is not RunStatus.RUNNING:
        raise RuntimeError("Research success claim selected the wrong run")
    repository.succeed(
        SUCCESS_RUN_ID,
        worker_id="private-worker",
        session_id="private-session-success",
        lease_token="private-lease-success",
        expected_revision=success_lease.revision,
        result=BacktestRunReference(
            requested_run_id=SUCCESS_RUN_ID,
            engine_run_id="engine-browser-success",
            output_directory=path.parent / "private-result-output",
            bars_processed=144,
            restart_verified=True,
        ),
        at=NOW,
    )

    failed_claim = repository.claim_next(
        experiment_id=EXPERIMENT_ID,
        worker_id="private-worker",
        session_id="private-session-failed",
        lease_token="private-lease-failed",
        at=NOW,
        lease_seconds=60,
    )
    if failed_claim is None:
        raise RuntimeError("Research failed run was not claimed")
    failed_job, failed_state, failed_lease = failed_claim
    if failed_job.run_id != FAILED_RUN_ID or failed_state.status is not RunStatus.RUNNING:
        raise RuntimeError("Research failed claim selected the wrong run")
    repository.fail(
        FAILED_RUN_ID,
        worker_id="private-worker",
        session_id="private-session-failed",
        lease_token="private-lease-failed",
        expected_revision=failed_lease.revision,
        failure=FailureRecord(
            category=FailureCategory.ENGINE,
            message="PRIVATE failure message must never be projected",
            occurred_at=NOW,
            retryable=False,
            exception_type="PrivateEngineError",
        ),
        retry_at=None,
        at=NOW,
    )
    repository.set_experiment_status(
        EXPERIMENT_ID,
        ExperimentStatus.COMPLETED_WITH_FAILURES,
        at=NOW,
    )

    reader = ReadOnlyExperimentRepository(path)
    experiment = reader.get_experiment(EXPERIMENT_ID)
    runs = reader.list_runs(EXPERIMENT_ID)
    by_id = {run.run_id: run for run in runs}
    if experiment.status is not ExperimentStatus.COMPLETED_WITH_FAILURES:
        raise RuntimeError("Research E2E experiment status mismatch")
    if by_id[SUCCESS_RUN_ID].status is not RunStatus.SUCCEEDED:
        raise RuntimeError("Research E2E success run status mismatch")
    if by_id[FAILED_RUN_ID].status is not RunStatus.FAILED:
        raise RuntimeError("Research E2E failed run status mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-path", type=Path, required=True)
    args = parser.parse_args()
    state_path = args.state_path.expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    seed(state_path)
    print(
        f"research_experiment_id={EXPERIMENT_ID} "
        f"success_run_id={SUCCESS_RUN_ID} failed_run_id={FAILED_RUN_ID}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
