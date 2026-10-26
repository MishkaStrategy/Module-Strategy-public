from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from effective_config_fixture import prepared_effective_config

from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_FILENAME,
    write_sidecar_for_job,
)
from modular_research_runner.engine import BacktestJob, BacktestRunReference
from modular_research_runner.manifest import (
    EnvironmentManifest,
    ExperimentManifest,
    ExperimentManifestBuilder,
    GitProvenance,
)
from modular_research_runner.models import ResearchSpecification, SpecificationError
from modular_research_runner.planning import ExperimentPlan, ResearchPlanBuilder
from modular_research_runner.registry_package import (
    LocalResultsAggregatorPackageSink,
    RegistryPackageReference,
)
from modular_research_runner.results import RunResultEnvelope

_CORE_FILES = (
    "run.json",
    "metrics.json",
    "journal.json",
    "trades.csv",
    "equity.csv",
    "events.jsonl",
)
_ENGINE_FILES = (
    "application-run.json",
    "metrics-extended.json",
    "data-validation.json",
    "restart-evidence.json",
)
_ENGINE_MANIFEST = "artifact-manifest.json"
_OPTIONAL_ENGINE_FILES = ("replay-checkpoint.json",)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_engine_artifacts(
    root: Path,
    *,
    engine_run_id: str,
    include_replay_checkpoint: bool = True,
) -> None:
    root.mkdir(parents=True)
    for name in (*_CORE_FILES, *_ENGINE_FILES):
        if name.endswith(".csv"):
            payload = "column\nvalue\n"
        elif name.endswith(".jsonl"):
            payload = '{"event":"done"}\n'
        else:
            payload = json.dumps({"artifact": name}, sort_keys=True) + "\n"
        (root / name).write_text(payload, encoding="utf-8")
    if include_replay_checkpoint:
        (root / _OPTIONAL_ENGINE_FILES[0]).write_text(
            '{"checkpoint":"verified"}\n',
            encoding="utf-8",
        )
    files = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    (root / _ENGINE_MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": engine_run_id,
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _context(
    tmp_path: Path,
    specification: ResearchSpecification,
    *,
    dependency_lock_sha256: str | None = "d" * 64,
    clean: bool = True,
) -> tuple[
    ExperimentPlan,
    ExperimentManifest,
    BacktestJob,
    RunResultEnvelope,
    Path,
]:
    plan = ResearchPlanBuilder(tmp_path / "jobs").build(
        experiment_id="exp-registry-package",
        specification=specification,
    )
    manifest = ExperimentManifestBuilder().build(
        experiment_id=plan.experiment_id,
        specification=specification,
        configurations=plan.configurations,
        split=plan.split,
        git=GitProvenance(
            branch="research/experiment-runner",
            commit_sha="a" * 40,
            clean=clean,
            diff_sha256=None if clean else "b" * 64,
        ),
        backtest_engine_version="0.1.0",
        metric_schema_version="metrics-v1",
        module_api_version="module-api-v1",
        scheduler_configuration={"executor_mode": "thread", "maximum_workers": 1},
        environment=EnvironmentManifest(
            python_version="3.12.0",
            platform="test-platform",
            dependency_lock_sha256=dependency_lock_sha256,
        ),
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    job = plan.jobs[0]
    output = tmp_path / "engine-output"
    engine_run_id = "engine-run-001"
    _write_engine_artifacts(output, engine_run_id=engine_run_id)
    engine = BacktestRunReference(job.run_id, engine_run_id, output, 100, True)
    envelope = RunResultEnvelope(
        experiment_id=job.experiment_id,
        run_id=job.run_id,
        configuration_sha256=str(job.metadata["configuration_sha256"]),
        stage_id=job.stage_id,
        stage_role=job.stage_role,
        split_id=job.split_id,
        dataset_id=job.dataset_id,
        dataset_sha256=specification.datasets[0].sha256,
        scenario_id=job.scenario_id,
        seed=job.seed,
        engine=engine,
        metrics_schema_version="metrics-v1",
        metrics_artifact=None,
        metadata={},
        created_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
    )
    return plan, manifest, job, envelope, output


def _publish(
    tmp_path: Path,
    *,
    envelope: RunResultEnvelope,
    manifest: ExperimentManifest,
    specification: ResearchSpecification,
    job: BacktestJob,
) -> RegistryPackageReference:
    return LocalResultsAggregatorPackageSink(
        tmp_path / "registry-packages",
        producer_module_version="0.3.0",
    ).publish(
        envelope=envelope,
        experiment_manifest=manifest,
        specification=specification,
        job=job,
    )


def test_registry_package_is_deterministic_idempotent_and_source_independent(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    source_hashes = {path.name: _sha256(path) for path in output.iterdir() if path.is_file()}
    sink = LocalResultsAggregatorPackageSink(
        tmp_path / "registry-packages",
        producer_module_version="0.3.0",
    )
    first = sink.publish(
        envelope=envelope,
        experiment_manifest=manifest,
        specification=specification,
        job=job,
    )
    second = sink.publish(
        envelope=envelope,
        experiment_manifest=manifest,
        specification=specification,
        job=job,
    )
    assert first == second
    assert not (output / "artifact-package.json").exists()
    assert source_hashes == {
        path.name: _sha256(path) for path in output.iterdir() if path.is_file()
    }

    document = json.loads(
        (first.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    assert document["package_id"] == first.package_id
    assert document["schema_id"] == "modular-results-registry.artifact-package"
    assert document["producer_module_id"] == "modular-research-runner"
    expected_paths = {
        *_CORE_FILES,
        *_ENGINE_FILES,
        *_OPTIONAL_ENGINE_FILES,
        _ENGINE_MANIFEST,
    }
    assert {item["relative_path"] for item in document["artifact_inventory"]} == expected_paths
    assert {
        path.name for path in first.package_directory.iterdir() if path.is_file()
    } == expected_paths | {"artifact-package.json"}

    packaged_run = (first.package_directory / "run.json").read_bytes()
    (output / "run.json").write_text("mutated after publication", encoding="utf-8")
    assert (first.package_directory / "run.json").read_bytes() == packaged_run


def test_registry_package_rejects_incomplete_or_ambiguous_output(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path / "missing", specification)
    (output / "metrics.json").unlink()
    with pytest.raises(SpecificationError, match="missing"):
        _publish(
            tmp_path / "missing",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )

    _, manifest, job, envelope, output = _context(tmp_path / "extra", specification)
    (output / "unexpected.bin").write_bytes(b"unexpected")
    with pytest.raises(SpecificationError, match="extras"):
        _publish(
            tmp_path / "extra",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_requires_release_provenance(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, dirty_manifest, job, envelope, _ = _context(tmp_path / "dirty", specification, clean=False)
    with pytest.raises(SpecificationError, match="clean Git"):
        _publish(
            tmp_path / "dirty",
            envelope=envelope,
            manifest=dirty_manifest,
            specification=specification,
            job=job,
        )

    _, unlocked_manifest, job, envelope, _ = _context(
        tmp_path / "unlocked", specification, dependency_lock_sha256=None
    )
    with pytest.raises(SpecificationError, match="dependency_lock"):
        _publish(
            tmp_path / "unlocked",
            envelope=envelope,
            manifest=unlocked_manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_conflicting_existing_package(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, _ = _context(tmp_path, specification)
    reference = _publish(
        tmp_path,
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )
    (reference.package_directory / "artifact-package.json").write_text(
        "{}",
        encoding="utf-8",
    )
    with pytest.raises(SpecificationError, match="conflicts"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_authoritative_results_aggregator_accepts_package(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    intake = pytest.importorskip("modular_results_registry.intake")
    _, manifest, job, envelope, _ = _context(tmp_path, specification)
    reference = _publish(
        tmp_path,
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )
    profile = intake.ProducerProfile(
        producer_module_id="modular-research-runner",
        allowed_producer_schema_versions=frozenset({"1"}),
        allowed_artifact_schema_prefixes=("modular-core.", "modular-backtest."),
        required_artifact_paths=frozenset(_CORE_FILES),
    )
    verified = intake.validate_artifact_package(reference.package_directory, profile)
    assert verified.manifest.package_id == reference.package_id
    assert verified.manifest.run_id == job.run_id
    assert len(verified.verified_files) == 12


def _read_engine_manifest(output: Path) -> dict[str, object]:
    return json.loads((output / _ENGINE_MANIFEST).read_text(encoding="utf-8"))


def _write_engine_manifest(output: Path, document: dict[str, object]) -> None:
    (output / _ENGINE_MANIFEST).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("producer_version", "max_file_bytes", "max_total_bytes", "message"),
    [
        ("", 1, 1, "producer_module_version"),
        ("0.3.0", 0, 1, "max_file_bytes"),
        ("0.3.0", 1, 0, "max_total_bytes"),
    ],
)
def test_registry_package_sink_rejects_invalid_configuration(
    tmp_path: Path,
    producer_version: str,
    max_file_bytes: int,
    max_total_bytes: int,
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        LocalResultsAggregatorPackageSink(
            tmp_path / "packages",
            producer_module_version=producer_version,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )


def test_registry_package_sink_rejects_unsafe_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "packages-link"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(SpecificationError, match="symlink"):
        LocalResultsAggregatorPackageSink(
            symlink,
            producer_module_version="0.3.0",
        )

    file_root = tmp_path / "packages-file"
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SpecificationError, match="cannot create"):
        LocalResultsAggregatorPackageSink(
            file_root,
            producer_module_version="0.3.0",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing artifact-manifest"),
        ("invalid-json", "manifest is invalid"),
        ("not-object", "must be an object"),
        ("schema", "unsupported"),
        ("files", "files are invalid"),
    ],
)
def test_registry_package_rejects_invalid_engine_manifest(
    tmp_path: Path,
    specification: ResearchSpecification,
    mutation: str,
    message: str,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    manifest_path = output / _ENGINE_MANIFEST
    if mutation == "missing":
        manifest_path.unlink()
    elif mutation == "invalid-json":
        manifest_path.write_text("{", encoding="utf-8")
    elif mutation == "not-object":
        manifest_path.write_text("[]", encoding="utf-8")
    else:
        document = _read_engine_manifest(output)
        if mutation == "schema":
            document["schema_version"] = "2"
        else:
            document["files"] = {}
        _write_engine_manifest(output, document)
    with pytest.raises(SpecificationError, match=message):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_engine_manifest_identity_set_and_hash_drift(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path / "identity", specification)
    document = _read_engine_manifest(output)
    document["run_id"] = "different-engine-run"
    _write_engine_manifest(output, document)
    with pytest.raises(SpecificationError, match="run_id"):
        _publish(
            tmp_path / "identity",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )

    _, manifest, job, envelope, output = _context(tmp_path / "set", specification)
    document = _read_engine_manifest(output)
    files = document["files"]
    assert isinstance(files, dict)
    files.pop("events.jsonl")
    _write_engine_manifest(output, document)
    with pytest.raises(SpecificationError, match="file set"):
        _publish(
            tmp_path / "set",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )

    _, manifest, job, envelope, output = _context(tmp_path / "hash", specification)
    (output / "run.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(SpecificationError, match="checksum mismatch"):
        _publish(
            tmp_path / "hash",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_symlinks_and_resource_limit_overruns(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path / "symlink", specification)
    events = output / "events.jsonl"
    events.unlink()
    events.symlink_to(output / "run.json")
    with pytest.raises(SpecificationError, match="contain symlinks"):
        _publish(
            tmp_path / "symlink",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )

    _, manifest, job, envelope, _ = _context(tmp_path / "file-limit", specification)
    with pytest.raises(SpecificationError, match="exceeds limit"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "file-limit" / "packages",
            producer_module_version="0.3.0",
            max_file_bytes=1,
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )

    _, manifest, job, envelope, _ = _context(tmp_path / "total-limit", specification)
    with pytest.raises(SpecificationError, match="total byte"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "total-limit" / "packages",
            producer_module_version="0.3.0",
            max_total_bytes=1,
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_context_and_lineage_drift(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    from dataclasses import replace

    _, manifest, job, envelope, _ = _context(tmp_path, specification)
    sink = LocalResultsAggregatorPackageSink(
        tmp_path / "packages",
        producer_module_version="0.3.0",
    )
    cases = (
        (replace(job, stage_id="stage-different"), envelope, manifest, specification, "context"),
        (
            job,
            envelope,
            replace(manifest, experiment_id="exp-different"),
            specification,
            "another experiment",
        ),
        (
            job,
            envelope,
            manifest,
            replace(specification, strategy_version="changed"),
            "specification lineage",
        ),
        (
            job,
            envelope,
            replace(manifest, strategy_version="changed"),
            specification,
            "strategy lineage",
        ),
        (
            job,
            replace(envelope, configuration_sha256="f" * 64),
            manifest,
            specification,
            "configuration lineage",
        ),
        (
            job,
            replace(envelope, dataset_sha256="e" * 64),
            manifest,
            specification,
            "dataset lineage",
        ),
        (
            job,
            replace(envelope, metrics_schema_version="metrics-v2"),
            manifest,
            specification,
            "metric schema lineage",
        ),
    )
    for changed_job, changed_envelope, changed_manifest, changed_specification, message in cases:
        with pytest.raises(SpecificationError, match=message):
            sink.publish(
                envelope=changed_envelope,
                experiment_manifest=changed_manifest,
                specification=changed_specification,
                job=changed_job,
            )

    with pytest.raises(SpecificationError, match="evaluation period"):
        sink.publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=replace(job, period=None),
        )
    with pytest.raises(SpecificationError, match="non-negative"):
        sink.publish(
            envelope=replace(envelope, seed=-1),
            experiment_manifest=manifest,
            specification=specification,
            job=replace(job, seed=-1),
        )
    missing_output = tmp_path / "missing-output"
    invalid_engine = replace(envelope.engine, output_directory=missing_output)
    with pytest.raises(SpecificationError, match="real directory"):
        sink.publish(
            envelope=replace(envelope, engine=invalid_engine),
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_existing_inventory_and_artifact_conflicts(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, _ = _context(tmp_path / "inventory", specification)
    reference = _publish(
        tmp_path / "inventory",
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )
    (reference.package_directory / "events.jsonl").unlink()
    with pytest.raises(SpecificationError, match="inventory conflicts"):
        _publish(
            tmp_path / "inventory",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )

    _, manifest, job, envelope, _ = _context(tmp_path / "artifact", specification)
    reference = _publish(
        tmp_path / "artifact",
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )
    (reference.package_directory / "events.jsonl").write_text(
        "tampered",
        encoding="utf-8",
    )
    with pytest.raises(SpecificationError, match="artifact conflicts"):
        _publish(
            tmp_path / "artifact",
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_accepts_authoritative_backtest_engine_output(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    pytest.importorskip("modular_backtest")
    from dataclasses import replace
    from datetime import timedelta
    from inspect import signature

    from modular_backtest.config import BacktestAppConfig, DataSourceConfig, OutputConfig
    from modular_backtest.data import ValidationSummary
    from modular_backtest.provenance import SourceProvenance
    from modular_backtest.recovery import ReplayRun
    from modular_backtest.results import verify_artifact_manifest
    from modular_backtest.runner import _write_result_files
    from modular_core import BacktestEngine, Candle, MarketView, ModuleDecision, StrategyModule

    class NoopModule(StrategyModule):
        name = "registry_contract_noop"

        def on_bar(self, view: MarketView) -> ModuleDecision:
            return ModuleDecision()

    bars = [
        Candle(
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
            100.0 + index,
            101.0 + index,
            99.0 + index,
            100.5 + index,
            1.0,
            "BTCUSDT",
            "5m",
        )
        for index in range(3)
    ]
    result = BacktestEngine(NoopModule()).run(bars)
    replay = ReplayRun(
        result=result,
        checkpoint=None,
        restart_verified=True,
        continuous_sha256="c" * 64,
        restarted_sha256="c" * 64,
    )
    validation = ValidationSummary(
        path=str(tmp_path / "bars.csv"),
        sha256="d" * 64,
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=len(bars),
        first_timestamp=bars[0].timestamp.isoformat(),
        last_timestamp=bars[-1].timestamp.isoformat(),
        gaps=0,
        duplicates=0,
        requested_start=None,
        requested_end=None,
    )
    config = BacktestAppConfig(
        data=DataSourceConfig(path=tmp_path / "bars.csv", format="csv", symbol="BTCUSDT"),
        modules=tmp_path / "modules.yaml",
        requested_timeframes=("15m",),
        output=OutputConfig(tmp_path / "unused-results"),
        seed=7,
    )
    provenance = SourceProvenance(
        application_version="0.1.0",
        distribution_version="0.4.0",
        application_source_sha256="a" * 64,
        core_source_sha256="b" * 64,
        application_identity_sha256="c" * 64,
        source_commit_sha="d" * 40,
    )
    official_output = tmp_path / "official-engine-output"
    official_output.mkdir()
    write_kwargs = {
        "result": result,
        "replay": replay,
        "validation": validation,
        "config": config,
        "provenance": provenance,
    }
    if "performance" in signature(_write_result_files).parameters:
        from modular_backtest.scalability import PerformanceConfig

        write_kwargs["performance"] = PerformanceConfig()
    _write_result_files(official_output, **write_kwargs)
    verified_source_manifest = verify_artifact_manifest(official_output)

    _, manifest, job, envelope, _ = _context(tmp_path / "research", specification)
    official_engine = replace(
        envelope.engine,
        engine_run_id=result.run_manifest.run_id,
        output_directory=official_output,
        bars_processed=result.bars_processed,
        restart_verified=True,
    )
    reference = _publish(
        tmp_path / "research",
        envelope=replace(envelope, engine=official_engine),
        manifest=manifest,
        specification=specification,
        job=job,
    )
    assert verify_artifact_manifest(official_output) == verified_source_manifest
    package_document = json.loads(
        (reference.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    packaged_paths = {item["relative_path"] for item in package_document["artifact_inventory"]}
    assert packaged_paths == set(verified_source_manifest["files"]) | {_ENGINE_MANIFEST}
    assert not (official_output / "artifact-package.json").exists()


def test_registry_package_carries_v2_effective_config_sidecar(
    tmp_path: Path, specification: ResearchSpecification
) -> None:
    _, manifest, job, envelope, _ = _context(tmp_path, specification)
    job.output_directory.mkdir(parents=True)
    assert job.period is not None
    prepared = prepared_effective_config(
        tmp_path,
        run_id=job.run_id,
        context_overrides={
            "experiment_id": job.experiment_id,
            "stage_id": job.stage_id,
            "stage_role": job.stage_role,
            "split_id": job.split_id,
            "dataset_id": job.dataset_id,
            "dataset_sha256": envelope.dataset_sha256,
            "scenario_id": job.scenario_id,
            "seed": job.seed,
            "period_start": job.period.start.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "period_end": job.period.end.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "configuration_sha256": envelope.configuration_sha256,
        },
    )
    artifact = write_sidecar_for_job(
        job.output_directory,
        prepared,
        engine_run_id=envelope.engine.engine_run_id,
    )
    v2_envelope = replace(
        envelope,
        engine=replace(envelope.engine, effective_config_artifact=artifact),
        schema_version=2,
    )
    package = _publish(
        tmp_path,
        envelope=v2_envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )
    document = json.loads(
        (package.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    inventory = {item["relative_path"]: item for item in document["artifact_inventory"]}
    assert inventory[EFFECTIVE_CONFIG_FILENAME]["sha256"] == artifact.sha256
    assert inventory[EFFECTIVE_CONFIG_FILENAME]["schema_id"] == (
        "modular-backtest.effective-backtest-config"
    )
    assert inventory[EFFECTIVE_CONFIG_FILENAME]["schema_version"] == "3"
    packaged_artifact = package.package_directory / EFFECTIVE_CONFIG_FILENAME
    source_artifact = (
        job.output_directory.parent
        / f".{job.output_directory.name}.runner-artifacts"
        / EFFECTIVE_CONFIG_FILENAME
    )
    assert packaged_artifact.read_bytes() == source_artifact.read_bytes()

    source_artifact.write_bytes(source_artifact.read_bytes() + b" ")
    with pytest.raises(SpecificationError, match="missing or invalid"):
        _publish(
            tmp_path / "tampered",
            envelope=v2_envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )
