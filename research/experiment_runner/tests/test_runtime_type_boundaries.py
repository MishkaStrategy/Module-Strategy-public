from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_research_runner import (
    BacktestJob,
    BacktestRunReference,
    EngineContractError,
    ModularBacktestEngineAdapter,
    SpecificationError,
)
from modular_research_runner.repository import (
    StateRepositoryError,
    _failure,
    _job,
    _json,
    _result,
)

NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _backtest_job(tmp_path: Path) -> BacktestJob:
    return BacktestJob(
        run_id="run-strict",
        experiment_id="exp-strict",
        stage_id="stage-train",
        split_id="split-train",
        dataset_id="dataset-1",
        scenario_id="base",
        seed=7,
        parameters={},
        output_directory=tmp_path / "output",
        stage_role="train",
        metadata={},
    )


def _stored_job_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "run-strict",
        "experiment_id": "exp-strict",
        "stage_id": "stage-train",
        "split_id": "split-train",
        "dataset_id": "dataset-1",
        "scenario_id": "base",
        "seed": 7,
        "parameters": {},
        "output_directory": tmp_path / "output",
        "stage_role": "train",
        "period": None,
        "repetition_index": 0,
        "metadata": {},
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", None),
        ("seed", True),
        ("output_directory", "not-a-tagged-path"),
        ("repetition_index", True),
        ("parameters", []),
        ("metadata", []),
    ],
)
def test_stored_jobs_reject_coercible_corruption(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _stored_job_payload(tmp_path)
    payload[field] = value

    with pytest.raises(StateRepositoryError, match="stored job payload is invalid"):
        _job(_json(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", None),
        ("message", None),
        ("occurred_at", "2026-07-24T00:00:00Z"),
        ("retryable", "false"),
        ("exception_type", 123),
    ],
)
def test_stored_failures_reject_coercible_corruption(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "category": "TRANSIENT",
        "message": "temporary failure",
        "occurred_at": NOW,
        "retryable": False,
        "exception_type": "TimeoutError",
    }
    payload[field] = value

    with pytest.raises(StateRepositoryError, match="stored failure payload is invalid"):
        _failure(_json(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_run_id", None),
        ("engine_run_id", None),
        ("output_directory", "not-a-tagged-path"),
        ("bars_processed", True),
        ("restart_verified", "false"),
    ],
)
def test_stored_results_reject_coercible_corruption(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "requested_run_id": "run-strict",
        "engine_run_id": "engine-strict",
        "output_directory": tmp_path / "engine-output",
        "bars_processed": 100,
        "restart_verified": False,
    }
    payload[field] = value

    with pytest.raises(StateRepositoryError, match="stored result payload is invalid"):
        _result(_json(payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", None, "run_id must be non-empty"),
        ("bars_processed", True, "bars_processed must be a non-negative integer"),
        ("bars_processed", -1, "bars_processed must be a non-negative integer"),
        ("restart_verified", "false", "restart_verified must be a boolean"),
    ],
)
def test_engine_adapter_rejects_coercible_outcomes(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload: dict[str, object] = {
        "run_id": "engine-strict",
        "output_directory": tmp_path / "engine-output",
        "bars_processed": 100,
        "restart_verified": True,
    }
    payload[field] = value
    adapter = ModularBacktestEngineAdapter(
        lambda job: job,
        lambda config: SimpleNamespace(**payload),
    )

    with pytest.raises(EngineContractError, match=message):
        adapter.execute(_backtest_job(tmp_path))


def test_runtime_models_reject_bool_as_integer_and_string_as_boolean(tmp_path: Path) -> None:
    with pytest.raises(SpecificationError, match="repetition_index"):
        BacktestJob(
            run_id="run-strict",
            experiment_id="exp-strict",
            stage_id="stage-train",
            split_id="split-train",
            dataset_id="dataset-1",
            scenario_id="base",
            seed=7,
            parameters={},
            output_directory=tmp_path,
            repetition_index=True,
        )

    with pytest.raises(SpecificationError, match="bars_processed"):
        BacktestRunReference(
            requested_run_id="run-strict",
            engine_run_id="engine-strict",
            output_directory=tmp_path,
            bars_processed=True,
            restart_verified=True,
        )

    with pytest.raises(SpecificationError, match="restart_verified"):
        BacktestRunReference(
            requested_run_id="run-strict",
            engine_run_id="engine-strict",
            output_directory=tmp_path,
            bars_processed=1,
            restart_verified="false",
        )


def test_valid_durable_payloads_still_round_trip(tmp_path: Path) -> None:
    job = _job(_json(_stored_job_payload(tmp_path)))
    assert job.seed == 7
    assert job.output_directory == tmp_path / "output"

    failure = _failure(
        _json(
            {
                "category": "TRANSIENT",
                "message": "temporary failure",
                "occurred_at": NOW,
                "retryable": False,
                "exception_type": "TimeoutError",
            }
        )
    )
    assert failure is not None
    assert failure.retryable is False

    result = _result(
        _json(
            {
                "requested_run_id": "run-strict",
                "engine_run_id": "engine-strict",
                "output_directory": tmp_path / "engine-output",
                "bars_processed": 100,
                "restart_verified": False,
            }
        )
    )
    assert result is not None
    assert result.restart_verified is False
