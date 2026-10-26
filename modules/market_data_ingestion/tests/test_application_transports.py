import json
from datetime import UTC, datetime

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
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType


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


def application(tmp_path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="transport-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def test_version_health_and_openapi(tmp_path):
    app = application(tmp_path)
    try:
        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        version = client.get("/api/v1/version")
        health = client.get("/api/v1/health")
        schema = client.get("/api/v1/openapi.json")

        assert version.status_code == 200
        assert version.json()["api_version"] == "v1"
        assert health.status_code == 200
        assert health.json()["checks"]["jobs"] == "ok"
        assert "/api/v1/downloads" in schema.json()["paths"]
        assert "/api/v1/jobs/{job_id}" in schema.json()["paths"]
    finally:
        app.shutdown()


def test_cli_and_api_share_request_hash_and_idempotency(tmp_path):
    app = application(tmp_path)
    request_path = tmp_path / "download.json"
    request_path.write_text(request_model().model_dump_json(), encoding="utf-8")
    runner = CliRunner()
    cli = create_cli(app)
    try:
        cli_result = runner.invoke(
            cli,
            [
                "--storage-root",
                str(tmp_path),
                "download",
                "--request",
                str(request_path),
                "--idempotency-key",
                "transport-parity",
            ],
        )
        assert cli_result.exit_code == 0, cli_result.output
        cli_payload = json.loads(cli_result.output)

        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        response = client.post(
            "/api/v1/downloads",
            headers={"Idempotency-Key": "transport-parity"},
            json=request_model().model_dump(mode="json"),
        )
        assert response.status_code == 202, response.text
        api_payload = response.json()

        assert api_payload["reused"] is True
        assert api_payload["job"]["job_id"] == cli_payload["job_id"]
        assert api_payload["job"]["request_hash"] == cli_payload["request_hash"]

        listed = client.get("/api/v1/jobs")
        shown = client.get(f"/api/v1/jobs/{cli_payload['job_id']}")
        assert listed.status_code == 200
        assert listed.json()["pagination"]["total"] == 1
        assert shown.status_code == 200
        assert shown.json()["job_id"] == cli_payload["job_id"]
    finally:
        app.shutdown()


def test_cli_and_api_map_not_found_consistently(tmp_path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        cli_result = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "jobs", "show", "missing-job"],
        )
        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        response = client.get("/api/v1/jobs/missing-job")

        assert cli_result.exit_code == 4
        assert json.loads(cli_result.output)["error"]["code"] == "JOB_NOT_FOUND"
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "JOB_NOT_FOUND"
    finally:
        app.shutdown()


def test_event_replay_matches_cli_and_supports_sse_resume(tmp_path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="transport-events",
        )
        completed = app.run_job(created.job.job_id)
        assert completed.result is not None

        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        replay = client.get(f"/api/v1/jobs/{completed.job_id}/events")
        assert replay.status_code == 200
        events = replay.json()
        assert len(events) >= 2
        first_id = events[0]["event_id"]

        cli_result = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "jobs",
                "events",
                completed.job_id,
                "--after-event-id",
                str(first_id),
            ],
        )
        assert cli_result.exit_code == 0, cli_result.output
        cli_events = json.loads(cli_result.output)
        assert [item["event_id"] for item in cli_events] == [
            item["event_id"] for item in events if item["event_id"] > first_id
        ]

        stream = client.get(
            f"/api/v1/jobs/{completed.job_id}/events/stream",
            headers={"Last-Event-ID": str(first_id)},
        )
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert f"id: {first_id}\n" not in stream.text
        for event in events[1:]:
            assert f"id: {event['event_id']}\n" in stream.text
    finally:
        app.shutdown()
