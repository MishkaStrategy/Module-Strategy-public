from datetime import UTC, datetime, timedelta

import pytest

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    SQLiteJobLeaseStore,
    SQLiteJobStore,
    build_application_context,
)
from market_data_application.contracts import JobError, JobOperation, StorageFormat
from market_data_application.errors import JobLeaseLostError
from market_data_application.lease_store import FencedJobStore, activate_job_lease
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState


def create_job(path):
    store = SQLiteJobStore(path)
    created = store.create_job(
        operation=JobOperation.DOWNLOAD,
        request={"symbol": "BTCUSDT"},
        idempotency_key="lease-test-job",
    )
    return store, created.job.job_id


def download_request() -> CreateDownloadRequest:
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


def test_only_one_owner_can_claim_live_job(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    _, job_id = create_job(path)
    first = SQLiteJobLeaseStore(path)
    second = SQLiteJobLeaseStore(path)

    lease = first.claim(job_id, owner_id="worker-a", ttl_seconds=30)

    assert lease is not None
    assert second.claim(job_id, owner_id="worker-b", ttl_seconds=30) is None
    assert first.is_current(lease)


def test_stale_takeover_increments_generation_and_fences_old_owner(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    _, job_id = create_job(path)
    leases = SQLiteJobLeaseStore(path)
    started = datetime.now(UTC)

    first = leases.claim(job_id, owner_id="worker-a", ttl_seconds=2, at=started)
    assert first is not None
    second = leases.claim(
        job_id,
        owner_id="worker-b",
        ttl_seconds=30,
        at=started + timedelta(seconds=3),
    )

    assert second is not None
    assert second.generation == first.generation + 1
    assert not leases.is_current(first)
    assert leases.is_current(second)
    assert leases.renew(first, ttl_seconds=30) is None
    assert leases.release(first) is False


def test_heartbeat_extends_lease_and_delays_takeover(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    _, job_id = create_job(path)
    leases = SQLiteJobLeaseStore(path)
    started = datetime.now(UTC)

    first = leases.claim(job_id, owner_id="worker-a", ttl_seconds=4, at=started)
    assert first is not None
    renewed = leases.renew(first, ttl_seconds=4, at=started + timedelta(seconds=3))

    assert renewed is not None
    assert renewed.expires_at == started + timedelta(seconds=7)
    assert (
        leases.claim(
            job_id,
            owner_id="worker-b",
            ttl_seconds=30,
            at=started + timedelta(seconds=5),
        )
        is None
    )
    takeover = leases.claim(
        job_id,
        owner_id="worker-b",
        ttl_seconds=30,
        at=started + timedelta(seconds=8),
    )
    assert takeover is not None


def test_fenced_store_rejects_writes_from_stale_generation(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    raw, job_id = create_job(path)
    leases = SQLiteJobLeaseStore(path)
    fenced = FencedJobStore(raw, leases)
    started = datetime.now(UTC)

    first = leases.claim(job_id, owner_id="worker-a", ttl_seconds=2, at=started)
    assert first is not None
    with activate_job_lease(first):
        fenced.transition(job_id, RunState.VALIDATING_REQUEST)

    second = leases.claim(
        job_id,
        owner_id="worker-b",
        ttl_seconds=30,
        at=started + timedelta(seconds=3),
    )
    assert second is not None

    with activate_job_lease(first), pytest.raises(JobLeaseLostError):
        fenced.transition(job_id, RunState.DISCOVERING_CAPABILITIES)

    with activate_job_lease(second):
        updated = fenced.transition(job_id, RunState.DISCOVERING_CAPABILITIES)
    assert updated.state is RunState.DISCOVERING_CAPABILITIES


def test_terminal_job_cannot_be_claimed_and_clears_old_lease(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    raw, job_id = create_job(path)
    leases = SQLiteJobLeaseStore(path)
    lease = leases.claim(job_id, owner_id="worker-a", ttl_seconds=30)
    assert lease is not None

    raw.transition(job_id, RunState.VALIDATING_REQUEST)
    raw.transition(
        job_id,
        RunState.FAILED,
        error=JobError(
            code="TEST_FAILURE",
            message="test failure",
            retryable=False,
            stage="test",
            correlation_id="corr_test",
        ),
    )

    assert leases.claim(job_id, owner_id="worker-b", ttl_seconds=30) is None
    assert leases.get(job_id) is None


def test_application_does_not_execute_job_claimed_by_other_process(tmp_path):
    first_adapter = FakeExchangeAdapter(chunk_bars=6)
    second_adapter = FakeExchangeAdapter(chunk_bars=6)
    first = MarketDataApplication(
        build_application_context(
            ApplicationSettings(storage_root=tmp_path, instance_id="worker-a", worker_count=1),
            adapters={"fake": first_adapter},
        )
    )
    second = MarketDataApplication(
        build_application_context(
            ApplicationSettings(storage_root=tmp_path, instance_id="worker-b", worker_count=1),
            adapters={"fake": second_adapter},
        )
    )
    try:
        created = first.create_download(
            download_request(),
            idempotency_key="application-lease",
        )
        held = first.context.lease_store.claim(
            created.job.job_id,
            owner_id="external-owner",
            ttl_seconds=30,
        )
        assert held is not None

        unchanged = second.run_job(created.job.job_id)
        assert unchanged.state is RunState.CREATED
        assert second_adapter.acquired_chunks == []

        assert first.context.lease_store.release(held)
        completed = second.run_job(created.job.job_id)
        assert completed.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}
        assert second_adapter.acquired_chunks
    finally:
        first.shutdown()
        second.shutdown()
