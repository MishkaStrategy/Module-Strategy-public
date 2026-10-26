from __future__ import annotations

import hashlib
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

from auth_helpers import LOCAL_ADMIN_HEADERS, local_admin_authenticator
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApplicationSettings,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.contracts import DatasetDescriptor
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from scripts import benchmark_control_plane_slo
from scripts.benchmark_control_plane_contention import run_contention_probe
from scripts.benchmark_control_plane_slo import run_benchmark


def _descriptor(root: Path) -> DatasetDescriptor:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    dataset_id = "public-contract-dataset"
    return DatasetDescriptor(
        dataset_id=dataset_id,
        version=1,
        dataset_hash=hashlib.sha256(dataset_id.encode()).hexdigest(),
        manifest_uri="manifests/public-contract-dataset/v1.json",
        storage_uri=root.as_uri(),
        exchange_id="benchmark",
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument_id="benchmark:usdt-m-perpetual:btcusdt",
        data_types=(DataType.KLINES,),
        timeframes=("5m",),
        start=start,
        end=start + timedelta(minutes=5),
        schema_version=1,
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        lineage=("synthetic:public-contract",),
    )


def test_control_plane_slo_benchmark_smoke(tmp_path: Path) -> None:
    result = run_benchmark(
        root=tmp_path / "benchmark",
        dataset_counts=(1, 10),
        job_counts=(1, 10),
        samples=3,
    )

    assert result["environment"]["no_network"] is True
    expected_semantics = {
        "resource_warning_zero": True,
        "fd_stable": True,
        "thread_stable": True,
        "concurrent_reader_availability": 1.0,
        "sse_ordering_violation_count": 0,
        "cursor_violation_count": 0,
        "orphan_job_count": 0,
        "path_leakage_count": 0,
        "credential_leakage_count": 0,
    }
    assert {
        key: result["semantic_verdict"][key] for key in expected_semantics
    } == expected_semantics
    assert result["jobs"]["duplicate_submission"]["deterministic_identity"] is True
    assert result["jobs"]["progress_updates"]["monotonic"] is True
    assert result["restart_recovery"]["terminal_state"] == "COMPLETED"
    assert result["resources"]["resource_warning_count"] == 0
    assert result["resources"]["temporary_bytes"] == 0


def test_control_plane_slo_benchmark_counts_shutdown_resource_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_factory = benchmark_control_plane_slo._application

    def application_with_shutdown_warning(
        root: Path,
        *,
        instance_id: str,
    ) -> MarketDataApplication:
        application = original_factory(root, instance_id=instance_id)
        if instance_id == "wave2e-control-plane-benchmark":
            shutdown = application.shutdown

            def shutdown_with_warning() -> None:
                shutdown()
                warnings.warn("synthetic shutdown warning", ResourceWarning, stacklevel=2)

            monkeypatch.setattr(application, "shutdown", shutdown_with_warning)
        return application

    monkeypatch.setattr(
        benchmark_control_plane_slo,
        "_application",
        application_with_shutdown_warning,
    )
    result = run_benchmark(
        root=tmp_path / "shutdown-warning",
        dataset_counts=(1,),
        job_counts=(1,),
        samples=3,
    )

    assert result["resources"]["shutdown_ms"] >= 0
    assert result["resources"]["resource_warning_count"] >= 1
    assert result["semantic_verdict"]["resource_warning_zero"] is False


def test_control_plane_contention_probe_smoke(tmp_path: Path) -> None:
    result = run_contention_probe(tmp_path / "contention", attempts=20)

    assert result["availability"] == 1.0
    assert result["busy_error_count"] == 0
    assert result["unexpected_error_count"] == 0
    assert result["lock_probe"]["observed_writer_latency_ms"] >= 50.0
    assert result["resources"]["sqlite_connection_count_after"] == 0
    assert result["resources"]["task_growth"] == 0
    assert all(result["verdict"].values())


def test_api_and_cli_redact_local_storage_root_without_mutating_registry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "market-data"
    application = MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=root,
                instance_id="public-dataset-contract-test",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )
    descriptor = _descriptor(root)
    application.context.dataset_registry.register(descriptor)

    try:
        raw_before = application.context.dataset_registry.inspect(descriptor.dataset_id, 1)
        assert raw_before is not None
        public = application.get_dataset(descriptor.dataset_id)
        assert public.storage_uri == "local://managed/public-contract-dataset/v1"
        assert raw_before["storage_uri"] == root.as_uri()

        with TestClient(
            create_api(application, authenticator=local_admin_authenticator()),
            headers=LOCAL_ADMIN_HEADERS,
        ) as client:
            response = client.get(f"/api/v1/datasets/{descriptor.dataset_id}")
        assert response.status_code == 200
        assert response.json()["storage_uri"] == public.storage_uri
        assert str(root) not in response.text
        assert "file://" not in response.text
        assert application.context.dataset_registry.inspect(descriptor.dataset_id, 1) == raw_before

        cli = CliRunner().invoke(
            create_cli(application),
            ["--storage-root", str(root), "datasets", "show", descriptor.dataset_id],
        )
        assert cli.exit_code == 0
        assert public.storage_uri in cli.output
        assert str(root) not in cli.output
        assert "file://" not in cli.output
        assert application.context.dataset_registry.inspect(descriptor.dataset_id, 1) == raw_before
    finally:
        application.shutdown()
