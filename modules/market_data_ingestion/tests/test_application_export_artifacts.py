import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApplicationSettings,
    CreateDatasetExportRequest,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_application.contracts import StorageFormat
from market_data_application.security import ApiRole, ApiTokenAuthenticator
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.registry import descriptor_from_dict


def request_model() -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
        storage_format=StorageFormat.CSV,
        compression="zstd",
        concurrency=1,
    )


def application(tmp_path: Path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="export-artifact-tests",
                worker_count=1,
                artifact_max_bytes=16 * 1024 * 1024,
                artifact_retention_total_bytes=32 * 1024 * 1024,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def registered_dataset(app: MarketDataApplication) -> str:
    created = app.create_download(
        request_model(),
        idempotency_key="export-source-dataset",
    )
    completed = app.run_job(created.job.job_id)
    assert completed.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}
    assert completed.result is not None
    return str(completed.result["dataset"]["dataset_id"])


def run_export(
    app: MarketDataApplication,
    dataset_id: str,
    *,
    key: str,
) -> dict[str, object]:
    created = app.create_export(
        dataset_id,
        CreateDatasetExportRequest(),
        idempotency_key=key,
        actor_id="test-operator",
    )
    completed = app.run_job(created.job.job_id)
    assert completed.state is RunState.COMPLETED, completed.error
    assert completed.result is not None
    return completed.result


def failed_export(
    app: MarketDataApplication,
    dataset_id: str,
    *,
    key: str,
) -> str:
    created = app.create_export(
        dataset_id,
        CreateDatasetExportRequest(),
        idempotency_key=key,
        actor_id="test-operator",
    )
    failed = app.run_job(created.job.job_id)
    assert failed.state is RunState.FAILED
    assert failed.error is not None
    return failed.error.code


def test_export_is_deterministic_and_manifest_is_transport_only(tmp_path: Path) -> None:
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        first = run_export(app, dataset_id, key="export-one")
        second = run_export(app, dataset_id, key="export-two")

        assert first == second
        artifact_id = str(first["artifact_id"])
        descriptor = app.get_artifact(artifact_id)
        content = app.artifact_content(artifact_id)
        assert descriptor.checksum_sha256 == first["checksum_sha256"]
        assert content.path.parent == tmp_path / "artifacts"

        with zipfile.ZipFile(content.path) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("manifest.json"))
        assert names == sorted(names)
        assert {"descriptor.json", "manifest.json"}.issubset(names)
        assert all(item["storage_uri"].startswith("data/") for item in manifest["partitions"])

        payload = app.context.dataset_registry.inspect(dataset_id)
        assert payload is not None
        original = load_manifest(tmp_path, descriptor_from_dict(payload))
        assert all(not item.storage_uri.startswith("data/") for item in original.partitions)
    finally:
        app.shutdown()


def test_export_recovers_missing_sidecar_or_zip_without_duplicate(tmp_path: Path) -> None:
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        first = run_export(app, dataset_id, key="export-recovery-one")
        artifact_id = str(first["artifact_id"])
        metadata = tmp_path / "artifacts" / f"{artifact_id}.json"
        payload = tmp_path / "artifacts" / f"{artifact_id}.zip"

        metadata.unlink()
        recovered_metadata = run_export(app, dataset_id, key="export-recovery-two")
        assert recovered_metadata == first
        assert metadata.is_file()

        payload.unlink()
        recovered_payload = run_export(app, dataset_id, key="export-recovery-three")
        assert recovered_payload == first
        assert payload.is_file()
        assert len(list((tmp_path / "artifacts").glob("*.zip"))) == 1
    finally:
        app.shutdown()


def test_export_fails_closed_for_corrupt_existing_artifact(tmp_path: Path) -> None:
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        first = run_export(app, dataset_id, key="export-corrupt-one")
        artifact_id = str(first["artifact_id"])
        payload = tmp_path / "artifacts" / f"{artifact_id}.zip"
        payload.write_bytes(b"corrupt")

        assert failed_export(app, dataset_id, key="export-corrupt-two") == (
            "ARTIFACT_INTEGRITY_FAILED"
        )
        assert payload.read_bytes() == b"corrupt"
    finally:
        app.shutdown()


def test_export_rejects_registered_partition_symlink(tmp_path: Path) -> None:
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        payload = app.context.dataset_registry.inspect(dataset_id)
        assert payload is not None
        descriptor = descriptor_from_dict(payload)
        manifest = load_manifest(tmp_path, descriptor)
        partition = tmp_path / manifest.partitions[0].storage_uri
        outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
        outside.write_bytes(partition.read_bytes())
        partition.unlink()
        partition.symlink_to(outside)

        assert failed_export(app, dataset_id, key="export-symlink") == ("ARTIFACT_INTEGRITY_FAILED")
    finally:
        app.shutdown()


def test_export_rejects_registered_manifest_hash_mismatch(tmp_path: Path) -> None:
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        payload = app.context.dataset_registry.inspect(dataset_id)
        assert payload is not None
        descriptor = descriptor_from_dict(payload)
        manifest_path = tmp_path / descriptor.manifest_uri
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_payload["validation_policy_version"] = "tampered-policy"
        manifest_path.write_text(
            json.dumps(manifest_payload, sort_keys=True),
            encoding="utf-8",
        )

        assert failed_export(app, dataset_id, key="export-tampered-manifest") == (
            "ARTIFACT_INTEGRITY_FAILED"
        )
        assert not list((tmp_path / "artifacts").glob("*.zip"))
    finally:
        app.shutdown()


def test_export_validates_sidecar_before_recovering_missing_zip(tmp_path: Path) -> None:
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        first = run_export(app, dataset_id, key="export-sidecar-one")
        artifact_id = str(first["artifact_id"])
        metadata = tmp_path / "artifacts" / f"{artifact_id}.json"
        payload = tmp_path / "artifacts" / f"{artifact_id}.zip"
        payload.unlink()
        metadata_payload = json.loads(metadata.read_text(encoding="utf-8"))
        metadata_payload["checksum_sha256"] = "0" * 64
        metadata.write_text(
            json.dumps(metadata_payload, sort_keys=True),
            encoding="utf-8",
        )

        assert failed_export(app, dataset_id, key="export-sidecar-two") == (
            "ARTIFACT_INTEGRITY_FAILED"
        )
        assert not payload.exists()
    finally:
        app.shutdown()


def test_api_and_cli_enforce_roles_and_expose_artifacts(tmp_path: Path) -> None:
    app = application(tmp_path)
    reader_token = "reader-token-000000000000000000000000"
    operator_token = "operator-token-0000000000000000000000"
    authenticator = ApiTokenAuthenticator(
        {
            reader_token: ApiRole.READER,
            operator_token: ApiRole.OPERATOR,
        }
    )
    client = TestClient(create_api(app, authenticator=authenticator))
    runner = CliRunner()
    try:
        dataset_id = registered_dataset(app)
        reader_headers = {"Authorization": f"Bearer {reader_token}"}
        operator_headers = {
            "Authorization": f"Bearer {operator_token}",
            "Idempotency-Key": "api-export",
        }
        denied = client.post(
            f"/api/v1/datasets/{dataset_id}/exports",
            json={},
            headers={**reader_headers, "Idempotency-Key": "reader-export"},
        )
        accepted = client.post(
            f"/api/v1/datasets/{dataset_id}/exports",
            json={},
            headers=operator_headers,
        )
        assert denied.status_code == 403
        assert accepted.status_code == 202
        completed = app.wait(accepted.json()["job"]["job_id"], timeout=10)
        assert completed.state is RunState.COMPLETED
        assert completed.result is not None
        artifact_id = str(completed.result["artifact_id"])

        metadata = client.get(f"/api/v1/artifacts/{artifact_id}", headers=reader_headers)
        content = client.get(
            f"/api/v1/artifacts/{artifact_id}/content",
            headers=reader_headers,
        )
        cli_show = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "artifacts", "show", artifact_id],
        )
        cli_export = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "export",
                dataset_id,
                "--idempotency-key",
                "cli-export",
                "--actor-id",
                "cli-operator",
            ],
        )

        assert metadata.status_code == 200
        assert content.status_code == 200
        assert content.headers["content-type"] == "application/zip"
        assert cli_show.exit_code == 0, cli_show.output
        assert cli_export.exit_code == 0, cli_export.output
        assert json.loads(cli_show.output)["artifact_id"] == artifact_id
        assert json.loads(cli_export.output)["state"] == RunState.COMPLETED.value
    finally:
        app.shutdown()
