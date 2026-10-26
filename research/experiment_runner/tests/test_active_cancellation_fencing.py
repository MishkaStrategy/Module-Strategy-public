from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from modular_research_runner.engine import BacktestJob, ModularBacktestEngineAdapter
from modular_research_runner.models import ExperimentStatus
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.repository import SqliteExperimentRepository
from modular_research_runner.scheduler import CancellationToken, LocalResearchScheduler
from modular_research_runner.state import RunStatus

NOW = datetime(2026, 7, 24, tzinfo=UTC)


def test_active_future_cannot_publish_after_experiment_cancellation(
    tmp_path: Path,
) -> None:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    repository.register_experiment(
        experiment_id="exp-cancel",
        specification_sha256="a" * 64,
        manifest={"schema_version": 1},
        created_at=NOW,
    )
    job = BacktestJob(
        run_id="run-active",
        experiment_id="exp-cancel",
        stage_id="stage-train",
        split_id="split-train",
        dataset_id="dataset",
        scenario_id="base",
        seed=1,
        parameters={},
        output_directory=tmp_path / "output",
    )
    repository.register_jobs([job], max_attempts=1, created_at=NOW)

    started = threading.Event()
    release = threading.Event()
    engine_finished = threading.Event()

    def run_function(claimed_job: BacktestJob) -> SimpleNamespace:
        output = claimed_job.output_directory / claimed_job.run_id
        output.mkdir(parents=True)
        (output / "result.json").write_text("{}\n", encoding="utf-8")
        started.set()
        release.wait(timeout=10)
        engine_finished.set()
        return SimpleNamespace(
            run_id="engine-run-active",
            output_directory=output,
            bars_processed=1,
            restart_verified=True,
        )

    token = CancellationToken()
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda value: value, run_function),
        retry_policy=RetryPolicy(max_attempts=1),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=1,
        poll_interval_seconds=0.01,
        session_id_factory=lambda: "scheduler-cancel-test",
        lease_token_factory=lambda: "claim-cancel-test",
    )
    result: dict[str, object] = {}

    def run_scheduler() -> None:
        result["summary"] = scheduler.run("exp-cancel", cancellation=token)

    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.start()
    assert started.wait(timeout=5)
    token.cancel()
    scheduler_thread.join(timeout=5)
    assert not scheduler_thread.is_alive()

    summary = result["summary"]
    assert summary.status is ExperimentStatus.CANCELED
    assert summary.runs[0].status is RunStatus.CANCELED
    assert summary.runs[0].result is None

    release.set()
    assert engine_finished.wait(timeout=5)
    final = repository.get_run(job.run_id)
    assert final.status is RunStatus.CANCELED
    assert final.result is None
