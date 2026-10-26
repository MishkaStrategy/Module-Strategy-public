from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from modular_results_registry.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_sha256,
    deterministic_id,
)
from modular_results_registry.errors import ValidationError
from modular_results_registry.models import (
    ArtifactRecord,
    ArtifactStatus,
    ConfigurationRecord,
    DatasetRecord,
    ExperimentRecord,
    ExperimentStatus,
    MetricSnapshot,
)
from conftest import NOW


def test_canonical_mapping_order_and_decimal_are_stable() -> None:
    assert canonical_json_bytes({"b": 2, "a": Decimal("1.00")}) == canonical_json_bytes(
        {"a": Decimal("1"), "b": 2}
    )
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_canonical_rejects_nonfinite_naive_and_nonstring_keys() -> None:
    for value in (float("nan"), float("inf"), Decimal("NaN")):
        with pytest.raises(CanonicalizationError):
            canonical_json_bytes(value)
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(datetime(2026, 1, 1))
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({1: "bad"})


def test_deterministic_id_validates_prefix_and_length() -> None:
    assert deterministic_id("run", {"x": 1}) == deterministic_id("run", {"x": 1})
    with pytest.raises(CanonicalizationError):
        deterministic_id("bad prefix", {"x": 1})
    with pytest.raises(CanonicalizationError):
        deterministic_id("run", {"x": 1}, digest_chars=8)


def test_artifact_rejects_unsafe_path_and_unverified_status() -> None:
    common = dict(
        artifact_id="artifact-001",
        package_id="package-001",
        media_type="application/json",
        size_bytes=1,
        sha256="a" * 64,
        producer_module_id="core",
        producer_module_version="1",
        schema_id="journal",
        schema_version="1",
    )
    for path in ("../x", "/absolute", "a//b", "./x", "C:/x"):
        with pytest.raises(ValidationError):
            ArtifactRecord(relative_path=path, **common)
    with pytest.raises(ValidationError):
        ArtifactRecord(relative_path="x.json", status=ArtifactStatus.RECEIVED, **common)


def test_configuration_create_is_order_independent_and_deeply_immutable() -> None:
    left = ConfigurationRecord.create(
        strategy_id="strategy-001",
        schema_version="1",
        canonical_configuration={"b": {"y": [2, 3]}, "a": 1},
        stage="search",
    )
    right = ConfigurationRecord.create(
        strategy_id="strategy-001",
        schema_version="1",
        canonical_configuration={"a": 1, "b": {"y": [2, 3]}},
        stage="search",
    )
    assert left.configuration_id == right.configuration_id
    assert left.configuration_hash == right.configuration_hash
    with pytest.raises(TypeError):
        left.canonical_configuration["a"] = 3  # type: ignore[index]
    nested = left.canonical_configuration["b"]
    with pytest.raises(TypeError):
        nested["y"] = ()  # type: ignore[index]


def test_dataset_and_experiment_enforce_reproducibility() -> None:
    with pytest.raises(ValidationError):
        DatasetRecord(
            dataset_id="dataset-001",
            source="local",
            market="BINANCE",
            market_type="USDTM_FUTURES",
            instrument="BTCUSDT",
            timeframe="5m",
            min_timestamp=datetime(2026, 1, 1),
            max_timestamp=NOW,
            row_count=1,
            sha256="a" * 64,
            preprocessing_version="1",
            aggregation_version="1",
            metadata_hash="b" * 64,
        )
    with pytest.raises(ValidationError):
        ExperimentRecord(
            experiment_id="experiment-001",
            research_id="research-001",
            title="Dirty",
            description="",
            strategy_id="strategy-001",
            strategy_version="1",
            commit_sha="5e03b933",
            branch="work",
            working_tree_status="DIRTY",
            diff_hash="a" * 64,
            patch_artifact_id=None,
            module_versions={},
            dependency_lock_hash="b" * 64,
            started_at=NOW,
            status=ExperimentStatus.RUNNING,
            search_method="grid",
            split_policy="walk-forward",
        )


def test_metric_rejects_nonfinite_and_key_captures_context() -> None:
    with pytest.raises(ValidationError):
        MetricSnapshot(
            "run-001",
            "profit_factor",
            float("nan"),
            "ratio",
            6,
            "v1",
            "producer",
            "test",
            "all",
            "base",
        )
    left = MetricSnapshot(
        "run-001", "profit_factor", 1.2, "ratio", 6, "v1", "producer", "test", "all", "base"
    )
    right = MetricSnapshot(
        "run-001", "profit_factor", 1.2, "ratio", 6, "v2", "producer", "test", "all", "base"
    )
    assert left.metric_key != right.metric_key
