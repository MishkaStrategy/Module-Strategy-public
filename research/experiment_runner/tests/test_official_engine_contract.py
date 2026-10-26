from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_research_runner import BacktestJob, ModularBacktestEngineAdapter
from modular_research_runner.output_manifest import verify_output_manifest

modular_backtest = pytest.importorskip("modular_backtest")


def test_adapter_accepts_authoritative_run_outcome_type(tmp_path: Path) -> None:
    from modular_backtest import RunOutcome

    job = BacktestJob(
        run_id="run-contract",
        experiment_id="exp-contract",
        stage_id="stage-train",
        split_id="split-contract",
        dataset_id="dataset-contract",
        scenario_id="base",
        seed=1,
        parameters={},
        output_directory=tmp_path,
    )
    output = tmp_path / "official-output"
    output.mkdir()
    (output / "result.json").write_text("{}\n", encoding="utf-8")
    outcome = RunOutcome(
        output_directory=output,
        validation=SimpleNamespace(),
        run_id="official-run",
        bars_processed=42,
        restart_verified=True,
    )
    reference = ModularBacktestEngineAdapter(
        lambda value: value,
        lambda _: outcome,
    ).execute(job)
    assert reference.requested_run_id == job.run_id
    assert reference.engine_run_id == "official-run"
    assert reference.output_directory == outcome.output_directory
    assert reference.bars_processed == 42
    assert reference.restart_verified is True
    assert verify_output_manifest(reference)
