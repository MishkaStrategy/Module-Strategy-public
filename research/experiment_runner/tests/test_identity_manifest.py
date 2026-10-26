from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_research_runner import (
    EnvironmentManifest,
    ExperimentIdentityInputs,
    ExperimentManifestBuilder,
    GitProvenance,
    GridSearchPlanner,
    SpecificationError,
    configuration_id,
    derive_seed,
    plan_chronological_split,
    run_id,
)


def test_experiment_identity_changes_on_engine_version(specification) -> None:
    first = ExperimentIdentityInputs(specification, "0.1.0", "1", "1.0").experiment_id()
    second = ExperimentIdentityInputs(specification, "0.2.0", "1", "1.0").experiment_id()
    assert first.startswith("exp-")
    assert first != second


def test_configuration_and_run_ids_are_order_independent() -> None:
    assert configuration_id({"b": 2, "a": 1}) == configuration_id({"a": 1, "b": 2})
    first = run_id(
        experiment_id="exp-a",
        stage_id="stage-1",
        split_id="split-1",
        configuration_sha256="a" * 64,
        scenario_id="base",
        seed=11,
        repetition_index=0,
    )
    second = run_id(
        experiment_id="exp-a",
        stage_id="stage-1",
        split_id="split-1",
        configuration_sha256="a" * 64,
        scenario_id="base",
        seed=11,
        repetition_index=0,
    )
    assert first == second


def test_seed_hierarchy_is_stable_and_label_sensitive() -> None:
    assert derive_seed(42, "experiment", "stage", "run") == derive_seed(
        42, "experiment", "stage", "run"
    )
    assert derive_seed(42, "experiment", "stage", "run") != derive_seed(
        42, "experiment", "stage", "other"
    )


def test_git_provenance_requires_dirty_diff_hash() -> None:
    with pytest.raises(SpecificationError, match="requires"):
        GitProvenance(branch="research/experiment-runner", commit_sha="a" * 40, clean=False)
    with pytest.raises(SpecificationError, match="cannot"):
        GitProvenance(
            branch="research/experiment-runner",
            commit_sha="a" * 40,
            clean=True,
            diff_sha256="b" * 64,
        )


def test_manifest_is_complete_deterministic_except_timestamp(
    specification,
    overall_range,
) -> None:
    configurations = GridSearchPlanner().generate(specification.search_space)
    split = plan_chronological_split(specification.split_policy, overall_range)
    identity = ExperimentIdentityInputs(specification, "0.1.0", "1", "1.0")
    created_at = datetime(2026, 7, 23, tzinfo=UTC)
    kwargs = dict(
        experiment_id=identity.experiment_id(),
        specification=specification,
        configurations=configurations,
        split=split,
        git=GitProvenance(
            branch="research/experiment-runner",
            commit_sha="5e03b933207a99940244634f49a756d27888a5c3",  # pragma: allowlist secret
            clean=True,
        ),
        backtest_engine_version="0.1.0",
        metric_schema_version="1",
        module_api_version="1.0",
        scheduler_configuration={"workers": 4},
        environment=EnvironmentManifest("3.13.5", "test", None),
        created_at=created_at,
    )
    first = ExperimentManifestBuilder().build(**kwargs)
    second = ExperimentManifestBuilder().build(**kwargs)
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.dataset_hashes == {"binance-btcusdt-5m-v1": "a" * 64}
    assert len(first.configuration_hashes) == 4
