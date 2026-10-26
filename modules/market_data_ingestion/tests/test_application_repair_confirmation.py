import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    CreateRepairConfirmationRequest,
    MarketDataApplication,
    SQLiteRepairConfirmationStore,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_application.contracts import StorageFormat
from market_data_application.errors import (
    RepairConfirmationConflictError,
    RepairConfirmationRejectedError,
    RepairPreviewStaleError,
)
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
                instance_id="repair-confirmation-tests",
                worker_count=1,
                repair_confirmation_ttl_seconds=600,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def registered_dataset(app: MarketDataApplication) -> tuple[str, int, Path]:
    created = app.create_download(
        request_model(),
        idempotency_key="repair-confirmation-dataset",
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


def normalized_partition(manifest_path: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for partition in manifest["partitions"]:
        uri = str(partition["storage_uri"])
        if uri.startswith("normalized/") and partition["data_type"] == DataType.KLINES.value:
            return manifest_path.parents[2] / uri
    raise AssertionError("no normalized kline partition found")


def immutable_snapshot(root: Path) -> dict[str, str]:
    layers = {
        "raw",
        "normalized",
        "derived",
        "manifests",
        "quarantine",
        "repair_audits",
    }
    output: dict[str, str] = {}
    for layer in layers:
        for path in sorted((root / layer).rglob("*")):
            if path.is_file() and not path.is_symlink():
                output[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return output


def damaged_preview(app: MarketDataApplication):
    dataset_id, version, manifest_path = registered_dataset(app)
    partition = normalized_partition(manifest_path)
    partition.write_bytes(partition.read_bytes() + b"tamper")
    preview = app.preview_repair(
        dataset_id,
        version=version,
        data_type=DataType.KLINES,
    )
    assert preview.confirmation_required is True
    return dataset_id, version, partition, preview


def confirmation_request(preview, *, acknowledgement: str | None = None):
    return CreateRepairConfirmationRequest(
        version=preview.version,
        preview_id=preview.preview_id,
        selected_data_type=preview.selected_data_type,
        acknowledged_mutations=preview.expected_mutations,
        acknowledgement=acknowledgement or f"CONFIRM {preview.preview_id}",
    )


def test_confirmation_recomputes_preview_and_stores_only_token_digest(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, _, _, preview = damaged_preview(app)
        before = immutable_snapshot(app.context.storage.root)

        issued = app.create_repair_confirmation(
            dataset_id,
            confirmation_request(preview),
            actor_id="operator-a",
        )
        status = app.get_repair_confirmation(issued.confirmation_id)
        after = immutable_snapshot(app.context.storage.root)

        assert issued.confirmation_token
        assert issued.actor_id == "operator-a"
        assert issued.preview_id == preview.preview_id
        assert issued.acknowledged_mutations == preview.expected_mutations
        assert status.status == "active"
        assert status.preview_id == preview.preview_id
        assert before == after
        assert not any((app.context.storage.root / "repair_audits").iterdir())
        database_bytes = b"".join(
            path.read_bytes()
            for path in sorted(app.context.storage.root.glob("jobs.sqlite3*"))
            if path.is_file()
        )
        assert issued.confirmation_token.encode() not in database_bytes
    finally:
        app.shutdown()


def test_confirmation_rejects_stale_preview_exact_phrase_and_mutation_mismatch(
    tmp_path: Path,
):
    app = application(tmp_path)
    try:
        dataset_id, _, partition, preview = damaged_preview(app)

        with pytest.raises(RepairConfirmationConflictError) as phrase_error:
            app.create_repair_confirmation(
                dataset_id,
                confirmation_request(preview, acknowledgement="CONFIRM SOMETHING ELSE"),
                actor_id="operator-a",
            )
        assert phrase_error.value.details["reason"] == "confirmation_phrase_mismatch"

        wrong_mutations = preview.expected_mutations[:-1]
        assert wrong_mutations
        with pytest.raises(RepairConfirmationConflictError) as mutation_error:
            app.create_repair_confirmation(
                dataset_id,
                CreateRepairConfirmationRequest(
                    version=preview.version,
                    preview_id=preview.preview_id,
                    selected_data_type=preview.selected_data_type,
                    acknowledged_mutations=wrong_mutations,
                    acknowledgement=f"CONFIRM {preview.preview_id}",
                ),
                actor_id="operator-a",
            )
        assert mutation_error.value.details["reason"] == "mutation_acknowledgement_mismatch"

        partition.write_bytes(partition.read_bytes() + b"changed-again")
        with pytest.raises(RepairPreviewStaleError):
            app.create_repair_confirmation(
                dataset_id,
                confirmation_request(preview),
                actor_id="operator-a",
            )
    finally:
        app.shutdown()


def test_healthy_dataset_cannot_receive_repair_confirmation(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, version, _ = registered_dataset(app)
        preview = app.preview_repair(dataset_id, version=version)
        assert preview.action == "no_action"

        with pytest.raises(RepairConfirmationConflictError) as captured:
            app.create_repair_confirmation(
                dataset_id,
                CreateRepairConfirmationRequest(
                    version=version,
                    preview_id=preview.preview_id,
                    selected_data_type=None,
                    acknowledged_mutations=("manifest_write",),
                    acknowledgement=f"CONFIRM {preview.preview_id}",
                ),
                actor_id="operator-a",
            )
        assert captured.value.details["reason"] == "no_repair_required"
    finally:
        app.shutdown()


def test_confirmation_token_is_one_time_and_actor_bound(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, _, _, preview = damaged_preview(app)
        issued = app.create_repair_confirmation(
            dataset_id,
            confirmation_request(preview),
            actor_id="operator-a",
        )

        with pytest.raises(RepairConfirmationRejectedError) as actor_error:
            app.context.repair_confirmation_store.consume(
                issued.confirmation_id,
                token=issued.confirmation_token,
                actor_id="operator-b",
                preview_id=preview.preview_id,
            )
        assert actor_error.value.details["reason"] == "actor_mismatch"

        consumed = app.context.repair_confirmation_store.consume(
            issued.confirmation_id,
            token=issued.confirmation_token,
            actor_id="operator-a",
            preview_id=preview.preview_id,
        )
        assert consumed.status == "consumed"

        with pytest.raises(RepairConfirmationRejectedError) as reused:
            app.context.repair_confirmation_store.consume(
                issued.confirmation_id,
                token=issued.confirmation_token,
                actor_id="operator-a",
                preview_id=preview.preview_id,
            )
        assert reused.value.details["reason"] == "already_consumed"
    finally:
        app.shutdown()


def test_confirmation_store_reports_expired_without_plaintext_token(tmp_path: Path):
    token = "deterministic-confirmation-token-0000000000000000"
    store = SQLiteRepairConfirmationStore(
        tmp_path / "confirmations.sqlite3",
        token_factory=lambda _size: token,
    )
    issued = store.issue(
        dataset_id="dataset_expired",
        version=1,
        preview_id="repair-preview:" + "a" * 24,
        action="derived_local_rebuild",
        selected_data_type=DataType.KLINES,
        acknowledged_mutations=("derived_partition_write",),
        actor_id="operator-a",
        ttl_seconds=60,
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )

    assert issued.record.status == "expired"
    assert token.encode() not in (tmp_path / "confirmations.sqlite3").read_bytes()
    with pytest.raises(RepairConfirmationRejectedError) as expired:
        store.consume(
            issued.record.confirmation_id,
            token=token,
            actor_id="operator-a",
            preview_id=issued.record.preview_id,
        )
    assert expired.value.details["reason"] == "expired"


def test_api_requires_operator_and_exposes_confirmation_status(tmp_path: Path):
    app = application(tmp_path)
    authenticator = ApiTokenAuthenticator(
        {
            "reader-token-00000000000000000000000000": ApiRole.READER,
            "operator-token-000000000000000000000000": ApiRole.OPERATOR,
        }
    )
    try:
        dataset_id, _, _, preview = damaged_preview(app)
        client = TestClient(create_api(app, authenticator=authenticator))
        payload = confirmation_request(preview).model_dump(mode="json")
        reader_headers = {"Authorization": "Bearer reader-token-00000000000000000000000000"}
        operator_headers = {"Authorization": "Bearer operator-token-000000000000000000000000"}

        denied = client.post(
            f"/api/v1/datasets/{dataset_id}/repair-confirmations",
            json=payload,
            headers=reader_headers,
        )
        created = client.post(
            f"/api/v1/datasets/{dataset_id}/repair-confirmations",
            json=payload,
            headers=operator_headers,
        )
        assert denied.status_code == 403
        assert created.status_code == 201
        body = created.json()
        assert body["confirmation_token"]
        assert body["preview_id"] == preview.preview_id

        status_denied = client.get(
            f"/api/v1/repair-confirmations/{body['confirmation_id']}",
            headers=reader_headers,
        )
        status_response = client.get(
            f"/api/v1/repair-confirmations/{body['confirmation_id']}",
            headers=operator_headers,
        )
        schema = client.get("/api/v1/openapi.json").json()["paths"]

        assert status_denied.status_code == 403
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "active"
        assert "/api/v1/datasets/{dataset_id}/repair-confirmations" in schema
        assert "/api/v1/repair-confirmations/{confirmation_id}" in schema
    finally:
        app.shutdown()


def test_cli_creates_and_reads_confirmation_and_missing_maps_to_not_found(tmp_path: Path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        dataset_id, _, _, preview = damaged_preview(app)
        request_path = tmp_path / "repair-confirmation.json"
        request_path.write_text(
            confirmation_request(preview).model_dump_json(indent=2),
            encoding="utf-8",
        )
        created = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "repair-confirm",
                dataset_id,
                "--request",
                str(request_path),
                "--actor-id",
                "cli-operator",
            ],
        )
        assert created.exit_code == 0, created.output
        created_payload = json.loads(created.output)
        assert created_payload["preview_id"] == preview.preview_id
        assert created_payload["actor_id"] == "cli-operator"

        shown = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "repair-confirmation-show",
                created_payload["confirmation_id"],
            ],
        )
        missing = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "datasets",
                "repair-confirmation-show",
                "repair-confirmation:" + "f" * 24,
            ],
        )

        assert shown.exit_code == 0, shown.output
        assert json.loads(shown.output)["status"] == "active"
        assert missing.exit_code == 4
        assert json.loads(missing.output)["error"]["code"] == ("REPAIR_CONFIRMATION_NOT_FOUND")
    finally:
        app.shutdown()
