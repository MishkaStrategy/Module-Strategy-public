from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_research_runner import (
    ArtifactError,
    AtomicJsonArtifactStore,
    BacktestJob,
    ChronologicalSplitPolicy,
    DatasetLineage,
    EngineContractError,
    EnvironmentManifest,
    ExperimentManifestBuilder,
    GitProvenance,
    GridSearchPlanner,
    ModularBacktestEngineAdapter,
    ParameterConstraint,
    ParameterDimension,
    ParameterKind,
    RandomSearchPlanner,
    SearchSpace,
    SpecificationError,
    SplitMode,
    TimeRange,
    WalkForwardMode,
    WalkForwardPolicy,
    canonical_sha256,
    derive_seed,
    plan_chronological_split,
    plan_walk_forward,
    run_id,
)
from modular_research_runner.search import configuration_hashes


def test_canonical_rejects_invalid_mapping_keys() -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_sha256({1: "bad"})


def test_time_range_rejects_naive_datetime() -> None:
    with pytest.raises(SpecificationError, match="timezone-aware"):
        TimeRange(datetime(2024, 1, 1), datetime(2024, 1, 2, tzinfo=UTC))


def _dataset(overall: TimeRange, **changes) -> DatasetLineage:
    values = dict(
        dataset_id="dataset-v1",
        source="binance",
        market_type="binance-usdt-m-perpetual",
        instrument="BTCUSDT",
        timeframe="5m",
        period=overall,
        row_count=1,
        timezone="UTC",
        file_format="csv",
        file_size=1,
        sha256="a" * 64,
        preprocessing_version="v1",
        aggregation_version="v1",
        metadata_version="v1",
    )
    values.update(changes)
    return DatasetLineage(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dataset_id": "bad value"}, "portable"),
        ({"timezone": "Europe/Amsterdam"}, "UTC"),
        ({"file_format": "json"}, "csv or parquet"),
        ({"row_count": -1}, "non-negative"),
        ({"funding_data_sha256": "bad"}, "SHA-256"),
    ],
)
def test_dataset_lineage_rejects_invalid_metadata(overall_range, changes, message) -> None:
    with pytest.raises(SpecificationError, match=message):
        _dataset(overall_range, **changes)


def test_dataset_fingerprint_is_stable(dataset) -> None:
    assert dataset.fingerprint == dataset.fingerprint


@pytest.mark.parametrize(
    "dimension",
    [
        lambda: ParameterDimension("x", ParameterKind.INTEGER, ()),
        lambda: ParameterDimension("x", ParameterKind.BOOLEAN, (0,)),
        lambda: ParameterDimension("x", ParameterKind.CATEGORICAL, ("",)),
    ],
)
def test_parameter_dimension_fail_closed(dimension) -> None:
    with pytest.raises(SpecificationError):
        dimension()


def test_constraint_constructor_and_type_errors() -> None:
    with pytest.raises(SpecificationError, match="unsupported"):
        ParameterConstraint("x", "contains", right_value=1)
    with pytest.raises(SpecificationError, match="exactly one"):
        ParameterConstraint("x", "==")
    with pytest.raises(SpecificationError, match="exactly one"):
        ParameterConstraint("x", "==", right_parameter="y", right_value=1)
    with pytest.raises(SpecificationError, match="comparable"):
        ParameterConstraint("x", "<", right_value="a").accepts({"x": 1})


def test_search_space_constructor_failures() -> None:
    with pytest.raises(SpecificationError, match="at least one"):
        SearchSpace(())
    duplicate = ParameterDimension("x", ParameterKind.INTEGER, (1,))
    with pytest.raises(SpecificationError, match="unique"):
        SearchSpace((duplicate, duplicate))
    with pytest.raises(SpecificationError, match="unknown"):
        SearchSpace(
            (duplicate,),
            constraints=(ParameterConstraint("y", "==", right_value=1),),
        )
    with pytest.raises(SpecificationError, match="positive"):
        SearchSpace((duplicate,), maximum_combinations=0)


def test_grid_all_rejected_and_random_invalid_count() -> None:
    space = SearchSpace(
        (ParameterDimension("x", ParameterKind.INTEGER, (1, 2)),),
        constraints=(ParameterConstraint("x", ">", right_value=10),),
    )
    with pytest.raises(SpecificationError, match="all grid"):
        GridSearchPlanner().generate(space)
    valid = SearchSpace((ParameterDimension("x", ParameterKind.INTEGER, (1, 2)),))
    with pytest.raises(SpecificationError, match="positive"):
        RandomSearchPlanner().generate(valid, sample_count=0, seed=1)
    generated = GridSearchPlanner().generate(valid)
    assert configuration_hashes(generated) == tuple(item.sha256 for item in generated)


def test_split_policy_constructor_failures(overall_range) -> None:
    with pytest.raises(SpecificationError, match="non-negative"):
        ChronologicalSplitPolicy(
            mode=SplitMode.FIXED,
            train=overall_range,
            validation=overall_range,
            test=overall_range,
            gap_seconds=-1,
        )
    with pytest.raises(SpecificationError, match="cannot define fractions"):
        ChronologicalSplitPolicy(
            mode=SplitMode.FIXED,
            train=TimeRange(
                datetime(2020, 1, 1, tzinfo=UTC),
                datetime(2021, 1, 1, tzinfo=UTC),
            ),
            validation=TimeRange(
                datetime(2021, 1, 1, tzinfo=UTC),
                datetime(2022, 1, 1, tzinfo=UTC),
            ),
            test=TimeRange(
                datetime(2022, 1, 1, tzinfo=UTC),
                datetime(2023, 1, 1, tzinfo=UTC),
            ),
            train_fraction=0.5,
        )
    with pytest.raises(SpecificationError, match="cannot define fixed"):
        ChronologicalSplitPolicy(
            mode=SplitMode.PERCENTAGE,
            train=overall_range,
            train_fraction=0.6,
            validation_fraction=0.2,
            test_fraction=0.2,
        )


def test_split_outside_overall_and_gap_exhaustion(overall_range, split_policy) -> None:
    outside = replace(
        split_policy,
        train=TimeRange(
            datetime(2019, 1, 1, tzinfo=UTC),
            datetime(2022, 1, 1, tzinfo=UTC),
        ),
    )
    with pytest.raises(SpecificationError, match="contained"):
        plan_chronological_split(outside, overall_range)
    tiny = TimeRange(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC),
    )
    policy = ChronologicalSplitPolicy(
        mode=SplitMode.PERCENTAGE,
        train_fraction=0.6,
        validation_fraction=0.2,
        test_fraction=0.2,
        gap_seconds=2,
    )
    with pytest.raises(SpecificationError, match="consume"):
        plan_chronological_split(policy, tiny)


def test_walk_forward_policy_guards_and_minimum_history() -> None:
    with pytest.raises(SpecificationError, match="positive"):
        WalkForwardPolicy(WalkForwardMode.ROLLING, 0, 1, 1, 1)
    with pytest.raises(SpecificationError, match="non-negative"):
        WalkForwardPolicy(WalkForwardMode.ROLLING, 1, 1, 1, 1, purge_seconds=-1)
    overall = TimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 4, 1, tzinfo=UTC),
    )
    policy = WalkForwardPolicy(
        WalkForwardMode.ANCHORED,
        10 * 86400,
        5 * 86400,
        5 * 86400,
        5 * 86400,
        minimum_history_seconds=20 * 86400,
    )
    windows = plan_walk_forward(policy, overall)
    assert windows[0].train.start == overall.start
    assert windows[0].fingerprint == windows[0].fingerprint


def test_identity_input_guards() -> None:
    with pytest.raises(SpecificationError, match="non-negative"):
        run_id(
            experiment_id="exp",
            stage_id="stage",
            split_id="split",
            configuration_sha256="a" * 64,
            scenario_id="base",
            seed=1,
            repetition_index=-1,
        )
    with pytest.raises(SpecificationError, match="root seed"):
        derive_seed(True, "x")
    with pytest.raises(SpecificationError, match="labels"):
        derive_seed(1, "")


def test_manifest_and_environment_guards(specification, overall_range) -> None:
    with pytest.raises(SpecificationError, match="40 lowercase"):
        GitProvenance("branch", "bad", True)
    with pytest.raises(SpecificationError, match="untracked"):
        GitProvenance("branch", "a" * 40, True, untracked_policy="ignore")
    assert EnvironmentManifest.current().python_version
    split = plan_chronological_split(specification.split_policy, overall_range)
    with pytest.raises(SpecificationError, match="generated"):
        ExperimentManifestBuilder().build(
            experiment_id="exp-x",
            specification=specification,
            configurations=(),
            split=split,
            git=GitProvenance("branch", "a" * 40, True),
            backtest_engine_version="0.1",
            metric_schema_version="1",
            module_api_version="1",
            scheduler_configuration={},
        )


def test_artifact_root_and_reference_failures(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactError, match="root"):
        AtomicJsonArtifactStore(linked)
    store = AtomicJsonArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactError, match="safe"):
        store.write_json("", {})
    with pytest.raises(ArtifactError, match="safe"):
        store.write_json("/absolute.json", {})
    reference = store.write_json("item.json", {"ok": True})
    (store.root / "item.json").unlink()
    with pytest.raises(ArtifactError, match="does not exist"):
        store.read_json(reference)


def test_engine_default_import_and_relative_output(tmp_path: Path, monkeypatch) -> None:
    job = BacktestJob(
        "run",
        "exp",
        "stage",
        "split",
        "dataset",
        "base",
        1,
        {},
        tmp_path,
    )
    relative_output = tmp_path / "relative"
    relative_output.mkdir()
    outcome = SimpleNamespace(
        run_id="engine",
        output_directory=Path("relative"),
        bars_processed=1,
        restart_verified=False,
    )
    result = ModularBacktestEngineAdapter(lambda _: {}, lambda _: outcome).execute(job)
    assert result.output_directory == relative_output

    import builtins

    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "modular_backtest":
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(EngineContractError, match="authoritative Backtest Engine"):
        ModularBacktestEngineAdapter(lambda _: {}).execute(job)
