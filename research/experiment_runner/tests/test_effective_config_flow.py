from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from effective_config_fixture import prepared_effective_config

from modular_research_runner.canonical import canonical_json_bytes
from modular_research_runner.effective_config import (
    read_sidecar_for_job,
    sidecar_path_for_job,
    write_sidecar_for_job,
)
from modular_research_runner.engine import (
    BacktestJob,
    EngineContractError,
    ModularBacktestEngineAdapter,
)
from modular_research_runner.models import SpecificationError, TimeRange
from modular_research_runner.scheduler import verify_output_directory


def _write_performance_evidence(output: Path, run_id: str, performance: Any) -> None:
    (output / "application-run.json").write_text(
        json.dumps({"core_run_id": run_id, "performance": asdict(performance)}),
        encoding="utf-8",
    )


def test_engine_adapter_binds_opted_in_projection_to_run_outcome(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    job_output = (tmp_path / "jobs" / "runner-run").resolve()
    job_output.mkdir(parents=True)
    job = BacktestJob(
        run_id="runner-run",
        experiment_id="experiment-001",
        stage_id="stage-001",
        stage_role="validation",
        split_id="split-001",
        dataset_id="dataset-001",
        scenario_id="base",
        seed=7,
        repetition_index=0,
        parameters={},
        output_directory=job_output,
        period=TimeRange(start, end),
        metadata={"configuration_sha256": "b" * 64},
    )
    prepared = prepared_effective_config(tmp_path, run_id=job.run_id)
    received: list[object] = []

    def run_function(config: Any, *, performance: Any) -> SimpleNamespace:
        received.append(config)
        engine_output = job_output / "engine-results"
        engine_output.mkdir()
        (engine_output / "synthetic.json").write_text("{}", encoding="utf-8")
        _write_performance_evidence(engine_output, "engine-run-001", performance)
        return SimpleNamespace(
            run_id="engine-run-001",
            output_directory=engine_output,
            bars_processed=10,
            restart_verified=False,
        )

    result = ModularBacktestEngineAdapter(lambda _: prepared, run_function).execute(job)
    assert len(received) == 1 and received[0] is prepared.config
    assert result.effective_config_artifact is not None
    payload, document = read_sidecar_for_job(job_output, result.effective_config_artifact)
    assert payload
    assert document["context"]["engine_run_id"] == "engine-run-001"
    assert document["context"]["configuration_sha256"] == "b" * 64
    assert verify_output_directory(result, job=job)

    sidecar = sidecar_path_for_job(job_output)
    sidecar.unlink()
    assert not verify_output_directory(result, job=job)
    sidecar.write_bytes(payload)
    sidecar.write_bytes(sidecar.read_bytes() + b"tampered")
    assert not verify_output_directory(result, job=job)


def test_engine_adapter_freezes_registered_reader_options_during_execution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", raising=False)
    monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", raising=False)
    job_output = (tmp_path / "jobs" / "runner-run").resolve()
    job_output.mkdir(parents=True)
    job = BacktestJob(
        run_id="runner-run",
        experiment_id="experiment-001",
        stage_id="stage-001",
        stage_role="validation",
        split_id="split-001",
        dataset_id="dataset-001",
        scenario_id="base",
        seed=7,
        repetition_index=0,
        parameters={},
        output_directory=job_output,
        period=TimeRange(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        metadata={"configuration_sha256": "b" * 64},
    )
    prepared = prepared_effective_config(tmp_path, run_id=job.run_id)
    source = prepared.effective_configuration["data_source"]
    if source["source_kind"] != "registered":
        pytest.skip("installed Backtest profile does not support registered data")
    observed_options: list[tuple[str, int | None]] = []

    def run_function(config: Any, *, performance: Any) -> SimpleNamespace:
        monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", "legacy_materialized")
        monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", raising=False)
        from modular_backtest.streaming_data import resolve_registered_data_options

        observed_options.append(resolve_registered_data_options(config.data))
        engine_output = job_output / "engine-results"
        engine_output.mkdir()
        (engine_output / "synthetic.json").write_text("{}", encoding="utf-8")
        _write_performance_evidence(engine_output, "engine-run-001", performance)
        return SimpleNamespace(
            run_id="engine-run-001",
            output_directory=engine_output,
            bars_processed=10,
            restart_verified=False,
        )

    ModularBacktestEngineAdapter(lambda _: prepared, run_function).execute(job)

    assert observed_options == [(source["registered_mode"], source["stream_batch_size"])]


def test_legacy_run_reference_canonical_bytes_omit_opt_in_field(tmp_path: Path) -> None:
    from modular_research_runner.engine import BacktestRunReference

    payload = canonical_json_bytes(BacktestRunReference("run", "engine", tmp_path, 0, False))
    assert b"effective_config_artifact" not in payload


def test_engine_adapter_rejects_projection_for_different_actual_config(tmp_path: Path) -> None:
    job_output = (tmp_path / "jobs" / "runner-run").resolve()
    job_output.mkdir(parents=True)
    job = BacktestJob(
        run_id="runner-run",
        experiment_id="experiment-001",
        stage_id="stage-001",
        stage_role="validation",
        split_id="split-001",
        dataset_id="dataset-001",
        scenario_id="base",
        seed=7,
        repetition_index=0,
        parameters={},
        output_directory=job_output,
        period=TimeRange(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        metadata={"configuration_sha256": "b" * 64},
    )
    prepared = prepared_effective_config(tmp_path, run_id=job.run_id)
    mismatched = replace(prepared, config=replace(prepared.config, initial_capital=2000.0))
    called = False

    def run_function(config: object, *, performance: Any) -> SimpleNamespace:
        nonlocal called
        called = True
        raise AssertionError("mismatched configuration must fail before engine callback")

    adapter = ModularBacktestEngineAdapter(lambda _: mismatched, run_function)
    with pytest.raises(EngineContractError, match="differs from its actual typed config"):
        adapter.execute(job)
    assert not called
    assert not (job_output.parent / f".{job_output.name}.runner-artifacts").exists()


def test_engine_adapter_rejects_config_mutated_during_callback(tmp_path: Path) -> None:
    job_output = (tmp_path / "jobs" / "runner-run").resolve()
    job_output.mkdir(parents=True)
    job = BacktestJob(
        run_id="runner-run",
        experiment_id="experiment-001",
        stage_id="stage-001",
        stage_role="validation",
        split_id="split-001",
        dataset_id="dataset-001",
        scenario_id="base",
        seed=7,
        repetition_index=0,
        parameters={},
        output_directory=job_output,
        period=TimeRange(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        metadata={"configuration_sha256": "b" * 64},
    )
    prepared = prepared_effective_config(tmp_path, run_id=job.run_id)

    def run_function(config: object, *, performance: Any) -> SimpleNamespace:
        object.__setattr__(config, "execution", {**config.execution, "slippage_bps": 99.0})
        return SimpleNamespace(
            run_id="engine-run-001",
            output_directory=job_output,
            bars_processed=10,
            restart_verified=False,
        )

    adapter = ModularBacktestEngineAdapter(lambda _: prepared, run_function)
    with pytest.raises(EngineContractError, match="changed during engine execution"):
        adapter.execute(job)
    assert not (job_output.parent / f".{job_output.name}.runner-artifacts").exists()


def test_effective_config_sidecar_missing_and_modified_files_fail_closed(tmp_path: Path) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="sidecar-run")
    output = tmp_path / "sidecar-run-output"
    output.mkdir()
    reference = write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
    sidecar = sidecar_path_for_job(output)

    sidecar.unlink()
    with pytest.raises(SpecificationError, match="effective config artifact is missing"):
        read_sidecar_for_job(output, reference)

    write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
    sidecar.write_bytes(b"tampered\n")
    with pytest.raises(
        SpecificationError,
        match="effective config artifact size or type changed|hash changed",
    ):
        read_sidecar_for_job(output, reference)


def test_effective_config_sidecar_identical_rewrite_is_idempotent(tmp_path: Path) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="idempotent-sidecar-run")
    output = tmp_path / "idempotent-sidecar-output"
    output.mkdir()

    first_reference = write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
    sidecar = sidecar_path_for_job(output)
    original_payload = sidecar.read_bytes()

    second_reference = write_sidecar_for_job(output, prepared, engine_run_id="engine-run")

    assert second_reference == first_reference
    assert sidecar.read_bytes() == original_payload


def test_effective_config_sidecar_symlinks_and_conflicting_rewrite_fail_closed(
    tmp_path: Path,
) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="symlink-run")
    output = tmp_path / "symlink-run-output"
    output.mkdir()
    reference = write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
    sidecar = sidecar_path_for_job(output)
    sidecar.unlink()
    external = tmp_path / "outside-effective-config.json"
    external.write_bytes(b"{}\n")
    sidecar.symlink_to(external)
    with pytest.raises(SpecificationError, match="effective config artifact cannot be a symlink"):
        write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
    with pytest.raises(SpecificationError, match="effective config artifact size or type changed"):
        read_sidecar_for_job(output, reference)

    sidecar.unlink()
    write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
    changed_configuration = json.loads(prepared._effective_configuration_bytes)
    changed_configuration["app"]["initial_capital"] = 2000.0
    changed_configuration["engine"]["initial_cash"] = 2000.0
    changed = replace(prepared, effective_configuration=changed_configuration)
    with pytest.raises(SpecificationError, match="existing effective config artifact differs"):
        write_sidecar_for_job(output, changed, engine_run_id="engine-run")


def test_effective_config_output_location_rejects_symlinked_job_or_artifact_dir(
    tmp_path: Path,
) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="location-run")
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(SpecificationError, match="job output directory must be a real directory"):
        sidecar_path_for_job(linked_output)
    with pytest.raises(SpecificationError, match="job output directory must be an absolute path"):
        sidecar_path_for_job(Path("relative-output"))

    output = tmp_path / "job-output"
    output.mkdir()
    artifact_directory = output.parent / f".{output.name}.runner-artifacts"
    artifact_directory.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(SpecificationError, match="artifact directory cannot be a symlink"):
        write_sidecar_for_job(output, prepared, engine_run_id="engine-run")
