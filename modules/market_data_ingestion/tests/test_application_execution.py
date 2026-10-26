import sqlite3
from datetime import UTC, datetime

import pytest

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.contracts import StorageFormat
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.state_machine import (
    IngestionStateMachine,
    observe_transitions,
)


def download_request(symbol: str = "BTCUSDT") -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol=symbol,
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 2, tzinfo=UTC),
        storage_format=StorageFormat.CSV,
        compression="zstd",
        concurrency=1,
    )


def application(tmp_path) -> MarketDataApplication:
    settings = ApplicationSettings(storage_root=tmp_path, worker_count=1)
    context = build_application_context(
        settings,
        adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
    )
    return MarketDataApplication(context)


def inject_one_shot_transition_failure(app, monkeypatch, injected):
    original_transition = app.context.job_store.transition
    attempted_targets: list[RunState] = []
    fail_next = True

    def flaky_transition(job_id, target, **kwargs):
        nonlocal fail_next
        attempted_targets.append(target)
        if fail_next:
            fail_next = False
            raise injected
        return original_transition(job_id, target, **kwargs)

    monkeypatch.setattr(app.context.job_store, "transition", flaky_transition)
    return attempted_targets


def test_transition_observer_is_scoped_and_non_authoritative():
    observed: list[tuple[RunState, RunState]] = []
    machine = IngestionStateMachine()
    with observe_transitions(lambda previous, target, _at: observed.append((previous, target))):
        machine.transition(RunState.VALIDATING_REQUEST)
    machine.transition(RunState.DISCOVERING_CAPABILITIES)
    assert observed == [(RunState.CREATED, RunState.VALIDATING_REQUEST)]

    def broken_observer(_previous, _target, _at):
        raise RuntimeError("observer failure must be isolated")

    with observe_transitions(broken_observer):
        machine.transition(RunState.RESOLVING_INSTRUMENT)
    assert machine.state is RunState.RESOLVING_INSTRUMENT


def test_application_executes_download_and_registers_dataset(tmp_path):
    app = application(tmp_path)
    try:
        created = app.create_download(
            download_request(),
            idempotency_key="application-success",
        )
        completed = app.run_job(created.job.job_id)
        assert completed.state in {
            RunState.COMPLETED,
            RunState.COMPLETED_WITH_WARNINGS,
        }
        assert completed.backend_run_id is not None
        assert completed.result is not None
        assert completed.result["dataset"]["dataset_id"]
        descriptor = app.context.dataset_registry.inspect(completed.result["dataset"]["dataset_id"])
        assert descriptor is not None

        events = app.events_after(completed.job_id)
        states = [event.state for event in events]
        assert states[0] is RunState.CREATED
        assert RunState.DOWNLOADING in states
        assert RunState.VALIDATING_DATA in states
        assert RunState.REGISTERING in states
        assert states[-1] is completed.state
    finally:
        app.shutdown()


def test_durable_transition_failure_surfaces_before_terminal_recording(tmp_path, monkeypatch):
    app = application(tmp_path)
    try:
        created = app.create_download(
            download_request(),
            idempotency_key="application-transition-failure",
        )
        injected = sqlite3.OperationalError(
            "database is locked: injected durable transition failure"
        )
        attempted_targets = inject_one_shot_transition_failure(app, monkeypatch, injected)

        with pytest.raises(sqlite3.OperationalError) as caught:
            app.run_job(created.job.job_id)

        assert caught.value is injected
        assert attempted_targets == [RunState.VALIDATING_REQUEST]
        stored = app.get_job(created.job.job_id)
        assert stored.state is RunState.CREATED
        assert [job.job_id for job in app.context.job_store.recoverable_jobs()] == [stored.job_id]
    finally:
        app.shutdown()


def test_durable_transition_contention_failure_is_repeatable(tmp_path, monkeypatch):
    for iteration in range(8):
        app = application(tmp_path / f"iteration-{iteration}")
        try:
            created = app.create_download(
                download_request(),
                idempotency_key=f"application-transition-contention-{iteration}",
            )
            injected = sqlite3.OperationalError(
                f"database is locked: injected contention iteration {iteration}"
            )
            attempted_targets = inject_one_shot_transition_failure(app, monkeypatch, injected)

            with pytest.raises(sqlite3.OperationalError) as caught:
                app.run_job(created.job.job_id)

            assert caught.value is injected
            assert attempted_targets == [RunState.VALIDATING_REQUEST]
            assert app.get_job(created.job.job_id).state is RunState.CREATED
        finally:
            app.shutdown()


def test_background_submission_reuses_one_job(tmp_path):
    app = application(tmp_path)
    try:
        first = app.submit_download(
            download_request(),
            idempotency_key="application-background",
        )
        second = app.submit_download(
            download_request(),
            idempotency_key="application-background",
        )
        assert second.reused is True
        assert second.job.job_id == first.job.job_id
        completed = app.wait(first.job.job_id, timeout=30)
        assert completed.state in {
            RunState.COMPLETED,
            RunState.COMPLETED_WITH_WARNINGS,
        }
        assert app.list_jobs().pagination.total == 1
    finally:
        app.shutdown()


def test_recovery_schedules_created_job(tmp_path):
    first = application(tmp_path)
    created = first.create_download(
        download_request(),
        idempotency_key="application-recovery",
    )
    first.shutdown()

    recovered = application(tmp_path)
    try:
        assert recovered.recover_jobs() == (created.job.job_id,)
        completed = recovered.wait(created.job.job_id, timeout=30)
        assert completed.state in {
            RunState.COMPLETED,
            RunState.COMPLETED_WITH_WARNINGS,
        }
    finally:
        recovered.shutdown()


def test_backend_structured_error_becomes_failed_job(tmp_path):
    app = application(tmp_path)
    try:
        created = app.create_download(
            download_request("UNKNOWNUSDT"),
            idempotency_key="application-failure",
        )
        failed = app.run_job(created.job.job_id)
        assert failed.state is RunState.FAILED
        assert failed.error is not None
        assert failed.error.code == "INSTRUMENT_NOT_FOUND"
        assert failed.error.correlation_id == failed.correlation_id
        assert failed.result is None
    finally:
        app.shutdown()
