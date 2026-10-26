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
    CreateRepairExecutionRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_application.contracts import StorageFormat
from market_data_application.errors import RepairConfirmationRejectedError
from market_data_application.security import ApiRole, ApiTokenAuthenticator
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.operations import DatasetOperations


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
                instance_id="repair-execution-tests",
                worker_count=1,
                repair_confirmation_ttl_seconds=600,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def damaged_dataset(app: MarketDataApplication, *, actor_id: str = "operator-a"):
    created = app.create_download(request_model(), idempotency_key="repair-execution-dataset")
    completed = app.run_job(created.job.job_id)
    assert completed.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}
    assert completed.result is not None
    dataset = completed.result["dataset"]
    assert isinstance(dataset, dict)
    dataset_id = str(dataset["dataset_id"])
    version = int(dataset["version"])
    manifest_path = app.context.storage.root / str(dataset["manifest_uri"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    partition = next(
        item
        for item in manifest["partitions"]
        if item["storage_uri"].startswith("normalized/")
        and item["data_type"] == DataType.KLINES.value
        and item["timeframe"] == "5m"
    )
    partition_path = app.context.storage.root / str(partition["storage_uri"])
    partition_path.write_bytes(partition_path.read_bytes() + b"repair-execution-corruption")
    preview = app.preview_repair(
        dataset_id,
        version=version,
        data_type=DataType.KLINES,
    )
    assert preview.action == "stream_redownload_and_rebuild"
    confirmation = app.create_repair_confirmation(
        dataset_id,
        CreateRepairConfirmationRequest(
            version=version,
            preview_id=preview.preview_id,
            selected_data_type=DataType.KLINES,
            acknowledged_mutations=preview.expected_mutations,
            acknowledgement=f"CONFIRM {preview.preview_id}",
        ),
        actor_id=actor_id,
    )
    execution = CreateRepairExecutionRequest(
        version=version,
        preview_id=preview.preview_id,
        selected_data_type=DataType.KLINES,
        confirmation_id=confirmation.confirmation_id,
        confirmation_token=confirmation.confirmation_token,
        storage_format=StorageFormat.CSV,
        compression="zstd",
    )
    return dataset_id, version, partition_path, preview, confirmation, execution


def immutable_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for layer in ("raw", "normalized", "derived", "manifests", "repair_audits")
        for path in sorted((root / layer).rglob("*"))
        if path.is_file()
    }


def test_repair_job_reserves_digest_only_receipt_and_publishes_new_version(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, version, _, preview, confirmation, execution = damaged_dataset(app)
        before = immutable_digest(app.context.storage.root)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-execution-1",
            actor_id="operator-a",
        )
        reserved = app.get_repair_confirmation(confirmation.confirmation_id)
        stored = app.get_job(created.job.job_id)

        assert created.reused is False
        assert stored.operation.value == "repair"
        assert "confirmation_token" not in stored.request
        assert reserved.status == "reserved"
        assert reserved.reserved_job_id == created.job.job_id
        database_bytes = b"".join(
            path.read_bytes()
            for path in sorted(app.context.storage.root.glob("jobs.sqlite3*"))
            if path.is_file()
        )
        assert confirmation.confirmation_token.encode() not in database_bytes
        assert before == immutable_digest(app.context.storage.root)

        completed = app.run_job(created.job.job_id)
        consumed = app.get_repair_confirmation(confirmation.confirmation_id)

        assert completed.state is RunState.COMPLETED
        assert completed.result is not None
        assert completed.result["status"] == "REPAIRED"
        assert completed.result["new_dataset_version"] == version + 1
        assert consumed.status == "consumed"
        assert consumed.consumed_job_id == created.job.job_id
        repaired = app.context.dataset_registry.inspect(dataset_id, version + 1)
        assert repaired is not None
    finally:
        app.shutdown()


def test_repair_submission_is_atomic_and_idempotent(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, _, _, _, confirmation, execution = damaged_dataset(app)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-idempotent",
            actor_id="operator-a",
        )
        reused = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-idempotent",
            actor_id="operator-a",
        )
        assert reused.reused is True
        assert reused.job.job_id == created.job.job_id

        wrong = CreateRepairExecutionRequest.model_validate(
            {
                **execution.model_dump(mode="python", exclude={"confirmation_token"}),
                "confirmation_token": "x" * 48,
            }
        )
        with pytest.raises(RepairConfirmationRejectedError) as rejected:
            app.create_repair(
                dataset_id,
                wrong,
                idempotency_key="repair-wrong-token",
                actor_id="operator-a",
            )
        assert rejected.value.details["reason"] == "invalid_token"
        listed = app.list_jobs(operations=())
        assert {item.job_id for item in listed.items} >= {created.job.job_id}
        assert all(item.job_id != "repair-wrong-token" for item in listed.items)
        assert app.get_repair_confirmation(confirmation.confirmation_id).status == "reserved"
    finally:
        app.shutdown()


def test_repair_job_revalidates_preview_before_consuming_receipt(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, _, partition, _, confirmation, execution = damaged_dataset(app)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-stale-preview",
            actor_id="operator-a",
        )
        partition.write_bytes(partition.read_bytes() + b"changed-after-reservation")

        failed = app.run_job(created.job.job_id)
        status = app.get_repair_confirmation(confirmation.confirmation_id)

        assert failed.state is RunState.FAILED
        assert failed.error is not None
        assert failed.error.code == "REPAIR_PREVIEW_STALE"
        assert status.status == "reserved"
        assert status.consumed_at is None
        assert app.context.dataset_registry.inspect(dataset_id, 2) is None
    finally:
        app.shutdown()


def test_repair_job_recovers_after_process_crash_post_consume(tmp_path: Path, monkeypatch):
    app = application(tmp_path)
    original = DatasetOperations.repair
    calls = 0

    def crash_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("simulated process crash")
        return original(self, *args, **kwargs)

    try:
        dataset_id, _, _, _, confirmation, execution = damaged_dataset(app)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-crash-recovery",
            actor_id="operator-a",
        )
        monkeypatch.setattr(DatasetOperations, "repair", crash_once)
        with pytest.raises(KeyboardInterrupt):
            app.run_job(created.job.job_id)

        interrupted = app.get_job(created.job.job_id)
        consumed = app.get_repair_confirmation(confirmation.confirmation_id)
        assert interrupted.state is RunState.REPAIRING
        assert consumed.status == "consumed"
        assert consumed.consumed_job_id == created.job.job_id

        recovered = app.run_job(created.job.job_id)
        assert recovered.state is RunState.COMPLETED
        assert recovered.result is not None
        assert recovered.result["new_dataset_version"] == 2
        assert calls == 2
    finally:
        app.shutdown()


def test_repair_job_recovers_after_backend_completed_before_job_commit(tmp_path: Path, monkeypatch):
    app = application(tmp_path)
    original = DatasetOperations.repair
    calls = 0

    def complete_then_crash(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(self, *args, **kwargs)
        if calls == 1:
            raise KeyboardInterrupt("crash after backend registration")
        return result

    try:
        dataset_id, _, _, _, confirmation, execution = damaged_dataset(app)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-post-register-crash",
            actor_id="operator-a",
        )
        monkeypatch.setattr(DatasetOperations, "repair", complete_then_crash)
        with pytest.raises(KeyboardInterrupt):
            app.run_job(created.job.job_id)

        assert app.context.dataset_registry.inspect(dataset_id, 2) is not None
        assert app.get_job(created.job.job_id).state is RunState.REPAIRING
        assert app.get_repair_confirmation(confirmation.confirmation_id).status == "consumed"

        recovered = app.run_job(created.job.job_id)
        assert recovered.state is RunState.COMPLETED
        assert recovered.result is not None
        assert recovered.result["new_dataset_version"] == 2
        assert app.context.dataset_registry.inspect(dataset_id, 3) is None
        assert calls == 2

        reused = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-post-register-crash",
            actor_id="operator-a",
        )
        assert reused.reused is True
        assert reused.job.job_id == created.job.job_id
    finally:
        app.shutdown()


def test_api_and_cli_execute_the_same_operator_bound_repair(tmp_path: Path):
    api_app = application(tmp_path / "api")
    authenticator = ApiTokenAuthenticator(
        {
            "reader-token-00000000000000000000000000": ApiRole.READER,
            "operator-token-000000000000000000000000": ApiRole.OPERATOR,
        }
    )
    try:
        operator_principal = authenticator.authenticate(
            "operator-token-000000000000000000000000",
            required_role=ApiRole.OPERATOR,
        )
        dataset_id, _, _, _, _, execution = damaged_dataset(
            api_app,
            actor_id=operator_principal.principal_id,
        )
        client = TestClient(create_api(api_app, authenticator=authenticator))
        execution_payload = execution.model_dump(mode="json", exclude={"confirmation_token"})
        execution_payload["confirmation_token"] = execution.confirmation_token.get_secret_value()
        denied = client.post(
            f"/api/v1/datasets/{dataset_id}/repairs",
            json=execution_payload,
            headers={
                "Authorization": "Bearer reader-token-00000000000000000000000000",
                "Idempotency-Key": "api-repair",
            },
        )
        created = client.post(
            f"/api/v1/datasets/{dataset_id}/repairs",
            json=execution_payload,
            headers={
                "Authorization": "Bearer operator-token-000000000000000000000000",
                "Idempotency-Key": "api-repair",
            },
        )
        assert denied.status_code == 403
        assert created.status_code == 202, created.text
        job_id = created.json()["job"]["job_id"]
        completed = api_app.wait(job_id, timeout=10)
        assert completed.state is RunState.COMPLETED
        assert (
            "/api/v1/datasets/{dataset_id}/repairs"
            in client.get("/api/v1/openapi.json").json()["paths"]
        )
    finally:
        api_app.shutdown()

    cli_app = application(tmp_path / "cli")
    runner = CliRunner()
    try:
        dataset_id, _, _, _, _, execution = damaged_dataset(cli_app)
        request_path = tmp_path / "repair-execution.json"
        execution_payload = execution.model_dump(mode="json", exclude={"confirmation_token"})
        execution_payload["confirmation_token"] = execution.confirmation_token.get_secret_value()
        request_path.write_text(json.dumps(execution_payload, indent=2), encoding="utf-8")
        result = runner.invoke(
            create_cli(cli_app),
            [
                "--storage-root",
                str(tmp_path / "cli"),
                "datasets",
                "repair-execute",
                dataset_id,
                "--request",
                str(request_path),
                "--idempotency-key",
                "cli-repair",
                "--actor-id",
                "operator-a",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["state"] == "COMPLETED"
        assert payload["operation"] == "repair"
    finally:
        cli_app.shutdown()


def test_repair_job_recovers_after_crash_after_backend_published_version(
    tmp_path: Path, monkeypatch
):
    app = application(tmp_path)
    original = DatasetOperations.repair
    calls = 0
    published_version = None

    def crash_after_repair(self, *args, **kwargs):
        nonlocal calls, published_version
        calls += 1
        result = original(self, *args, **kwargs)
        published_version = result.new_dataset_version
        if calls == 1:
            raise KeyboardInterrupt("simulated process crash after backend repair")
        return result

    try:
        dataset_id, _, _, _, confirmation, execution = damaged_dataset(app)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-crash-after-publish",
            actor_id="operator-a",
        )
        monkeypatch.setattr(DatasetOperations, "repair", crash_after_repair)
        with pytest.raises(KeyboardInterrupt):
            app.run_job(created.job.job_id)

        interrupted = app.get_job(created.job.job_id)
        assert interrupted.state is RunState.REPAIRING
        assert published_version == 2
        assert app.context.dataset_registry.inspect(dataset_id, 2) is not None
        assert app.get_repair_confirmation(confirmation.confirmation_id).status == "consumed"

        recovered = app.run_job(created.job.job_id)
        assert recovered.state is RunState.COMPLETED
        assert recovered.result is not None
        assert recovered.result["new_dataset_version"] == 2
        assert calls == 2
        assert app.context.dataset_registry.inspect(dataset_id, 3) is None
    finally:
        app.shutdown()


def test_consumed_repair_receipt_still_enforces_actor_and_preview(tmp_path: Path):
    app = application(tmp_path)
    try:
        dataset_id, _, _, preview, confirmation, execution = damaged_dataset(app)
        created = app.create_repair(
            dataset_id,
            execution,
            idempotency_key="repair-consumed-binding",
            actor_id="operator-a",
        )
        app.context.repair_confirmation_store.consume_reserved(
            confirmation.confirmation_id,
            job_id=created.job.job_id,
            actor_id="operator-a",
            preview_id=preview.preview_id,
        )

        with pytest.raises(RepairConfirmationRejectedError) as actor_error:
            app.context.repair_confirmation_store.consume_reserved(
                confirmation.confirmation_id,
                job_id=created.job.job_id,
                actor_id="operator-b",
                preview_id=preview.preview_id,
            )
        assert actor_error.value.details["reason"] == "actor_mismatch"

        with pytest.raises(RepairConfirmationRejectedError) as preview_error:
            app.context.repair_confirmation_store.consume_reserved(
                confirmation.confirmation_id,
                job_id=created.job.job_id,
                actor_id="operator-a",
                preview_id="repair-preview:" + "f" * 24,
            )
        assert preview_error.value.details["reason"] == "preview_mismatch"
    finally:
        app.shutdown()
