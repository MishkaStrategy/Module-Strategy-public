import json
from datetime import UTC, datetime
from threading import Event

import pytest
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
from market_data_application.errors import JobCancellationConflictError
from market_data_application.security import ApiRole, ApiTokenAuthenticator
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.adapters.base import AcquiredData
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.execution_identity import ingestion_execution_identity


class BlockingFakeExchangeAdapter(FakeExchangeAdapter):
    def __init__(self) -> None:
        super().__init__(chunk_bars=6)
        self.acquire_started = Event()
        self.acquire_release = Event()

    def acquire(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> AcquiredData:
        self.acquire_started.set()
        if not self.acquire_release.wait(timeout=10):
            raise TimeoutError("test adapter release was not signalled")
        return super().acquire(chunk, instrument)


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


def application(tmp_path, adapter=None, *, instance_id="cancellation-tests"):
    selected = adapter or FakeExchangeAdapter(chunk_bars=6)
    app = MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id=instance_id,
                worker_count=1,
            ),
            adapters={"fake": selected},
        )
    )
    return app, selected


def test_execution_identity_matches_pipeline_result(tmp_path):
    app, adapter = application(tmp_path)
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="identity-parity",
        )
        identity = ingestion_execution_identity(
            request_model().to_domain(request_id=created.job.job_id),
            adapter,
            format_name="csv",
            compression="zstd",
            reject_unresolved_required_gaps=True,
            concurrency=1,
            max_records_in_memory=5_000_000,
        )

        completed = app.run_job(created.job.job_id)

        assert completed.backend_run_id == identity.run_id
        assert completed.result is not None
        assert completed.result["run_id"] == identity.run_id
    finally:
        app.shutdown()


def test_created_job_is_cancelled_without_execution(tmp_path):
    app, adapter = application(tmp_path)
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="cancel-created",
        )

        response = app.cancel_job(created.job.job_id)

        assert response.status == "cancelled"
        assert response.job.state is RunState.CANCELLED
        assert response.backend_run_id == response.job.backend_run_id
        assert adapter.acquired_chunks == []
        assert app.context.lease_store.get(created.job.job_id) is None
        assert "cancellation requested by user" in response.job.warnings
    finally:
        app.shutdown()


def test_worker_returns_fresh_state_when_cancel_wins_claim_race(tmp_path, monkeypatch):
    app, _ = application(tmp_path)
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="cancel-claim-race",
        )

        def cancel_before_claim(job_id, **_kwargs):
            app.context.job_store.transition(job_id, RunState.CANCELLED)
            return None

        monkeypatch.setattr(app.context.lease_store, "claim", cancel_before_claim)

        observed = app.run_job(created.job.job_id)

        assert observed.state is RunState.CANCELLED
        assert app.get_job(created.job.job_id).state is RunState.CANCELLED
    finally:
        app.shutdown()


def test_running_job_uses_durable_cancel_marker(tmp_path):
    adapter = BlockingFakeExchangeAdapter()
    app, _ = application(tmp_path, adapter)
    try:
        created = app.submit_download(
            request_model(),
            idempotency_key="cancel-running",
        )
        assert adapter.acquire_started.wait(timeout=10)

        requested = app.cancel_job(created.job.job_id)
        assert requested.status == "requested"
        assert requested.backend_run_id is not None
        assert app.context.storage.is_cancel_requested(requested.backend_run_id)

        adapter.acquire_release.set()
        cancelled = app.wait(created.job.job_id, timeout=30)

        assert cancelled.state is RunState.CANCELLED
        assert cancelled.backend_run_id == requested.backend_run_id
        assert cancelled.result is not None
        assert cancelled.result["final_state"] == RunState.CANCELLED.value
    finally:
        adapter.acquire_release.set()
        app.shutdown()


def test_terminal_cancel_is_idempotent_and_clears_stale_marker(tmp_path):
    app, _ = application(tmp_path)
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="cancel-terminal",
        )
        completed = app.run_job(created.job.job_id)
        assert completed.backend_run_id is not None
        app.context.storage.request_cancel(completed.backend_run_id)

        response = app.cancel_job(completed.job_id)

        assert response.status == "already_terminal"
        assert response.job.state is completed.state
        assert response.backend_run_id == completed.backend_run_id
        assert not app.context.storage.is_cancel_requested(completed.backend_run_id)
    finally:
        app.shutdown()


def test_late_phase_cancel_fails_closed(tmp_path):
    app, _ = application(tmp_path)
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="cancel-late",
        )
        for state in (
            RunState.VALIDATING_REQUEST,
            RunState.DISCOVERING_CAPABILITIES,
            RunState.RESOLVING_INSTRUMENT,
            RunState.PLANNING,
            RunState.DOWNLOADING,
            RunState.VERIFYING_RAW,
        ):
            app.context.job_store.transition(created.job.job_id, state)

        with pytest.raises(JobCancellationConflictError) as captured:
            app.cancel_job(created.job.job_id)

        assert captured.value.code == "JOB_CANCELLATION_CONFLICT"
        assert captured.value.details["state"] == RunState.VERIFYING_RAW.value
    finally:
        app.shutdown()


def test_cli_and_api_cancel_share_contract_and_operator_role(tmp_path):
    app, _ = application(tmp_path)
    runner = CliRunner()
    authenticator = ApiTokenAuthenticator(
        {
            "reader-token-00000000000000000000000000": ApiRole.READER,
            "operator-token-000000000000000000000000": ApiRole.OPERATOR,
        }
    )
    try:
        cli_job = app.create_download(
            request_model(),
            idempotency_key="cancel-cli",
        )
        cli_result = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "jobs",
                "cancel",
                cli_job.job.job_id,
            ],
        )
        assert cli_result.exit_code == 0, cli_result.output
        cli_payload = json.loads(cli_result.output)
        assert cli_payload["status"] == "cancelled"
        assert cli_payload["job"]["state"] == RunState.CANCELLED.value

        api_job = app.create_download(
            request_model(),
            idempotency_key="cancel-api",
        )
        client = TestClient(create_api(app, authenticator=authenticator))
        denied = client.post(
            f"/api/v1/jobs/{api_job.job.job_id}/cancel",
            headers={"Authorization": "Bearer reader-token-00000000000000000000000000"},
        )
        accepted = client.post(
            f"/api/v1/jobs/{api_job.job.job_id}/cancel",
            headers={"Authorization": "Bearer operator-token-000000000000000000000000"},
        )
        schema = client.get("/api/v1/openapi.json")

        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "API_ACCESS_DENIED"
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "cancelled"
        assert accepted.json()["job"]["state"] == RunState.CANCELLED.value
        assert "/api/v1/jobs/{job_id}/cancel" in schema.json()["paths"]
    finally:
        app.shutdown()
