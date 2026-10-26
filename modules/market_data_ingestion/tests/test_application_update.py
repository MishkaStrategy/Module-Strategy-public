from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from market_data_application import (
    ApiRole,
    ApiTokenAuthenticator,
    ApplicationSettings,
    CreateDatasetUpdateRequest,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_application.contracts import JobOperation, StorageFormat
from market_data_application.errors import ApplicationError
from market_data_application.lease_store import activate_job_lease
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.operations import DatasetOperations

BASE_START = datetime(2026, 1, 1, tzinfo=UTC)
BASE_END = datetime(2026, 1, 1, 1, tzinfo=UTC)
UPDATE_END = datetime(2026, 1, 1, 2, tzinfo=UTC)
TERMINAL_SUCCESS = {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}


def application(tmp_path, *, instance_id: str = "update-tests") -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id=instance_id,
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def download_request() -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=BASE_START,
        end=BASE_END,
        storage_format=StorageFormat.CSV,
        compression="zstd",
        concurrency=1,
    )


def update_request(*, end: datetime = UPDATE_END) -> CreateDatasetUpdateRequest:
    return CreateDatasetUpdateRequest(
        version=1,
        end=end,
        required_data=(DataType.KLINES,),
        storage_format=StorageFormat.CSV,
        compression="zstd",
    )


def registered_dataset(app: MarketDataApplication) -> str:
    created = app.create_download(download_request(), idempotency_key="update-parent")
    completed = app.run_job(created.job.job_id)
    assert completed.state in TERMINAL_SUCCESS
    assert completed.result is not None
    return str(completed.result["dataset"]["dataset_id"])


def parsed_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_update_pins_parent_and_publishes_one_immutable_child(tmp_path):
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        parent = app.get_dataset(dataset_id, version=1)
        created = app.create_update(
            dataset_id,
            update_request(),
            idempotency_key="update-once",
            actor_id="operator-a",
        )

        assert created.job.operation is JobOperation.UPDATE
        assert created.job.request["version"] == 1
        assert created.job.request["parent_dataset_hash"] == parent.dataset_hash
        assert parsed_utc(str(created.job.request["parent_end"])) == BASE_END

        completed = app.run_job(created.job.job_id)
        assert completed.state in TERMINAL_SUCCESS
        assert completed.result is not None
        assert completed.result["dataset"]["version"] == 2
        assert app.get_dataset(dataset_id, version=1).end == BASE_END
        assert app.get_dataset(dataset_id, version=2).end == UPDATE_END
        assert app.context.dataset_registry.inspect(dataset_id, version=3) is None
    finally:
        app.shutdown()


def test_update_idempotency_is_scoped_to_actor(tmp_path):
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        first = app.create_update(
            dataset_id,
            update_request(),
            idempotency_key="same-visible-key",
            actor_id="operator-a",
        )
        reused = app.create_update(
            dataset_id,
            update_request(),
            idempotency_key="same-visible-key",
            actor_id="operator-a",
        )
        other_actor = app.create_update(
            dataset_id,
            update_request(),
            idempotency_key="same-visible-key",
            actor_id="operator-b",
        )

        assert reused.reused is True
        assert reused.job.job_id == first.job.job_id
        assert other_actor.reused is False
        assert other_actor.job.job_id != first.job.job_id

        with pytest.raises(ApplicationError) as conflict:
            app.create_update(
                dataset_id,
                update_request(end=datetime(2026, 1, 1, 3, tzinfo=UTC)),
                idempotency_key="same-visible-key",
                actor_id="operator-a",
            )
        assert conflict.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    finally:
        app.shutdown()


def test_update_rejects_non_series_data_and_unavailable_parent_stream(tmp_path):
    with pytest.raises(ValidationError):
        CreateDatasetUpdateRequest(
            version=1,
            end=UPDATE_END,
            required_data=(DataType.INSTRUMENT_METADATA,),
        )

    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        with pytest.raises(ApplicationError) as unavailable:
            app.create_update(
                dataset_id,
                CreateDatasetUpdateRequest(
                    version=1,
                    end=UPDATE_END,
                    required_data=(DataType.FUNDING_RATE,),
                ),
                idempotency_key="missing-stream",
                actor_id="operator-a",
            )
        assert unavailable.value.code == "UPDATE_DATA_TYPE_UNAVAILABLE"
    finally:
        app.shutdown()


def test_update_cancellation_fails_closed(tmp_path):
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        created = app.create_update(
            dataset_id,
            update_request(),
            idempotency_key="no-cancel",
            actor_id="operator-a",
        )
        with pytest.raises(ApplicationError) as cancelled:
            app.cancel_job(created.job.job_id)
        assert cancelled.value.code == "JOB_CANCELLATION_CONFLICT"
        assert app.get_job(created.job.job_id).state is RunState.CREATED
    finally:
        app.shutdown()


def test_restart_recovery_reschedules_update_job(tmp_path):
    first = application(tmp_path, instance_id="update-before-restart")
    dataset_id = registered_dataset(first)
    created = first.create_update(
        dataset_id,
        update_request(),
        idempotency_key="restart-update",
        actor_id="operator-a",
    )
    first.shutdown()

    recovered = application(tmp_path, instance_id="update-after-restart")
    try:
        assert created.job.job_id in recovered.recover_jobs()
        completed = recovered.wait(created.job.job_id, timeout=30)
        assert completed.state in TERMINAL_SUCCESS
        assert completed.result is not None
        assert completed.result["dataset"]["version"] == 2
        assert recovered.context.dataset_registry.inspect(dataset_id, version=3) is None
    finally:
        recovered.shutdown()


def test_post_publication_recovery_reconciles_without_duplicate_version(tmp_path):
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        created = app.create_update(
            dataset_id,
            update_request(),
            idempotency_key="published-before-job-terminal",
            actor_id="operator-a",
        )

        lease = app.context.lease_store.claim(
            created.job.job_id,
            owner_id=app.context.settings.instance_id,
            ttl_seconds=app.context.settings.lease_ttl_seconds,
        )
        assert lease is not None
        try:
            with activate_job_lease(lease):
                for target in (
                    RunState.VALIDATING_REQUEST,
                    RunState.DISCOVERING_CAPABILITIES,
                    RunState.RESOLVING_INSTRUMENT,
                    RunState.PLANNING,
                    RunState.DOWNLOADING,
                    RunState.VERIFYING_RAW,
                    RunState.NORMALIZING,
                    RunState.VALIDATING_DATA,
                    RunState.HASHING,
                    RunState.REGISTERING,
                ):
                    app.context.job_store.transition(created.job.job_id, target)
        finally:
            app.context.lease_store.release(lease)

        published = DatasetOperations(
            service=app.context.ingestion_service,
            storage=app.context.storage,
            registry=app.context.dataset_registry,
        ).update(
            dataset_id,
            version=1,
            end=UPDATE_END,
            required_data=(DataType.KLINES,),
            request_id=created.job.job_id,
            format_name="csv",
            compression="zstd",
        )
        assert published.dataset is not None
        assert published.dataset.version == 2

        reconciled = app.run_job(created.job.job_id)
        assert reconciled.state is RunState.COMPLETED_WITH_WARNINGS
        assert reconciled.result is not None
        assert reconciled.result["dataset"]["version"] == 2
        assert app.context.dataset_registry.inspect(dataset_id, version=3) is None
    finally:
        app.shutdown()


def test_operator_api_and_cli_share_durable_update_contract(tmp_path):
    api_root = tmp_path / "api"
    api_app = application(api_root, instance_id="update-api")
    reader_token = "reader-token-" + "r" * 32
    operator_token = "operator-token-" + "o" * 32
    authenticator = ApiTokenAuthenticator(
        {reader_token: ApiRole.READER, operator_token: ApiRole.OPERATOR}
    )
    try:
        dataset_id = registered_dataset(api_app)
        client = TestClient(create_api(api_app, authenticator=authenticator))
        payload = update_request().model_dump(mode="json")
        denied = client.post(
            f"/api/v1/datasets/{dataset_id}/updates",
            json=payload,
            headers={
                "Authorization": f"Bearer {reader_token}",
                "Idempotency-Key": "api-update",
            },
        )
        accepted = client.post(
            f"/api/v1/datasets/{dataset_id}/updates",
            json=payload,
            headers={
                "Authorization": f"Bearer {operator_token}",
                "Idempotency-Key": "api-update",
            },
        )

        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "API_ACCESS_DENIED"
        assert accepted.status_code == 202
        completed = api_app.wait(accepted.json()["job"]["job_id"], timeout=30)
        assert completed.state in TERMINAL_SUCCESS
        assert completed.result is not None
        assert completed.result["dataset"]["version"] == 2
    finally:
        api_app.shutdown()

    cli_root = tmp_path / "cli"
    cli_app = application(cli_root, instance_id="update-cli")
    try:
        dataset_id = registered_dataset(cli_app)
        request_file = tmp_path / "update-request.json"
        request_file.write_text(update_request().model_dump_json(), encoding="utf-8")
        result = CliRunner().invoke(
            create_cli(cli_app),
            [
                "--storage-root",
                str(cli_root),
                "datasets",
                "update",
                dataset_id,
                "--request",
                str(request_file),
                "--idempotency-key",
                "cli-update",
                "--actor-id",
                "operator-cli",
            ],
        )

        assert result.exit_code == 0, result.output
        response = json.loads(result.output)
        assert response["operation"] == "update"
        assert response["result"]["dataset"]["version"] == 2
    finally:
        cli_app.shutdown()


def test_update_api_maps_missing_and_empty_range_errors(tmp_path):
    app = application(tmp_path)
    operator_token = "operator-errors-" + "x" * 32
    client = TestClient(
        create_api(
            app,
            authenticator=ApiTokenAuthenticator({operator_token: ApiRole.OPERATOR}),
        )
    )
    headers = {
        "Authorization": f"Bearer {operator_token}",
        "Idempotency-Key": "update-errors",
    }
    try:
        missing = client.post(
            "/api/v1/datasets/missing/updates",
            json=update_request().model_dump(mode="json"),
            headers=headers,
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "DATASET_NOT_FOUND"

        dataset_id = registered_dataset(app)
        empty = client.post(
            f"/api/v1/datasets/{dataset_id}/updates",
            json=update_request(end=BASE_END).model_dump(mode="json"),
            headers={**headers, "Idempotency-Key": "empty-range"},
        )
        assert empty.status_code == 422
        assert empty.json()["error"]["code"] == "UPDATE_RANGE_EMPTY"
    finally:
        app.shutdown()
