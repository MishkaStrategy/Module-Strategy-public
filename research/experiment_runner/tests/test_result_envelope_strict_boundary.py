from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_research_runner import (
    ArtifactReference,
    BacktestRunReference,
    SpecificationError,
)
from modular_research_runner.results import LocalResultSink, RunResultEnvelope

NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _envelope_mapping(tmp_path: Path) -> dict[str, object]:
    return {
        "experiment_id": "exp-strict",
        "run_id": "run-strict",
        "configuration_sha256": "a" * 64,
        "stage_id": "stage-train",
        "stage_role": "train",
        "split_id": "split-train",
        "dataset_id": "dataset-1",
        "dataset_sha256": "b" * 64,
        "scenario_id": "base",
        "seed": 7,
        "engine": BacktestRunReference(
            requested_run_id="run-strict",
            engine_run_id="engine-strict",
            output_directory=tmp_path / "engine-output",
            bars_processed=100,
            restart_verified=True,
        ),
        "metrics_schema_version": "metrics-v1",
        "metrics_artifact": None,
        "metadata": {"source": "authoritative-engine"},
        "created_at": NOW,
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    "field",
    [
        "experiment_id",
        "run_id",
        "configuration_sha256",
        "stage_id",
        "stage_role",
        "split_id",
        "dataset_id",
        "dataset_sha256",
        "scenario_id",
        "metrics_schema_version",
    ],
)
def test_required_envelope_strings_reject_none(
    tmp_path: Path,
    field: str,
) -> None:
    payload = _envelope_mapping(tmp_path)
    payload[field] = None

    with pytest.raises(SpecificationError, match=rf"{field} must be non-empty"):
        RunResultEnvelope(**payload)


@pytest.mark.parametrize("seed", [True, -1, "7"])
def test_result_envelope_rejects_coerced_seed(tmp_path: Path, seed: object) -> None:
    payload = _envelope_mapping(tmp_path)
    payload["seed"] = seed

    with pytest.raises(SpecificationError, match="seed must be a non-negative integer"):
        RunResultEnvelope(**payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("engine", None, "engine must be a BacktestRunReference"),
        ("metrics_artifact", {}, "metrics_artifact must be an ArtifactReference"),
        ("metadata", [], "metadata must be a mapping"),
        ("created_at", "2026-07-24T00:00:00Z", "created_at must be a datetime"),
        ("schema_version", True, "unsupported result envelope schema"),
        ("schema_version", "1", "unsupported result envelope schema"),
    ],
)
def test_result_envelope_rejects_invalid_boundary_types(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _envelope_mapping(tmp_path)
    payload[field] = value

    with pytest.raises(SpecificationError, match=message):
        RunResultEnvelope(**payload)


def test_result_envelope_accepts_typed_metrics_reference(tmp_path: Path) -> None:
    payload = _envelope_mapping(tmp_path)
    payload["metrics_artifact"] = ArtifactReference(
        relative_path="metrics/run-strict.json",
        sha256="c" * 64,
        size_bytes=123,
    )

    envelope = RunResultEnvelope(**payload)

    assert envelope.metrics_artifact is not None
    assert envelope.seed == 7


def test_result_sink_rejects_non_envelope(tmp_path: Path) -> None:
    sink = LocalResultSink(tmp_path / "results")

    with pytest.raises(SpecificationError, match="requires a RunResultEnvelope"):
        sink.publish({})
