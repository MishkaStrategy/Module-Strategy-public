from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import modular_research_runner.artifacts as artifacts_module
from modular_research_runner.access import FinalTestGrant
from modular_research_runner.artifacts import (
    ArtifactError,
    ArtifactReference,
    AtomicJsonArtifactStore,
)
from modular_research_runner.engine import (
    BacktestJob,
    BacktestRunReference,
    EngineContractError,
    ModularBacktestEngineAdapter,
)
from modular_research_runner.manifest import (
    EnvironmentManifest,
    ExperimentManifest,
    GitProvenance,
)
from modular_research_runner.models import ExperimentStatus, SpecificationError
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.results import RunResultEnvelope
from modular_research_runner.state import (
    FailureCategory,
    FailureRecord,
    RunState,
    RunStatus,
    StateEvent,
)

NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _grant(**changes: Any) -> FinalTestGrant:
    values: dict[str, Any] = {
        "grant_id": "grant-1",
        "experiment_id": "exp-1",
        "specification_sha256": "a" * 64,
        "split_fingerprint": "b" * 64,
        "candidate_configuration_hashes": ("c" * 64,),
        "issued_at": NOW,
    }
    values.update(changes)
    return FinalTestGrant(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"candidate_configuration_hashes": ()}, "frozen candidates"),
        ({"issued_at": datetime(2026, 7, 24)}, "issued_at"),
        ({"consumed_at": datetime(2026, 7, 24)}, "consumed_at"),
        ({"schema_version": 2}, "unsupported final test grant schema"),
    ],
)
def test_final_test_grant_rejects_invalid_direct_values(
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        _grant(**changes)


def _engine_reference(run_id: str = "run-1") -> BacktestRunReference:
    return BacktestRunReference(
        requested_run_id=run_id,
        engine_run_id="engine-1",
        output_directory=Path("engine-output"),
        bars_processed=10,
        restart_verified=True,
    )


def _envelope(**changes: Any) -> RunResultEnvelope:
    values: dict[str, Any] = {
        "experiment_id": "exp-1",
        "run_id": "run-1",
        "configuration_sha256": "a" * 64,
        "stage_id": "stage-1",
        "stage_role": "validation",
        "split_id": "split-1",
        "dataset_id": "dataset-1",
        "dataset_sha256": "b" * 64,
        "scenario_id": "base",
        "seed": 7,
        "engine": _engine_reference(),
        "metrics_schema_version": "1",
        "metrics_artifact": None,
        "metadata": {"source": ["external"]},
        "created_at": NOW,
    }
    values.update(changes)
    return RunResultEnvelope(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stage_role": " "}, "stage_role must be non-empty"),
        (
            {"engine": _engine_reference("different-run")},
            "engine reference does not belong",
        ),
        ({"created_at": datetime(2026, 7, 24)}, "timezone-aware"),
        ({"schema_version": 3}, "unsupported result envelope schema"),
    ],
)
def test_result_envelope_rejects_invalid_lineage_and_schema(
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        _envelope(**changes)


def _job(tmp_path: Path, **changes: Any) -> BacktestJob:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "experiment_id": "exp-1",
        "stage_id": "stage-1",
        "split_id": "split-1",
        "dataset_id": "dataset-1",
        "scenario_id": "base",
        "seed": 1,
        "parameters": {"ttl": 1},
        "output_directory": tmp_path,
        "stage_role": "train",
    }
    values.update(changes)
    return BacktestJob(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stage_role": " "}, "stage_role must be non-empty"),
        ({"seed": True}, "seed must be an integer"),
        ({"repetition_index": -1}, "repetition_index must be a non-negative integer"),
        ({"output_directory": "not-a-path"}, "output_directory must be a Path"),
        ({"period": "not-a-range"}, "period must be a TimeRange"),
    ],
)
def test_backtest_job_rejects_invalid_direct_values(
    tmp_path: Path,
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        _job(tmp_path, **changes)


@pytest.mark.parametrize(
    "missing_name",
    ["run_id", "output_directory", "bars_processed", "restart_verified"],
)
def test_engine_adapter_reports_each_missing_outcome_field(
    tmp_path: Path,
    missing_name: str,
) -> None:
    values: dict[str, Any] = {
        "run_id": "engine-1",
        "output_directory": Path("relative-output"),
        "bars_processed": 1,
        "restart_verified": True,
    }
    del values[missing_name]
    adapter = ModularBacktestEngineAdapter(
        lambda job: job,
        lambda config: SimpleNamespace(**values),
    )
    with pytest.raises(EngineContractError, match=missing_name):
        adapter.execute(_job(tmp_path))


def test_engine_adapter_resolves_relative_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "relative-output"
    output.mkdir()
    adapter = ModularBacktestEngineAdapter(
        lambda job: job,
        lambda config: SimpleNamespace(
            run_id="engine-1",
            output_directory=Path("relative-output"),
            bars_processed=1,
            restart_verified=True,
        ),
    )
    result = adapter.execute(_job(tmp_path))
    assert result.output_directory == output


def _manifest(**changes: Any) -> ExperimentManifest:
    values: dict[str, Any] = {
        "experiment_id": "exp-1",
        "research_id": "research-1",
        "parent_experiment_id": None,
        "specification_sha256": "a" * 64,
        "search_space_sha256": "b" * 64,
        "configuration_hashes": ("c" * 64,),
        "dataset_hashes": {"dataset-1": "d" * 64},
        "split": object(),
        "strategy_id": "strategy",
        "strategy_version": "1",
        "module_versions": {"module": "1"},
        "module_api_version": "1",
        "backtest_engine_version": "1",
        "metric_schema_version": "1",
        "git": GitProvenance("branch", "e" * 40, True),
        "environment": EnvironmentManifest("3.12", "test", None),
        "scheduler_configuration": {},
        "resource_limits": {},
        "seeds": (1,),
        "timezone": "UTC",
        "created_at": NOW,
        "status": ExperimentStatus.PLANNED,
    }
    values.update(changes)
    return ExperimentManifest(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"timezone": "Europe/Amsterdam"}, "timezone must be UTC"),
        ({"created_at": datetime(2026, 7, 24)}, "timezone-aware"),
        ({"schema_version": 2}, "unsupported experiment manifest schema"),
    ],
)
def test_experiment_manifest_rejects_invalid_runtime_metadata(
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        _manifest(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"commit_sha": "A" * 40}, "lowercase hexadecimal"),
        ({"untracked_policy": "ignore"}, "untracked policy"),
    ],
)
def test_git_provenance_rejects_invalid_identity_policy(
    changes: dict[str, Any],
    message: str,
) -> None:
    values: dict[str, Any] = {
        "branch": "branch",
        "commit_sha": "a" * 40,
        "clean": True,
    }
    values.update(changes)
    with pytest.raises(SpecificationError, match=message):
        GitProvenance(**values)


def test_artifact_store_rejects_root_and_target_symlinks(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual-root"
    actual_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(ArtifactError, match="root must not be a symlink"):
        AtomicJsonArtifactStore(linked_root)

    store = AtomicJsonArtifactStore(tmp_path / "store")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (store.root / "target.json").symlink_to(outside)
    with pytest.raises(ArtifactError, match="target must not be a symlink"):
        store.write_json("target.json", {})


def test_artifact_store_rejects_missing_reference(tmp_path: Path) -> None:
    store = AtomicJsonArtifactStore(tmp_path / "store")
    reference = ArtifactReference("missing.json", "0" * 64, 0)
    with pytest.raises(ArtifactError, match="does not exist"):
        store.read_json(reference)


def test_artifact_store_cleans_temporary_file_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AtomicJsonArtifactStore(tmp_path / "store")

    def fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(artifacts_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.write_json("report.json", {"value": 1})
    assert not list(store.root.glob(".report.json.*"))
    assert not (store.root / "report.json").exists()


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({"max_attempts": True}, "max_attempts must be an integer"),
        ({"initial_backoff_seconds": True}, "must be numeric"),
        ({"maximum_backoff_seconds": -1}, "must be >= 0"),
    ],
)
def test_retry_policy_rejects_boolean_and_negative_mapping_values(
    mapping: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        RetryPolicy.from_mapping(mapping)


@pytest.mark.parametrize("name", ["lease_seconds", "heartbeat_seconds"])
def test_resume_policy_rejects_boolean_integer_fields(name: str) -> None:
    with pytest.raises(SpecificationError, match=f"{name} must be an integer"):
        ResumePolicy.from_mapping({name: True})


def test_run_status_terminal_property_covers_terminal_and_active_states() -> None:
    assert RunStatus.SUCCEEDED.terminal
    assert RunStatus.CORRUPTED.terminal
    assert not RunStatus.PLANNED.terminal
    assert not RunStatus.RUNNING.terminal


def test_failure_record_rejects_blank_exception_type() -> None:
    with pytest.raises(SpecificationError, match="exception_type"):
        FailureRecord(FailureCategory.UNKNOWN, "failure", NOW, False, " ")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"attempt_count": -1}, "attempt_count must be non-negative"),
        ({"attempt_count": 2, "max_attempts": 1}, "attempt counters"),
        ({"revision": -1}, "revision must be non-negative"),
        ({"created_at": datetime(2026, 7, 24)}, "created_at must be timezone-aware"),
    ],
)
def test_run_state_rejects_invalid_counters_and_timestamps(
    changes: dict[str, Any],
    message: str,
) -> None:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "experiment_id": "exp-1",
        "status": RunStatus.PLANNED,
        "attempt_count": 0,
        "max_attempts": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "available_at": NOW,
        "revision": 0,
    }
    values.update(changes)
    with pytest.raises(SpecificationError, match=message):
        RunState(**values)


def test_run_state_rejects_naive_lease_expiry() -> None:
    with pytest.raises(SpecificationError, match="lease_expires_at"):
        RunState(
            run_id="run-1",
            experiment_id="exp-1",
            status=RunStatus.RUNNING,
            attempt_count=1,
            max_attempts=1,
            created_at=NOW,
            updated_at=NOW,
            available_at=NOW,
            revision=0,
            lease_owner="worker",
            lease_expires_at=datetime(2026, 7, 24),
        )


def test_state_event_rejects_naive_timestamp() -> None:
    with pytest.raises(SpecificationError, match="event time"):
        StateEvent(
            event_id=1,
            run_id="run-1",
            from_status=None,
            to_status=RunStatus.PLANNED,
            occurred_at=datetime(2026, 7, 24),
            revision=0,
        )
