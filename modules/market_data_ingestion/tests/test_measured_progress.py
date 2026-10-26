from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApiRole,
    ApiTokenAuthenticator,
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_application.contracts import JobEventType, JobProgress, StorageFormat
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.adapters.base import AcquiredData
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, RunState, SourceKind
from market_data_ingestion.errors import AdapterError
from market_data_ingestion.progress import (
    DownloadProgressTracker,
    IngestionProgressSnapshot,
    observe_progress,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=1)
TERMINAL_SUCCESS = {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}


class FailSecondChunkOnceAdapter(FakeExchangeAdapter):
    def __init__(self) -> None:
        super().__init__(chunk_bars=2)
        self.failed = False

    def acquire(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        if chunk.chunk_id.endswith(":000002") and not self.failed:
            self.failed = True
            raise AdapterError("FAKE_FAILURE", "injected one-time failure", True, "download")
        return super().acquire(chunk, instrument)


def application(tmp_path, adapter, *, instance_id: str) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id=instance_id,
                worker_count=1,
            ),
            adapters={"fake": adapter},
        )
    )


def request(*, concurrency: int = 3) -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=START,
        end=END,
        storage_format=StorageFormat.CSV,
        compression="zstd",
        concurrency=concurrency,
        max_records_in_memory=10_000,
    )


def progress_events(app: MarketDataApplication, job_id: str) -> list[JobProgress]:
    return [
        JobProgress.model_validate(event.payload["progress"])
        for event in app.events_after(job_id, limit=1000)
        if event.event_type is JobEventType.PROGRESS and "progress" in event.payload
    ]


def downloading(progress: list[JobProgress]) -> list[JobProgress]:
    return [item for item in progress if item.phase is RunState.DOWNLOADING and item.determinate]


def test_backend_progress_observer_is_scoped_recursive_safe_and_non_authoritative():
    chunk = DownloadChunk(
        chunk_id="job:klines:000001",
        data_type=DataType.KLINES,
        start=START,
        end=START + timedelta(minutes=10),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
        expected_uri="fake://klines",
    )
    observed: list[IngestionProgressSnapshot] = []
    tracker = DownloadProgressTracker()

    def observer(snapshot: IngestionProgressSnapshot) -> None:
        observed.append(snapshot)
        tracker.begin_downloading()
        raise RuntimeError("observer failures must be isolated")

    with observe_progress(observer):
        tracker.set_plan((chunk,))
        tracker.begin_downloading()
        tracker.record_committed(
            chunk.chunk_id,
            restored=False,
            records=2,
            downloaded_bytes=123,
        )

    assert [item.phase for item in observed] == [
        RunState.PLANNING,
        RunState.DOWNLOADING,
        RunState.DOWNLOADING,
    ]
    assert observed[-1].completed_chunks == 1
    assert observed[-1].downloaded_records == 2
    assert observed[-1].downloaded_bytes == 123
    assert all("fake://" not in (item.message or "") for item in observed)


def test_concurrent_download_emits_ordered_measured_progress_and_transport_visibility(tmp_path):
    app = application(
        tmp_path,
        FakeExchangeAdapter(chunk_bars=2),
        instance_id="measured-concurrent",
    )
    try:
        created = app.create_download(request(concurrency=3), idempotency_key="measured-concurrent")
        completed = app.run_job(created.job.job_id)
        assert completed.state in TERMINAL_SUCCESS

        snapshots = progress_events(app, created.job.job_id)
        measured = downloading(snapshots)
        assert [item.completed_units for item in measured] == list(range(0, 7))
        assert {item.total_units for item in measured} == {6}
        assert [item.percent for item in measured] == [
            0.0,
            16.666667,
            33.333333,
            50.0,
            66.666667,
            83.333333,
            100.0,
        ]
        committed = [item for item in measured if item.current_range_start is not None]
        assert [item.current_range_start for item in committed] == [
            START + timedelta(minutes=10 * index) for index in range(6)
        ]
        assert measured[-1].counters == {
            "checkpoint_completed_chunks": 0,
            "downloaded_bytes": measured[-1].counters["downloaded_bytes"],
            "downloaded_chunks": 6,
            "downloaded_records": 12,
            "planned_chunks": 6,
            "restored_chunks": 0,
            "restored_records": 0,
        }
        assert measured[-1].counters["downloaded_bytes"] > 0
        assert completed.progress is not None
        assert completed.progress.counters["downloaded_chunks"] == 6

        token = "reader-progress-" + "r" * 32
        client = TestClient(
            create_api(
                app,
                authenticator=ApiTokenAuthenticator({token: ApiRole.READER}),
            )
        )
        response = client.get(
            f"/api/v1/jobs/{created.job.job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["progress"]["counters"]["downloaded_chunks"] == 6

        event_response = client.get(
            f"/api/v1/jobs/{created.job.job_id}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert event_response.status_code == 200
        assert any(
            event["event_type"] == "progress"
            and event["payload"]["progress"].get("percent") == 100.0
            for event in event_response.json()
        )

        cli = CliRunner().invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "jobs",
                "events",
                created.job.job_id,
            ],
        )
        assert cli.exit_code == 0, cli.output
        assert any(
            event["event_type"] == "progress"
            and event["payload"]["progress"].get("percent") == 100.0
            for event in json.loads(cli.output)
        )
    finally:
        app.shutdown()


def test_restart_uses_checkpoint_and_preserves_monotonic_counters(tmp_path):
    first = application(
        tmp_path,
        FailSecondChunkOnceAdapter(),
        instance_id="measured-before-restart",
    )
    created = first.create_download(request(concurrency=1), idempotency_key="measured-restart")
    paused = first.run_job(created.job.job_id)
    assert paused.state is RunState.PAUSED
    before = downloading(progress_events(first, created.job.job_id))
    assert before[-1].completed_units == 1
    assert before[-1].counters["downloaded_chunks"] == 1
    first.shutdown()

    recovered = application(
        tmp_path,
        FakeExchangeAdapter(chunk_bars=2),
        instance_id="measured-after-restart",
    )
    try:
        assert created.job.job_id in recovered.recover_jobs()
        completed = recovered.wait(created.job.job_id, timeout=30)
        assert completed.state in TERMINAL_SUCCESS

        all_measured = downloading(progress_events(recovered, created.job.job_id))
        assert [item.completed_units for item in all_measured] == sorted(
            item.completed_units for item in all_measured
        )
        for name in (
            "downloaded_chunks",
            "restored_chunks",
            "downloaded_records",
            "restored_records",
            "downloaded_bytes",
        ):
            values = [item.counters[name] for item in all_measured]
            assert values == sorted(values), (name, values)
        assert all_measured[-1].completed_units == 6
        assert all_measured[-1].percent == 100.0
        assert all_measured[-1].counters["checkpoint_completed_chunks"] >= 1
        assert all_measured[-1].counters["downloaded_chunks"] == 6
        assert all_measured[-1].counters["restored_chunks"] >= 1
    finally:
        recovered.shutdown()


def test_progress_persistence_failure_does_not_change_ingestion_outcome(tmp_path, monkeypatch):
    app = application(
        tmp_path,
        FakeExchangeAdapter(chunk_bars=2),
        instance_id="measured-observer-failure",
    )
    original = app.context.job_store.record_progress

    def failing_progress(job_id: str, progress: JobProgress):
        if progress.phase is RunState.DOWNLOADING:
            raise OSError("injected progress persistence failure")
        return original(job_id, progress)

    monkeypatch.setattr(app.context.job_store, "record_progress", failing_progress)
    try:
        created = app.create_download(request(), idempotency_key="progress-failure")
        completed = app.run_job(created.job.job_id)
        assert completed.state in TERMINAL_SUCCESS
        assert completed.result is not None
    finally:
        app.shutdown()


def test_tracker_rejects_invalid_and_duplicate_progress_contracts():
    with pytest.raises(ValueError):
        IngestionProgressSnapshot(
            phase=RunState.PLANNING,
            determinate=True,
            planned_chunks=1,
            completed_chunks=0,
            total_chunks=1,
        )

    chunk = DownloadChunk(
        chunk_id="job:klines:000001",
        data_type=DataType.KLINES,
        start=START,
        end=START + timedelta(minutes=10),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
        expected_uri="fake://klines",
    )
    observed: list[IngestionProgressSnapshot] = []
    tracker = DownloadProgressTracker()
    with observe_progress(observed.append):
        tracker.set_plan((chunk,))
        tracker.begin_downloading()
        tracker.record_committed(chunk.chunk_id, restored=False, records=2, downloaded_bytes=10)
        tracker.record_committed(chunk.chunk_id, restored=False, records=2, downloaded_bytes=10)
    assert len([item for item in observed if item.completed_chunks == 1]) == 1
