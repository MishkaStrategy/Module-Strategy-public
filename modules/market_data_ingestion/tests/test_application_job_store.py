from datetime import UTC, datetime

import pytest

from market_data_application.contracts import (
    CreateDownloadRequest,
    JobError,
    JobOperation,
    JobProgress,
)
from market_data_application.errors import (
    IdempotencyConflictError,
    InvalidJobTransitionError,
)
from market_data_application.job_store import SQLiteJobStore
from market_data_ingestion.enums import DataType, MarketType, RunState


def request(symbol: str = "BTCUSDT") -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol=symbol,
        required_data=(DataType.KLINES,),
        optional_data=(DataType.FUNDING_RATE,),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 2, 1, tzinfo=UTC),
    )


def test_identical_idempotency_key_reuses_job(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    first = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request=request(),
        idempotency_key="req-1",
    )
    second = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request=request(),
        idempotency_key="req-1",
    )
    assert second.reused is True
    assert second.job.job_id == first.job.job_id
    assert store.events_after(first.job.job_id)[0].event_id == 1


def test_conflicting_idempotency_key_is_rejected(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    created = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request=request(),
        idempotency_key="req-1",
    )
    with pytest.raises(IdempotencyConflictError) as caught:
        store.create_job(
            operation=JobOperation.DOWNLOAD,
            request=request("ETHUSDT"),
            idempotency_key="req-1",
        )
    assert caught.value.details["job_id"] == created.job.job_id


def test_transition_progress_events_and_restart_recovery(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(path)
    created = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request=request(),
        idempotency_key="req-2",
    )
    job_id = created.job.job_id
    store.transition(job_id, RunState.VALIDATING_REQUEST)
    store.transition(job_id, RunState.DISCOVERING_CAPABILITIES)
    store.transition(job_id, RunState.RESOLVING_INSTRUMENT)
    store.transition(job_id, RunState.PLANNING)
    store.transition(job_id, RunState.DOWNLOADING, backend_run_id="mdi:abc")
    progress = JobProgress(
        phase=RunState.DOWNLOADING,
        determinate=True,
        completed_units=4,
        total_units=10,
        counters={"records": 1000, "bytes": 8192},
        current_range_start=datetime(2025, 1, 1, tzinfo=UTC),
        current_range_end=datetime(2025, 1, 2, tzinfo=UTC),
    )
    store.record_progress(job_id, progress)

    reopened = SQLiteJobStore(path)
    recovered = reopened.recoverable_jobs()
    assert [item.job_id for item in recovered] == [job_id]
    assert recovered[0].progress is not None
    assert recovered[0].progress.percent == 40.0
    events = reopened.events_after(job_id, after_event_id=3)
    assert [event.event_id for event in events] == list(range(4, len(events) + 4))


def test_invalid_transition_and_terminal_progress_are_rejected(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    created = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request=request(),
        idempotency_key="req-3",
    )
    job_id = created.job.job_id
    with pytest.raises(InvalidJobTransitionError):
        store.transition(job_id, RunState.DOWNLOADING)

    store.transition(job_id, RunState.CANCELLED)
    with pytest.raises(InvalidJobTransitionError):
        store.record_progress(job_id, JobProgress(phase=RunState.CANCELLED))


def test_failure_requires_structured_error(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    created = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request=request(),
        idempotency_key="req-4",
    )
    job_id = created.job.job_id
    store.transition(job_id, RunState.VALIDATING_REQUEST)
    with pytest.raises(ValueError, match="requires error"):
        store.transition(job_id, RunState.FAILED)
    failed = store.transition(
        job_id,
        RunState.FAILED,
        error=JobError(
            code="SOURCE_UNAVAILABLE",
            message="source is unavailable",
            retryable=True,
            stage="capability_discovery",
            correlation_id=created.job.request_hash[:16],
        ),
    )
    assert failed.completed_at is not None
    assert failed.error is not None
    assert failed.error.code == "SOURCE_UNAVAILABLE"
    assert store.recoverable_jobs() == ()


def test_list_filters_and_pagination(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
        store.create_job(
            operation=JobOperation.DOWNLOAD,
            request=request(symbol),
            idempotency_key=f"req-list-{index}",
        )
    page = store.list_jobs(limit=2, offset=1, states=(RunState.CREATED,))
    assert page.pagination.total == 3
    assert len(page.items) == 2


def test_download_contract_rejects_naive_or_invalid_ranges():
    with pytest.raises(ValueError, match="timezone-aware"):
        CreateDownloadRequest(
            exchange_id="binance",
            market_type=MarketType.SPOT,
            symbol="BTCUSDT",
            start=datetime(2025, 1, 1),
            end=datetime(2025, 2, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="start must precede end"):
        CreateDownloadRequest(
            exchange_id="binance",
            market_type=MarketType.SPOT,
            symbol="BTCUSDT",
            start=datetime(2025, 2, 1, tzinfo=UTC),
            end=datetime(2025, 1, 1, tzinfo=UTC),
        )
