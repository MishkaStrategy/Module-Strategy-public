import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from auth_helpers import LOCAL_ADMIN_HEADERS, local_admin_authenticator
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApplicationSettings,
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
                instance_id="inspection-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def registered_dataset(app: MarketDataApplication) -> tuple[str, int, Path]:
    created = app.create_download(
        request_model(),
        idempotency_key="inspection-dataset",
    )
    completed = app.run_job(created.job.job_id)
    assert completed.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}
    assert completed.result is not None
    dataset = completed.result["dataset"]
    assert isinstance(dataset, dict)
    dataset_id = str(dataset["dataset_id"])
    version = int(dataset["version"])
    manifest_uri = str(dataset["manifest_uri"])
    return dataset_id, version, app.context.storage.root / manifest_uri


def storage_snapshot(root: Path) -> dict[str, str]:
    included = {
        "raw",
        "normalized",
        "derived",
        "manifests",
        "quarantine",
        "repair_audits",
    }
    snapshot: dict[str, str] = {}
    for layer in included:
        directory = root / layer
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return snapshot


def partition_path(manifest_path: Path, *, layer: str) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for partition in manifest["partitions"]:
        uri = str(partition["storage_uri"])
        if uri.startswith(f"{layer}/") and partition["data_type"] == DataType.KLINES.value:
            return manifest_path.parents[2] / uri
    raise AssertionError(f"no {layer} kline partition found")


def test_valid_dataset_validation_and_preview_are_read_only(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, version, _ = registered_dataset(app)
        before = storage_snapshot(app.context.storage.root)

        validation = app.validate_dataset(dataset_id, version=version, deep=True)
        first = app.preview_repair(dataset_id, version=version, deep=True)
        second = app.preview_repair(dataset_id, version=version, deep=True)
        after = storage_snapshot(app.context.storage.root)

        assert validation.status == "ok"
        assert validation.partitions_checked > 0
        assert validation.raw_artifacts_checked > 0
        assert first.action == "no_action"
        assert first.confirmation_required is False
        assert first.expected_mutations == ()
        assert first.preview_id == second.preview_id
        assert before == after
    finally:
        app.shutdown()


def test_damaged_normalized_partition_produces_deterministic_preview_without_mutation(
    tmp_path: Path,
):
    app = application(tmp_path)
    try:
        dataset_id, version, manifest_path = registered_dataset(app)
        damaged = partition_path(manifest_path, layer="normalized")
        damaged.write_bytes(damaged.read_bytes() + b"tamper")
        before = storage_snapshot(app.context.storage.root)

        validation = app.validate_dataset(dataset_id, version=version)
        first = app.preview_repair(
            dataset_id,
            version=version,
            data_type=DataType.KLINES,
        )
        second = app.preview_repair(
            dataset_id,
            version=version,
            data_type=DataType.KLINES,
        )
        after = storage_snapshot(app.context.storage.root)

        assert validation.status == "error"
        assert first.action == "stream_redownload_and_rebuild"
        assert first.confirmation_required is True
        assert first.requires_network is True
        assert first.damaged_partitions == 1
        assert first.affected_ranges[0].partition_id is not None
        assert "raw_artifact_write" in first.expected_mutations
        assert first.preview_id == second.preview_id
        assert before == after
        assert not any((app.context.storage.root / "repair_audits").iterdir())
    finally:
        app.shutdown()


def test_damaged_derived_partition_uses_local_rebuild_preview(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, version, manifest_path = registered_dataset(app)
        damaged = partition_path(manifest_path, layer="derived")
        damaged.write_bytes(damaged.read_bytes() + b"tamper")

        preview = app.preview_repair(dataset_id, version=version)

        assert preview.action == "derived_local_rebuild"
        assert preview.requires_network is False
        assert preview.expected_mutations[0] == "derived_partition_write"
    finally:
        app.shutdown()


def test_cli_and_api_share_validation_and_preview_contracts(tmp_path: Path):
    app = application(tmp_path)
    runner = CliRunner()
    authenticator = ApiTokenAuthenticator(
        {"reader-token-00000000000000000000000000": ApiRole.READER}
    )
    try:
        dataset_id, version, _ = registered_dataset(app)
        cli_validation = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "validate",
                dataset_id,
                "--version",
                str(version),
            ],
        )
        cli_preview = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "repair-preview",
                dataset_id,
                "--version",
                str(version),
            ],
        )
        client = TestClient(create_api(app, authenticator=authenticator))
        headers = {"Authorization": "Bearer reader-token-00000000000000000000000000"}
        api_validation = client.get(
            f"/api/v1/datasets/{dataset_id}/validation",
            params={"version": version},
            headers=headers,
        )
        api_preview = client.get(
            f"/api/v1/datasets/{dataset_id}/repair-preview",
            params={"version": version},
            headers=headers,
        )
        schema = client.get("/api/v1/openapi.json")

        assert cli_validation.exit_code == 0, cli_validation.output
        assert cli_preview.exit_code == 0, cli_preview.output
        assert api_validation.status_code == 200
        assert api_preview.status_code == 200
        assert json.loads(cli_validation.output) == api_validation.json()
        assert json.loads(cli_preview.output) == api_preview.json()
        assert "/api/v1/datasets/{dataset_id}/validation" in schema.json()["paths"]
        assert "/api/v1/datasets/{dataset_id}/repair-preview" in schema.json()["paths"]
    finally:
        app.shutdown()


def test_missing_dataset_validation_and_preview_map_to_not_found(tmp_path: Path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        cli_validation = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "validate",
                "missing-dataset",
            ],
        )
        cli_preview = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "repair-preview",
                "missing-dataset",
            ],
        )
        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        api_validation = client.get("/api/v1/datasets/missing-dataset/validation")
        api_preview = client.get("/api/v1/datasets/missing-dataset/repair-preview")

        assert cli_validation.exit_code == 4
        assert cli_preview.exit_code == 4
        assert json.loads(cli_validation.output)["error"]["code"] == "DATASET_NOT_FOUND"
        assert json.loads(cli_preview.output)["error"]["code"] == "DATASET_NOT_FOUND"
        assert api_validation.status_code == 404
        assert api_preview.status_code == 404
        assert api_validation.json()["error"]["code"] == "DATASET_NOT_FOUND"
        assert api_preview.json()["error"]["code"] == "DATASET_NOT_FOUND"
    finally:
        app.shutdown()
