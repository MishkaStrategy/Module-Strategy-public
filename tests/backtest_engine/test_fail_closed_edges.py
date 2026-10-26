from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from modular_backtest import provenance, results, runner
from modular_backtest.config import (
    BacktestAppConfig,
    ConfigurationError,
    DataSourceConfig,
    OutputConfig,
    _initial_capital,
    _mapping,
    _optional_datetime,
    _timeframes,
    _utc_datetime,
    _yaml_path,
)
from modular_backtest.provenance import ProvenanceError
from modular_backtest.results import ResultError, write_artifact_manifest


def _artifact_directory(path: Path, run_id: str = "run-edge") -> Path:
    path.mkdir()
    (path / "run.json").write_text(
        json.dumps({"manifest": {"run_id": run_id}}),
        encoding="utf-8",
    )
    (path / "metrics.json").write_text("{}", encoding="utf-8")
    write_artifact_manifest(path, run_id)
    return path


def _rewrite_manifest(path: Path, **changes: object) -> None:
    manifest_path = path / "artifact-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.update(changes)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


def test_json_mapping_rejects_invalid_json_and_non_mapping(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ResultError, match="cannot read valid"):
        results._json_mapping(invalid, "invalid")

    sequence = tmp_path / "sequence.json"
    sequence.write_text("[]", encoding="utf-8")
    with pytest.raises(ResultError, match="JSON object"):
        results._json_mapping(sequence, "sequence")


def test_artifact_scan_rejects_symlinks_and_empty_directories(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "payload.txt").write_text("payload", encoding="utf-8")
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(ResultError, match="result directory must not be a symlink"):
        results._artifact_files(linked_directory)

    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir()
    (artifact_directory / "payload.txt").write_text("payload", encoding="utf-8")
    (artifact_directory / "linked.txt").symlink_to(artifact_directory / "payload.txt")
    with pytest.raises(ResultError, match="must not contain symlinks"):
        results._artifact_files(artifact_directory)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ResultError, match="contains no artifacts"):
        results._artifact_files(empty)


def test_manifest_writer_validates_directory_run_id_and_existing_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(ResultError, match="does not exist"):
        write_artifact_manifest(tmp_path / "missing", "run")

    directory = tmp_path / "result"
    directory.mkdir()
    (directory / "payload.txt").write_text("payload", encoding="utf-8")
    with pytest.raises(ResultError, match="run_id must be non-empty"):
        write_artifact_manifest(directory, "")
    with pytest.raises(ResultError, match="run_id must be non-empty"):
        write_artifact_manifest(directory, cast(str, None))

    write_artifact_manifest(directory, "run")
    with pytest.raises(ResultError, match="already exists"):
        write_artifact_manifest(directory, "run")

    linked_manifest_directory = tmp_path / "linked-manifest"
    linked_manifest_directory.mkdir()
    payload = linked_manifest_directory / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    (linked_manifest_directory / "artifact-manifest.json").symlink_to(payload)
    with pytest.raises(ResultError, match="already exists"):
        write_artifact_manifest(linked_manifest_directory, "run")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": "2"}, "schema_version"),
        ({"run_id": ""}, "run_id"),
        ({"files": []}, "non-empty mapping"),
        ({"files": {}}, "non-empty mapping"),
    ],
)
def test_manifest_verifier_rejects_invalid_top_level_contracts(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    directory = _artifact_directory(tmp_path / message.replace(" ", "-"))
    _rewrite_manifest(directory, **changes)
    with pytest.raises(ResultError, match=message):
        results.verify_artifact_manifest(directory)


@pytest.mark.parametrize("name", ["", "bad\\name", "/absolute", "../escape"])
def test_manifest_verifier_rejects_all_unsafe_path_forms(tmp_path: Path, name: str) -> None:
    directory = _artifact_directory(tmp_path / f"unsafe-{abs(hash(name))}")
    _rewrite_manifest(directory, files={name: "0" * 64})
    with pytest.raises(ResultError, match="unsafe path"):
        results.verify_artifact_manifest(directory)


@pytest.mark.parametrize("digest", [123, "0" * 63, "G" * 64])
def test_manifest_verifier_rejects_invalid_digest_forms(
    tmp_path: Path,
    digest: object,
) -> None:
    directory = _artifact_directory(tmp_path / f"digest-{abs(hash(str(digest)))}")
    _rewrite_manifest(directory, files={"run.json": digest})
    with pytest.raises(ResultError, match="invalid SHA-256"):
        results.verify_artifact_manifest(directory)


def test_inspect_requires_core_files_and_mapping_payloads(tmp_path: Path) -> None:
    missing_metrics = tmp_path / "missing-metrics"
    missing_metrics.mkdir()
    (missing_metrics / "run.json").write_text("{}", encoding="utf-8")
    write_artifact_manifest(missing_metrics, "run")
    with pytest.raises(ResultError, match="run.json and metrics.json"):
        results.inspect_result(missing_metrics)

    invalid_manifest = tmp_path / "invalid-manifest"
    invalid_manifest.mkdir()
    (invalid_manifest / "run.json").write_text(
        json.dumps({"manifest": []}),
        encoding="utf-8",
    )
    (invalid_manifest / "metrics.json").write_text("{}", encoding="utf-8")
    write_artifact_manifest(invalid_manifest, "run")
    with pytest.raises(ResultError, match="manifest must be a JSON object"):
        results.inspect_result(invalid_manifest)

    invalid_metrics = tmp_path / "invalid-metrics"
    invalid_metrics.mkdir()
    (invalid_metrics / "run.json").write_text(
        json.dumps({"manifest": {"run_id": "run"}}),
        encoding="utf-8",
    )
    (invalid_metrics / "metrics.json").write_text("[]", encoding="utf-8")
    write_artifact_manifest(invalid_metrics, "run")
    with pytest.raises(ResultError, match="metrics.json must contain a JSON object"):
        results.inspect_result(invalid_metrics)


def test_recovery_evidence_writes_optional_checkpoint(tmp_path: Path) -> None:
    checkpoint = SimpleNamespace(to_dict=lambda: {"bars_processed": 3})
    replay = SimpleNamespace(
        checkpoint=checkpoint,
        evidence=lambda: {"restart_verified": True},
    )

    runner._write_recovery_evidence(tmp_path, replay)

    assert json.loads((tmp_path / "restart-evidence.json").read_text())["restart_verified"]
    assert json.loads((tmp_path / "replay-checkpoint.json").read_text()) == {"bars_processed": 3}


def test_publish_rejects_symlink_and_non_directory_destinations(tmp_path: Path) -> None:
    staging = _artifact_directory(tmp_path / "staging")
    target = tmp_path / "target"
    target.mkdir()
    destination = tmp_path / "destination"
    destination.symlink_to(target, target_is_directory=True)
    with pytest.raises(runner.ResultPublicationError, match="must not be a symlink"):
        runner._publish_result_directory(staging, destination)

    destination.unlink()
    destination.write_text("not a directory", encoding="utf-8")
    with pytest.raises(runner.ResultPublicationError, match="not a directory"):
        runner._publish_result_directory(staging, destination)


def test_publish_recovers_from_atomic_rename_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = _artifact_directory(tmp_path / "staging")
    destination = tmp_path / "destination"
    original_rename = Path.rename
    raced = False

    def race_once(path: Path, target: Path) -> Path:
        nonlocal raced
        if path == staging and target == destination and not raced:
            raced = True
            shutil.copytree(staging, destination)
            raise OSError("simulated publication race")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", race_once)

    assert runner._publish_result_directory(staging, destination) == destination
    assert raced is True
    assert not staging.exists()


def test_publish_wraps_rename_failure_without_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = _artifact_directory(tmp_path / "staging")
    destination = tmp_path / "destination"

    def fail_rename(path: Path, target: Path) -> Path:
        raise OSError(f"cannot rename {path} to {target}")

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(runner.ResultPublicationError, match="cannot publish"):
        runner._publish_result_directory(staging, destination)


@pytest.mark.parametrize("value", [None, "", "../run", "nested/run", "nested\\run"])
def test_run_id_validation_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(runner.ResultPublicationError):
        runner._validated_run_id(value)

    assert runner._validated_run_id("run-safe") == "run-safe"


def test_module_root_rejects_missing_locations(tmp_path: Path) -> None:
    missing_location = ModuleType("missing_location")
    missing_location.__file__ = None
    with pytest.raises(ProvenanceError, match="cannot resolve"):
        provenance._module_root(missing_location, "missing_location")

    missing_directory = ModuleType("missing_directory")
    missing_directory.__file__ = str(tmp_path / "missing" / "__init__.py")
    with pytest.raises(ProvenanceError, match="does not exist"):
        provenance._module_root(missing_directory, "missing_directory")


def test_source_commit_search_skips_duplicates_and_uses_later_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULAR_STRATEGY_SOURCE_SHA", raising=False)
    application_root = tmp_path / "one" / "two"
    application_root.mkdir(parents=True)
    responses = iter(
        [
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout="A" * 40),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(provenance.subprocess, "run", fake_run)

    assert provenance.resolve_source_commit_sha(application_root) == "a" * 40
    assert len(calls) == 2

    calls.clear()
    monkeypatch.setattr(
        provenance.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=1, stdout=""),
    )
    duplicate_root = Path.cwd() / "src" / "modular_backtest"
    assert provenance.resolve_source_commit_sha(duplicate_root) is None
    assert len(calls) == 1


def test_distribution_version_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_distribution(name: str) -> str:
        raise provenance.PackageNotFoundError(name)

    monkeypatch.setattr(provenance, "version", missing_distribution)
    assert provenance._distribution_version() is None


def test_configuration_private_validators_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="data.path"):
        DataSourceConfig(
            path=cast(Path, "bars.csv"),
            format="csv",
            symbol="BTCUSDT",
        )
    with pytest.raises(ConfigurationError, match="data.columns"):
        DataSourceConfig(
            path=tmp_path / "bars.csv",
            format="csv",
            symbol="BTCUSDT",
            columns=cast(dict[str, str], []),
        )
    with pytest.raises(ConfigurationError, match="output.directory"):
        OutputConfig(cast(Path, "results"))

    data = DataSourceConfig(path=tmp_path / "bars.csv", format="csv", symbol="BTCUSDT")
    with pytest.raises(ConfigurationError, match="modules"):
        BacktestAppConfig(data=data, modules=cast(Path, "modules.yaml"))
    with pytest.raises(ConfigurationError, match="execution must be a mapping"):
        BacktestAppConfig(
            data=data,
            modules=tmp_path / "modules.yaml",
            execution=cast(dict[str, Any], []),
        )
    with pytest.raises(ConfigurationError, match="non-empty strings"):
        BacktestAppConfig(
            data=data,
            modules=tmp_path / "modules.yaml",
            requested_timeframes=("15m", ""),
        )

    with pytest.raises(ConfigurationError, match="timezone-aware"):
        _utc_datetime(__import__("datetime").datetime(2024, 1, 1), "timestamp")
    with pytest.raises(ConfigurationError, match="ISO-8601"):
        _optional_datetime(123, "timestamp")
    with pytest.raises(ConfigurationError, match="ISO-8601"):
        _optional_datetime("not-a-date", "timestamp")
    with pytest.raises(ConfigurationError, match="must be a mapping"):
        _mapping([], "mapping")
    with pytest.raises(ConfigurationError, match="non-empty path"):
        _yaml_path("", "path")
    with pytest.raises(ConfigurationError, match="finite positive"):
        _initial_capital("100")
    with pytest.raises(ConfigurationError, match="sequence of strings"):
        _timeframes("15m")
    with pytest.raises(ConfigurationError, match="non-empty strings"):
        _timeframes(["15m", ""])
