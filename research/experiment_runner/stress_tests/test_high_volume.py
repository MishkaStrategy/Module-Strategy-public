from __future__ import annotations

import os
from pathlib import Path

from modular_research_runner import (
    BacktestJob,
    BootstrapMethod,
    BootstrapPlanner,
    SqliteExperimentRepository,
)
from modular_research_runner.scheduler import utc_now


def _scale() -> int:
    value = int(os.environ.get("RESEARCH_RUNNER_STRESS_SCALE", "1"))
    return max(value, 1)


def test_large_bootstrap_plan_is_reproducible() -> None:
    scale = _scale()
    planner = BootstrapPlanner()
    first = planner.generate(
        population_size=5_000 * scale,
        sample_count=20 * scale,
        root_seed=20260723,
        method=BootstrapMethod.MOVING_BLOCK,
        block_size=50,
    )
    second = planner.generate(
        population_size=5_000 * scale,
        sample_count=20 * scale,
        root_seed=20260723,
        method=BootstrapMethod.MOVING_BLOCK,
        block_size=50,
    )
    assert first == second
    assert len({item.sample_id for item in first}) == len(first)


def test_sqlite_registers_and_orders_large_run_set(tmp_path: Path) -> None:
    scale = _scale()
    count = 2_000 * scale
    repository = SqliteExperimentRepository(tmp_path / "stress.sqlite3")
    now = utc_now()
    repository.register_experiment(
        experiment_id="exp-stress",
        specification_sha256="a" * 64,
        manifest={"stress_scale": scale},
        created_at=now,
    )
    jobs = (
        BacktestJob(
            run_id=f"run-{index:08d}",
            experiment_id="exp-stress",
            stage_id="stage-train",
            split_id="split-stress",
            dataset_id="dataset-stress",
            scenario_id="base",
            seed=index,
            parameters={"index": index},
            output_directory=tmp_path / "runs" / str(index),
            stage_role="train",
            metadata={"configuration_sha256": f"{index:064x}"},
        )
        for index in reversed(range(count))
    )
    assert repository.register_jobs(jobs, max_attempts=2, created_at=now) == count
    states = repository.list_runs("exp-stress")
    assert len(states) == count
    assert [item.run_id for item in states] == sorted(item.run_id for item in states)
