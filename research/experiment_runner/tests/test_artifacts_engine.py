from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_research_runner import (
    ArtifactError,
    AtomicJsonArtifactStore,
    BacktestJob,
    EngineContractError,
    ModularBacktestEngineAdapter,
)
from modular_research_runner.output_manifest import (
    output_manifest_path,
    verify_output_manifest,
)


def _job(tmp_path: Path) -> BacktestJob:
    return BacktestJob(
        run_id="run-requested",
        experiment_id="exp-1",
        stage_id="stage-1",
        split_id="split-1",
        dataset_id="dataset-1",
        scenario_id="base",
        seed=42,
        parameters={"ttl": 36},
        output_directory=tmp_path,
    )


def test_atomic_artifact_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    store = AtomicJsonArtifactStore(tmp_path / "artifacts")
    reference = store.write_json("manifests/experiment.json", {"b": 2, "a": 1})
    assert store.read_json(reference) == {"a": 1, "b": 2}
    target = store.root / reference.relative_path
    target.write_text(json.dumps({"a": 9}), encoding="utf-8")
    with pytest.raises(ArtifactError, match="size|checksum"):
        store.read_json(reference)


def test_artifact_store_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    store = AtomicJsonArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactError, match="safe"):
        store.write_json("../escape.json", {})
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactError, match="symlink"):
        store.write_json("linked/report.json", {})


def test_engine_adapter_delegates_and_binds_output_manifest(tmp_path: Path) -> None:
    observed = []

    def config_factory(job: BacktestJob) -> dict[str, object]:
        return {"job": job.run_id, "parameters": dict(job.parameters)}

    def run_function(config):
        observed.append(config)
        output = tmp_path / "engine-run-1"
        output.mkdir()
        (output / "trades.csv").write_text("id,pnl\n1,2.5\n", encoding="utf-8")
        return SimpleNamespace(
            run_id="engine-run-1",
            output_directory=output,
            bars_processed=123,
            restart_verified=True,
        )

    result = ModularBacktestEngineAdapter(config_factory, run_function).execute(_job(tmp_path))
    assert observed == [{"job": "run-requested", "parameters": {"ttl": 36}}]
    assert result.engine_run_id == "engine-run-1"
    assert result.bars_processed == 123
    assert result.restart_verified
    assert verify_output_manifest(result)
    manifest = output_manifest_path(result)
    assert manifest.is_file()
    assert manifest.parent == result.output_directory.parent
    assert manifest not in result.output_directory.iterdir()


def test_engine_adapter_rejects_incomplete_outcome(tmp_path: Path) -> None:
    adapter = ModularBacktestEngineAdapter(
        lambda job: job,
        lambda config: SimpleNamespace(run_id="x"),
    )
    with pytest.raises(EngineContractError, match="missing"):
        adapter.execute(_job(tmp_path))


def test_engine_adapter_rejects_unmaterialized_output_directory(tmp_path: Path) -> None:
    adapter = ModularBacktestEngineAdapter(
        lambda job: job,
        lambda config: SimpleNamespace(
            run_id="engine-run",
            output_directory=tmp_path / "missing",
            bars_processed=1,
            restart_verified=True,
        ),
    )
    with pytest.raises(EngineContractError, match="verified manifest"):
        adapter.execute(_job(tmp_path))
