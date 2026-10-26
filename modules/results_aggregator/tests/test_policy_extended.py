from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modular_results_registry.canonical import canonical_sha256
from modular_results_registry.errors import ResourceLimitError, ValidationError
from modular_results_registry.intake import ArtifactInventoryItem, ArtifactPackageManifest
from modular_results_registry.policy import (
    ResourceGuard,
    ResourcePolicy,
    ResourceSnapshot,
    RetentionCandidate,
    RetentionCategory,
    build_retention_plan,
    disk_snapshot,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _manifest(*sizes: int) -> ArtifactPackageManifest:
    inventory = tuple(
        ArtifactInventoryItem(
            relative_path=f"artifact-{index}.json",
            sha256=f"{index + 1:064x}"[-64:],
            size_bytes=size,
            media_type="application/json",
            schema_id="test.artifact",
            schema_version="1",
        )
        for index, size in enumerate(sizes)
    )
    values = dict(
        schema_id="artifact-package",
        schema_version="1",
        package_id="package-001",
        package_version="1",
        producer_module_id="producer",
        producer_module_version="1",
        producer_schema_version="1",
        experiment_id="experiment-001",
        research_id="research-001",
        stage_id="stage-001",
        run_id="run-001",
        parent_experiment_id=None,
        strategy_id="strategy-001",
        strategy_version="1",
        module_api_version="1",
        backtest_engine_version="0.4.0",
        metric_schema_version="1",
        configuration_hash="a" * 64,
        dataset_ids=("dataset-001",),
        dataset_hashes=("b" * 64,),
        commit_sha="c" * 40,
        branch="research/results-aggregator",
        working_tree_status="CLEAN",
        diff_hash=None,
        patch_artifact_path=None,
        dependency_lock_hash="d" * 64,
        environment_manifest={},
        split_information={},
        seed=1,
        scenario_id="scenario-001",
        created_at=NOW,
        artifact_inventory=inventory,
        expected_references={},
        audit_evidence=(),
        completion_status="COMPLETED",
        integrity_checksum="0" * 64,
    )
    temp = ArtifactPackageManifest(**values)
    return replace(temp, integrity_checksum=canonical_sha256(temp.checksum_payload()))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_package_size_bytes": 0},
        {"max_artifact_count": True},
        {"max_trades_per_import": -1},
        {"query_timeout_seconds": 0},
        {"report_timeout_seconds": float("inf")},
    ],
)
def test_resource_policy_validation(kwargs) -> None:
    with pytest.raises(ValidationError):
        ResourcePolicy(**kwargs)


@pytest.mark.parametrize(
    "field", ["registry_bytes", "artifact_store_bytes", "free_disk_bytes", "concurrent_imports"]
)
def test_resource_snapshot_validation(field) -> None:
    values = dict(registry_bytes=1, artifact_store_bytes=2, free_disk_bytes=3, concurrent_imports=0)
    values[field] = -1
    with pytest.raises(ValidationError):
        ResourceSnapshot(**values)
    values[field] = True
    with pytest.raises(ValidationError):
        ResourceSnapshot(**values)


def test_disk_snapshot_counts_regular_files_and_missing_registry(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    (artifacts / "nested").mkdir(parents=True)
    (artifacts / "one").write_bytes(b"123")
    (artifacts / "nested" / "two").write_bytes(b"4567")
    snapshot = disk_snapshot(
        registry_path=tmp_path / "missing.sqlite",
        artifact_store_path=artifacts,
        concurrent_imports=2,
    )
    assert snapshot.registry_bytes == 0
    assert snapshot.artifact_store_bytes == 7
    assert snapshot.concurrent_imports == 2
    assert snapshot.free_disk_bytes > 0
    registry = tmp_path / "registry.sqlite"
    registry.write_bytes(b"abcde")
    assert disk_snapshot(registry_path=registry, artifact_store_path=artifacts).registry_bytes == 5


def test_disk_snapshot_rejects_nonregular_and_symlink_paths(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    with pytest.raises(ValidationError, match="regular file"):
        disk_snapshot(registry_path=registry_dir, artifact_store_path=artifacts)
    bad_store = tmp_path / "store-file"
    bad_store.write_text("x")
    with pytest.raises(ValidationError, match="real directory"):
        disk_snapshot(registry_path=tmp_path / "missing", artifact_store_path=bad_store)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValidationError, match="symlink"):
        disk_snapshot(registry_path=tmp_path / "missing", artifact_store_path=link)
    nested_link = artifacts / "nested-link"
    nested_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValidationError, match="symlink directories"):
        disk_snapshot(registry_path=tmp_path / "missing", artifact_store_path=artifacts)
    nested_link.unlink()
    file_target = tmp_path / "file-target"
    file_target.write_text("x")
    (artifacts / "file-link").symlink_to(file_target)
    with pytest.raises(ValidationError, match="symlink files"):
        disk_snapshot(registry_path=tmp_path / "missing", artifact_store_path=artifacts)


def test_resource_guard_accepts_boundary_values() -> None:
    policy = ResourcePolicy(
        max_package_size_bytes=10,
        max_artifact_count=2,
        max_trades_per_import=3,
        max_concurrent_imports=2,
        max_disk_use_bytes=20,
        min_free_disk_bytes=5,
        max_export_size_bytes=10,
        max_pareto_candidates_without_chunking=2,
        max_chart_points=3,
    )
    guard = ResourceGuard(policy)
    guard.check_import(
        _manifest(4, 6),
        ResourceSnapshot(5, 5, 15, 1),
        trade_count=3,
    )
    guard.check_export(10)
    guard.check_pareto(2, chunked=False)
    guard.check_pareto(3, chunked=True)
    guard.check_chart(3)


@pytest.mark.parametrize(
    ("policy", "manifest", "snapshot", "trade_count", "message"),
    [
        (
            ResourcePolicy(max_artifact_count=1),
            _manifest(1, 1),
            ResourceSnapshot(0, 0, 20_000_000_000),
            None,
            "artifact count",
        ),
        (
            ResourcePolicy(max_package_size_bytes=1),
            _manifest(2),
            ResourceSnapshot(0, 0, 20_000_000_000),
            None,
            "package size",
        ),
        (
            ResourcePolicy(max_trades_per_import=1),
            _manifest(1),
            ResourceSnapshot(0, 0, 20_000_000_000),
            2,
            "trade count",
        ),
        (
            ResourcePolicy(max_concurrent_imports=1),
            _manifest(1),
            ResourceSnapshot(0, 0, 20_000_000_000, 1),
            None,
            "concurrent",
        ),
        (
            ResourcePolicy(max_disk_use_bytes=2, min_free_disk_bytes=1),
            _manifest(1),
            ResourceSnapshot(1, 1, 20),
            None,
            "projected disk",
        ),
        (
            ResourcePolicy(min_free_disk_bytes=10),
            _manifest(1),
            ResourceSnapshot(0, 0, 10),
            None,
            "insufficient free",
        ),
    ],
)
def test_resource_guard_import_limits(policy, manifest, snapshot, trade_count, message) -> None:
    with pytest.raises(ResourceLimitError, match=message):
        ResourceGuard(policy).check_import(manifest, snapshot, trade_count=trade_count)


def test_resource_guard_other_limits_and_validation() -> None:
    guard = ResourceGuard(
        ResourcePolicy(
            max_export_size_bytes=2,
            max_pareto_candidates_without_chunking=2,
            max_chart_points=2,
        )
    )
    with pytest.raises(ResourceLimitError, match="export"):
        guard.check_export(3)
    with pytest.raises(ResourceLimitError, match="chunking"):
        guard.check_pareto(3, chunked=False)
    with pytest.raises(ResourceLimitError, match="chart"):
        guard.check_chart(3)
    for call in (
        lambda: guard.check_export(-1),
        lambda: guard.check_pareto(-1, chunked=False),
        lambda: guard.check_chart(True),
        lambda: guard.check_import(
            _manifest(1), ResourceSnapshot(0, 0, 20_000_000_000), trade_count=-1
        ),
    ):
        with pytest.raises(ValidationError):
            call()


def _candidate(
    identifier: str, category: RetentionCategory, age_days: int, referenced: bool = False
):
    return RetentionCandidate(identifier, category, NOW - timedelta(days=age_days), referenced)


def test_retention_plan_is_deterministic_and_fail_closed() -> None:
    plan = build_retention_plan(
        [
            _candidate("z-report", RetentionCategory.GENERATED_REPORT, 31),
            _candidate("a-cache", RetentionCategory.DERIVED_CACHE, 31),
            _candidate("new-export", RetentionCategory.TEMPORARY_EXPORT, 30),
            _candidate("referenced", RetentionCategory.DERIVED_CACHE, 31, True),
            _candidate("artifact", RetentionCategory.IMMUTABLE_ARTIFACT, 1000),
            _candidate("audit", RetentionCategory.AUDIT_LOG, 1000),
        ],
        retention_days=30,
        now=NOW,
    )
    assert plan.eligible_ids == ("a-cache", "z-report")
    assert plan.protected_ids == ("artifact", "audit", "new-export", "referenced")
    assert plan.cutoff_utc == NOW - timedelta(days=30)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RetentionCandidate("", RetentionCategory.DERIVED_CACHE, NOW, False),
        lambda: RetentionCandidate(" x ", RetentionCategory.DERIVED_CACHE, NOW, False),
        lambda: RetentionCandidate("x", "DERIVED_CACHE", NOW, False),
        lambda: RetentionCandidate(
            "x", RetentionCategory.DERIVED_CACHE, NOW.replace(tzinfo=None), False
        ),
        lambda: RetentionCandidate("x", RetentionCategory.DERIVED_CACHE, NOW, 0),
    ],
)
def test_retention_candidate_validation(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_retention_plan_validation() -> None:
    item = _candidate("x", RetentionCategory.DERIVED_CACHE, 31)
    with pytest.raises(ValidationError):
        build_retention_plan([item], retention_days=0, now=NOW)
    with pytest.raises(ValidationError, match="timezone"):
        build_retention_plan([item], retention_days=30, now=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="unique"):
        build_retention_plan([item, item], retention_days=30, now=NOW)
