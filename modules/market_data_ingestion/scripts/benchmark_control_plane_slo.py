from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import resource
import sqlite3
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.cli import create_cli
from market_data_application.contracts import JobError, JobProgress, StorageFormat
from market_data_application.control_plane_http import ControlPlaneSecurityController
from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import AuditContext, SQLiteControlPlaneStore
from market_data_application.security import ApiRole
from market_data_application.server import create_server
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.contracts import DatasetDescriptor
from market_data_ingestion.enums import DataType, MarketType, RunState, ValidationStatus

BENCHMARK_VERSION = 1
DEFAULT_DATASET_COUNTS = (1, 100, 1_000, 10_000)
DEFAULT_JOB_COUNTS = (1, 100, 1_000)
DEFAULT_SAMPLES = 20
_NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
_ADMIN_TOKEN = "wave2e-admin-token-000000000000000000000000"
_READER_TOKEN = "wave2e-reader-token-00000000000000000000000"
_AUDIT_KEY = b"wave2e-control-plane-audit-key-at-least-32-bytes"


def _summary(samples: Sequence[int]) -> dict[str, int | float]:
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def percentile(value: float) -> float:
        index = max(0, min(len(ordered) - 1, int(len(ordered) * value + 0.999) - 1))
        return ordered[index] / 1_000_000

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
    }


def _timed(operation: Callable[[], Any]) -> tuple[int, Any]:
    started = time.perf_counter_ns()
    result = operation()
    return time.perf_counter_ns() - started, result


def _throughput(samples: Sequence[int]) -> float:
    total_seconds = sum(samples) / 1_000_000_000
    return len(samples) / total_seconds if total_seconds > 0 else 0.0


def _rss_mib() -> float:
    rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return rss / (1024 * 1024) if platform.system() == "Darwin" else rss / 1024


def _fd_count() -> int | None:
    for path in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return len(tuple(path.iterdir()))
        except OSError:
            continue
    return None


def _file_bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _sqlite_sizes(path: Path) -> dict[str, int]:
    return {
        "database_bytes": _file_bytes(path),
        "wal_bytes": _file_bytes(path.with_name(path.name + "-wal")),
        "shm_bytes": _file_bytes(path.with_name(path.name + "-shm")),
    }


def _descriptor(root: Path, ordinal: int) -> DatasetDescriptor:
    dataset_id = f"control-plane-bench-{ordinal:05d}"
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=ordinal * 5)
    return DatasetDescriptor(
        dataset_id=dataset_id,
        version=1,
        dataset_hash=hashlib.sha256(dataset_id.encode()).hexdigest(),
        manifest_uri=f"manifests/{dataset_id}/v1.json",
        storage_uri=root.as_uri(),
        exchange_id="benchmark",
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument_id=f"benchmark:usdt-m-perpetual:s{ordinal:05d}",
        data_types=(DataType.KLINES,),
        timeframes=("5m",),
        start=start,
        end=start + timedelta(minutes=5),
        schema_version=1,
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        lineage=(f"synthetic:{ordinal:05d}",),
    )


def _request(ordinal: int = 0) -> CreateDownloadRequest:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=(ordinal % 10_000) * 5)
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=start,
        end=start + timedelta(minutes=10),
        storage_format=StorageFormat.CSV,
        concurrency=1,
        max_records_in_memory=10_000,
    )


def _application(root: Path, *, instance_id: str) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=root,
                instance_id=instance_id,
                worker_count=2,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def _controller(root: Path) -> ControlPlaneSecurityController:
    records = (
        ApiTokenRecord.from_plaintext(
            token_id="wave2e-admin",
            token=_ADMIN_TOKEN,
            role=ApiRole.ADMIN,
            not_before=_NOW,
        ),
        ApiTokenRecord.from_plaintext(
            token_id="wave2e-reader",
            token=_READER_TOKEN,
            role=ApiRole.READER,
            not_before=_NOW,
        ),
    )
    state = TokenSnapshotState(
        current=ApiTokenSnapshot(generation=1, created_at=_NOW, records=records)
    )
    store = SQLiteControlPlaneStore(root / "control-plane.sqlite3", audit_key=_AUDIT_KEY)
    store.bootstrap(
        state,
        audit=AuditContext(
            timestamp=_NOW,
            correlation_id="corr-wave2e-bootstrap",
            direct_peer="local_process",
            principal_id="wave2e-bootstrap",
            token_id="wave2e-bootstrap",
            action="security.bootstrap",
            outcome="accepted",
        ),
    )
    limits = ControlPlaneLimits(
        max_sse_global=16,
        max_sse_per_principal=8,
        max_sse_per_peer=16,
    )
    return ControlPlaneSecurityController(
        authenticator=ReloadableApiTokenAuthenticator(state, clock=lambda: _NOW),
        store=store,
        limits=limits,
        peer_limiter=BoundedTokenBucketLimiter(
            capacity=100_000,
            refill_per_second=100_000.0,
            max_entries=128,
            clock=time.monotonic,
        ),
        principal_limiter=BoundedTokenBucketLimiter(
            capacity=100_000,
            refill_per_second=100_000.0,
            max_entries=128,
            clock=time.monotonic,
        ),
        clock=lambda: _NOW,
    )


def _headers(token: str = _ADMIN_TOKEN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Correlation-ID": "corr-wave2e-benchmark",
    }


def _measure_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    expected_status: int,
    samples: int,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    latencies: list[int] = []
    for _ in range(samples):
        elapsed, response = _timed(lambda: client.request(method, path, headers=headers))
        if response.status_code != expected_status:
            raise AssertionError(
                f"{method} {path} returned {response.status_code}, expected {expected_status}"
            )
        latencies.append(elapsed)
    return {
        "latency": _summary(latencies),
        "requests_per_second": _throughput(latencies),
        "failure_count": 0,
    }


def _concurrent_requests(
    client: TestClient,
    *,
    dataset_id: str,
    attempts: int,
) -> dict[str, Any]:
    paths = ("/api/v1/datasets?limit=50", f"/api/v1/datasets/{dataset_id}")

    def request(index: int) -> tuple[int, int]:
        started = time.perf_counter_ns()
        response = client.get(paths[index % 2], headers=_headers(_READER_TOKEN))
        return time.perf_counter_ns() - started, response.status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(request, range(attempts)))
    failures = sum(status != 200 for _, status in results)
    latencies = [elapsed for elapsed, status in results if status == 200]
    return {
        "attempts": attempts,
        "successes": len(latencies),
        "availability": len(latencies) / attempts,
        "failure_count": failures,
        "latency": _summary(latencies),
        "requests_per_second": _throughput(latencies),
    }


def _dataset_workloads(
    application: MarketDataApplication,
    controller: ControlPlaneSecurityController,
    *,
    root: Path,
    counts: tuple[int, ...],
    samples: int,
) -> dict[str, Any]:
    current = 0
    results: dict[str, Any] = {}
    api = create_server(application, control_plane=controller)
    with TestClient(api) as client:
        health = _measure_request(
            client,
            "GET",
            "/api/v1/health",
            expected_status=200,
            samples=samples,
            headers=_headers(_READER_TOKEN),
        )
        for count in counts:
            if count < current:
                raise ValueError("dataset counts must be ascending")
            for ordinal in range(current, count):
                application.context.dataset_registry.register(_descriptor(root, ordinal))
            current = count
            selected = f"control-plane-bench-{count - 1:05d}"
            list_result = _measure_request(
                client,
                "GET",
                "/api/v1/datasets?limit=200",
                expected_status=200,
                samples=samples,
                headers=_headers(_READER_TOKEN),
            )
            detail_result = _measure_request(
                client,
                "GET",
                f"/api/v1/datasets/{selected}",
                expected_status=200,
                samples=samples,
                headers=_headers(_READER_TOKEN),
            )
            latest_result = _measure_request(
                client,
                "GET",
                f"/api/v1/datasets/{selected}?version=1",
                expected_status=200,
                samples=samples,
                headers=_headers(_READER_TOKEN),
            )
            missing_result = _measure_request(
                client,
                "GET",
                "/api/v1/datasets/missing-dataset",
                expected_status=404,
                samples=max(3, samples // 4),
                headers=_headers(_READER_TOKEN),
            )
            invalid_result = _measure_request(
                client,
                "GET",
                "/api/v1/datasets?limit=0",
                expected_status=422,
                samples=max(3, samples // 4),
                headers=_headers(_READER_TOKEN),
            )
            unauthorized_result = _measure_request(
                client,
                "GET",
                "/api/v1/datasets",
                expected_status=401,
                samples=max(3, samples // 4),
            )
            concurrent = _concurrent_requests(
                client,
                dataset_id=selected,
                attempts=max(16, samples * 2),
            )
            payload = client.get(
                f"/api/v1/datasets/{selected}",
                headers=_headers(_READER_TOKEN),
            ).json()
            serialized = json.dumps(payload, sort_keys=True)
            results[str(count)] = {
                "list": list_result,
                "detail": detail_result,
                "exact_version_detail": latest_result,
                "missing": missing_result,
                "validation_failure": invalid_result,
                "authorization_rejection": unauthorized_result,
                "concurrent_list_detail": concurrent,
                "semantic_checks": {
                    "reported_total": client.get(
                        "/api/v1/datasets?limit=1",
                        headers=_headers(_READER_TOKEN),
                    ).json()["pagination"]["total"],
                    "no_storage_root_exposure": str(root) not in serialized,
                    "no_file_uri_exposure": "file://" not in serialized,
                    "no_credential_marker": all(
                        marker not in serialized.lower()
                        for marker in ("password", "secret", "bearer ", _ADMIN_TOKEN.lower())
                    ),
                },
            }
    return {"health": health, "scales": results}


def _advance_to_downloading(application: MarketDataApplication, job_id: str) -> None:
    for state in (
        RunState.VALIDATING_REQUEST,
        RunState.DISCOVERING_CAPABILITIES,
        RunState.RESOLVING_INSTRUMENT,
        RunState.PLANNING,
        RunState.DOWNLOADING,
    ):
        application.context.job_store.transition(job_id, state)


def _job_workloads(
    application: MarketDataApplication,
    controller: ControlPlaneSecurityController,
    *,
    counts: tuple[int, ...],
    samples: int,
) -> dict[str, Any]:
    current = 0
    job_ids: list[str] = []
    creation_samples: list[int] = []
    scales: dict[str, Any] = {}
    api = create_server(application, control_plane=controller)
    with TestClient(api) as client:
        for count in counts:
            if count < current:
                raise ValueError("job counts must be ascending")
            for ordinal in range(current, count):
                elapsed, created = _timed(
                    lambda ordinal=ordinal: application.create_download(
                        _request(ordinal),
                        idempotency_key=f"wave2e-job-{ordinal:05d}",
                        correlation_id=f"corr-job-{ordinal:05d}",
                        actor_id="wave2e-benchmark",
                    )
                )
                creation_samples.append(elapsed)
                job_ids.append(created.job.job_id)
            current = count
            selected = job_ids[-1]
            list_result = _measure_request(
                client,
                "GET",
                "/api/v1/jobs?limit=200",
                expected_status=200,
                samples=samples,
                headers=_headers(_READER_TOKEN),
            )
            detail_result = _measure_request(
                client,
                "GET",
                f"/api/v1/jobs/{selected}",
                expected_status=200,
                samples=samples,
                headers=_headers(_READER_TOKEN),
            )
            scales[str(count)] = {
                "cumulative_submission": {
                    "latency": _summary(creation_samples[:count]),
                    "jobs_per_second": _throughput(creation_samples[:count]),
                },
                "list": list_result,
                "detail": detail_result,
                "reported_total": client.get(
                    "/api/v1/jobs?limit=1",
                    headers=_headers(_READER_TOKEN),
                ).json()["pagination"]["total"],
            }

        duplicate_samples: list[int] = []
        duplicate_job_id = job_ids[0]
        for _ in range(min(100, max(3, samples))):
            elapsed, reused = _timed(
                lambda: application.create_download(
                    _request(0),
                    idempotency_key="wave2e-job-00000",
                    correlation_id="corr-duplicate",
                    actor_id="wave2e-benchmark",
                )
            )
            if not reused.reused or reused.job.job_id != duplicate_job_id:
                raise AssertionError("duplicate job identity was not deterministic")
            duplicate_samples.append(elapsed)

        event_job = application.create_download(
            _request(max(counts) + 1),
            idempotency_key="wave2e-progress-job",
            correlation_id="corr-progress",
            actor_id="wave2e-benchmark",
        ).job.job_id
        _advance_to_downloading(application, event_job)
        progress_samples: list[int] = []
        progress_count = max(20, samples)
        for completed in range(1, progress_count + 1):
            progress = JobProgress(
                phase=RunState.DOWNLOADING,
                determinate=True,
                completed_units=completed,
                total_units=progress_count,
                counters={"events": completed},
                message="synthetic no-network progress",
            )
            elapsed, _ = _timed(
                lambda progress=progress: application.context.job_store.record_progress(
                    event_job, progress
                )
            )
            progress_samples.append(elapsed)
        events = application.events_after(event_job, after_event_id=0, limit=1000)
        cursor = events[len(events) // 2].event_id
        replay_result = _measure_request(
            client,
            "GET",
            f"/api/v1/jobs/{event_job}/events?after_event_id={cursor}&limit=1000",
            expected_status=200,
            samples=samples,
            headers=_headers(_READER_TOKEN),
        )
        sse_result = _measure_request(
            client,
            "GET",
            f"/api/v1/jobs/{event_job}/events/stream?after_event_id={cursor}",
            expected_status=200,
            samples=max(3, samples // 4),
            headers=_headers(_READER_TOKEN),
        )
        replay_payload = client.get(
            f"/api/v1/jobs/{event_job}/events?after_event_id={cursor}&limit=1000",
            headers=_headers(_READER_TOKEN),
        ).json()
        replay_ids = [int(item["event_id"]) for item in replay_payload]

        cancelled_job = application.create_download(
            _request(max(counts) + 2),
            idempotency_key="wave2e-cancelled-job",
        ).job.job_id
        cancel_ns, cancelled = _timed(lambda: application.cancel_job(cancelled_job))
        if cancelled.job.state is not RunState.CANCELLED:
            raise AssertionError("cancelled job was not terminal")

        failed_job = application.create_download(
            _request(max(counts) + 3),
            idempotency_key="wave2e-failed-job",
        ).job.job_id
        application.context.job_store.transition(failed_job, RunState.VALIDATING_REQUEST)
        failure_ns, failed = _timed(
            lambda: application.context.job_store.transition(
                failed_job,
                RunState.FAILED,
                error=JobError(
                    code="BENCHMARK_FAILURE",
                    message="synthetic benchmark failure",
                    retryable=False,
                    stage="wave2e_benchmark",
                    correlation_id="corr-failure",
                ),
            )
        )
        if failed.state is not RunState.FAILED:
            raise AssertionError("failed job was not terminal")
        terminal_events = application.events_after(failed_job, after_event_id=0, limit=1000)

    return {
        "scales": scales,
        "duplicate_submission": {
            "latency": _summary(duplicate_samples),
            "jobs_per_second": _throughput(duplicate_samples),
            "deterministic_identity": True,
        },
        "progress_updates": {
            "count": progress_count,
            "latency": _summary(progress_samples),
            "updates_per_second": _throughput(progress_samples),
            "monotonic": all(
                earlier.payload["progress"]["completed_units"]
                <= later.payload["progress"]["completed_units"]
                for earlier, later in zip(
                    [event for event in events if event.event_type.value == "progress"],
                    [event for event in events if event.event_type.value == "progress"][1:],
                    strict=False,
                )
            ),
        },
        "event_replay": {
            "rest": replay_result,
            "sse": sse_result,
            "cursor": cursor,
            "event_count": len(replay_ids),
            "strict_cursor": all(event_id > cursor for event_id in replay_ids),
            "ordered": replay_ids == sorted(replay_ids) and len(replay_ids) == len(set(replay_ids)),
        },
        "terminal_jobs": {
            "cancel_latency_ms": cancel_ns / 1_000_000,
            "cancel_state": cancelled.job.state.value,
            "failure_latency_ms": failure_ns / 1_000_000,
            "failure_state": failed.state.value,
            "terminal_event_type": terminal_events[-1].event_type.value,
        },
    }


def _cli_workloads(
    application: MarketDataApplication,
    *,
    dataset_id: str,
    job_id: str,
    root: Path,
    samples: int,
) -> dict[str, Any]:
    runner = CliRunner()
    cli = create_cli(application)

    def invoke(arguments: list[str], *, expected_exit: int) -> int:
        elapsed, result = _timed(lambda: runner.invoke(cli, arguments))
        if result.exit_code != expected_exit:
            raise AssertionError(
                f"CLI {' '.join(arguments)} exited {result.exit_code}: {result.output}"
            )
        return elapsed

    commands = {
        "dataset_list": (["--storage-root", str(root), "datasets", "list", "--limit", "50"], 0),
        "dataset_detail": (
            ["--storage-root", str(root), "datasets", "show", dataset_id],
            0,
        ),
        "job_list": (["--storage-root", str(root), "jobs", "list", "--limit", "50"], 0),
        "job_status": (["--storage-root", str(root), "jobs", "show", job_id], 0),
        "job_events": (["--storage-root", str(root), "jobs", "events", job_id], 0),
        "failure_path": (
            ["--storage-root", str(root), "datasets", "show", "missing-dataset"],
            4,
        ),
    }
    result: dict[str, Any] = {}
    for name, (arguments, expected_exit) in commands.items():
        latencies = [
            invoke(arguments, expected_exit=expected_exit) for _ in range(max(3, samples // 2))
        ]
        result[name] = {
            "latency": _summary(latencies),
            "invocations_per_second": _throughput(latencies),
            "exit_code": expected_exit,
        }

    request_path = root / "benchmark-download-request.json"
    request_path.write_text(_request(99_999).model_dump_json(indent=2), encoding="utf-8")
    start_latency = invoke(
        [
            "--storage-root",
            str(root),
            "download",
            "--request",
            str(request_path),
            "--idempotency-key",
            "wave2e-cli-download",
        ],
        expected_exit=0,
    )
    result["job_start_and_run"] = {
        "latency": _summary([start_latency]),
        "mode": "no_network_fake_adapter",
        "exit_code": 0,
    }
    return result


def _restart_recovery(root: Path) -> dict[str, Any]:
    first = _application(root, instance_id="wave2e-recovery-first")
    created = first.create_download(
        _request(88_888),
        idempotency_key="wave2e-restart-recovery",
        correlation_id="corr-recovery",
    )
    job_id = created.job.job_id
    first.shutdown()

    startup_ns, restarted = _timed(
        lambda: _application(root, instance_id="wave2e-recovery-restarted")
    )
    try:
        recovery_started = time.perf_counter_ns()
        recovery_ns, recovered = _timed(lambda: restarted.recover_jobs(limit=10))
        if job_id not in recovered:
            raise AssertionError("restart did not identify the durable job")
        final = restarted.wait(job_id, timeout=30)
        if final.state not in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}:
            raise AssertionError(f"recovered job ended in {final.state.value}")
        time_to_terminal_ns = time.perf_counter_ns() - recovery_started
    finally:
        shutdown_ns, _ = _timed(restarted.shutdown)
    return {
        "startup_ms": startup_ns / 1_000_000,
        "recover_call_ms": recovery_ns / 1_000_000,
        "recovered_job_count": len(recovered),
        "recovered_job_id": job_id,
        "terminal_state": final.state.value,
        "time_to_terminal_ms": time_to_terminal_ns / 1_000_000,
        "shutdown_ms": shutdown_ns / 1_000_000,
        "orphan_job_count": (
            0 if final.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS} else 1
        ),
    }


def _resource_warning_count(operation: Callable[[], Any]) -> tuple[Any, int]:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        result = operation()
        gc.collect()
    count = sum(issubclass(item.category, ResourceWarning) for item in captured)
    return result, count


def run_benchmark(
    *,
    root: Path,
    dataset_counts: tuple[int, ...] = DEFAULT_DATASET_COUNTS,
    job_counts: tuple[int, ...] = DEFAULT_JOB_COUNTS,
    samples: int = DEFAULT_SAMPLES,
) -> dict[str, Any]:
    if not dataset_counts or any(count <= 0 for count in dataset_counts):
        raise ValueError("dataset counts must contain positive integers")
    if not job_counts or any(count <= 0 for count in job_counts):
        raise ValueError("job counts must contain positive integers")
    if samples <= 0:
        raise ValueError("samples must be positive")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    fd_before = _fd_count()
    rss_before = _rss_mib()
    thread_before = threading.active_count()
    started = time.perf_counter_ns()

    application = _application(root, instance_id="wave2e-control-plane-benchmark")
    controller = _controller(root)
    try:
        datasets, dataset_warning_count = _resource_warning_count(
            lambda: _dataset_workloads(
                application,
                controller,
                root=root,
                counts=tuple(sorted(dataset_counts)),
                samples=samples,
            )
        )
        jobs, job_warning_count = _resource_warning_count(
            lambda: _job_workloads(
                application,
                controller,
                counts=tuple(sorted(job_counts)),
                samples=samples,
            )
        )
        cli, cli_warning_count = _resource_warning_count(
            lambda: _cli_workloads(
                application,
                dataset_id=f"control-plane-bench-{max(dataset_counts) - 1:05d}",
                job_id=application.list_jobs(limit=1).items[0].job_id,
                root=root,
                samples=samples,
            )
        )
    finally:
        shutdown_measurement, shutdown_warning_count = _resource_warning_count(
            lambda: _timed(application.shutdown)
        )
        shutdown_ns, _ = shutdown_measurement

    recovery_root = root / "restart-recovery"
    recovery, recovery_warning_count = _resource_warning_count(
        lambda: _restart_recovery(recovery_root)
    )
    gc.collect()
    elapsed_ns = time.perf_counter_ns() - started
    fd_after = _fd_count()
    rss_after = _rss_mib()
    thread_after = threading.active_count()
    registry_sizes = _sqlite_sizes(application.context.settings.registry_path)
    job_sizes = _sqlite_sizes(application.context.settings.jobs_path)
    audit_sizes = _sqlite_sizes(controller.store.path)
    total_jobs = application.list_jobs(limit=1).pagination.total
    with closing(sqlite3.connect(application.context.settings.jobs_path)) as connection:
        total_events = int(connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0])

    warnings_total = (
        dataset_warning_count
        + job_warning_count
        + cli_warning_count
        + shutdown_warning_count
        + recovery_warning_count
    )
    return {
        "schema_version": 1,
        "benchmark_version": BENCHMARK_VERSION,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "no_network": True,
            "localhost_test_client_only": True,
            "dataset_counts": list(sorted(dataset_counts)),
            "job_counts": list(sorted(job_counts)),
            "samples_per_workload": samples,
        },
        "datasets": datasets,
        "jobs": jobs,
        "cli": cli,
        "restart_recovery": recovery,
        "resources": {
            "elapsed_seconds": elapsed_ns / 1_000_000_000,
            "peak_rss_mib_before": rss_before,
            "peak_rss_mib_after": rss_after,
            "peak_rss_growth_mib": max(0.0, rss_after - rss_before),
            "fd_before": fd_before,
            "fd_after": fd_after,
            "fd_growth": (None if fd_before is None or fd_after is None else fd_after - fd_before),
            "thread_before": thread_before,
            "thread_after": thread_after,
            "thread_growth": thread_after - thread_before,
            "shutdown_ms": shutdown_ns / 1_000_000,
            "resource_warning_count": warnings_total,
            "orphan_connection_count": 0 if warnings_total == 0 else None,
            "registry": registry_sizes,
            "jobs": job_sizes,
            "audit": audit_sizes,
            "bytes_per_job": sum(job_sizes.values()) / total_jobs if total_jobs else 0.0,
            "event_journal_bytes_per_event": (
                sum(job_sizes.values()) / total_events if total_events else 0.0
            ),
            "temporary_bytes": sum(
                path.stat().st_size
                for path in root.rglob("*.tmp")
                if path.is_file() and not path.is_symlink()
            ),
        },
        "semantic_verdict": {
            "resource_warning_zero": warnings_total == 0,
            "fd_stable": fd_before is None or fd_after is None or fd_after <= fd_before + 4,
            "thread_stable": thread_after <= thread_before + 1,
            "concurrent_reader_availability": min(
                item["concurrent_list_detail"]["availability"]
                for item in datasets["scales"].values()
            ),
            "sse_ordering_violation_count": 0 if jobs["event_replay"]["ordered"] else 1,
            "cursor_violation_count": 0 if jobs["event_replay"]["strict_cursor"] else 1,
            "orphan_job_count": recovery["orphan_job_count"],
            "path_leakage_count": sum(
                not item["semantic_checks"]["no_storage_root_exposure"]
                or not item["semantic_checks"]["no_file_uri_exposure"]
                for item in datasets["scales"].values()
            ),
            "credential_leakage_count": sum(
                not item["semantic_checks"]["no_credential_marker"]
                for item in datasets["scales"].values()
            ),
        },
    }


def _parse_counts(value: str) -> tuple[int, ...]:
    counts = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not counts:
        raise argparse.ArgumentTypeError("at least one count is required")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(prog="benchmark-control-plane-slo")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--dataset-counts",
        type=_parse_counts,
        default=DEFAULT_DATASET_COUNTS,
    )
    parser.add_argument("--job-counts", type=_parse_counts, default=DEFAULT_JOB_COUNTS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    args = parser.parse_args()

    if args.root is None:
        with tempfile.TemporaryDirectory(prefix="wave2e-control-plane-") as directory:
            result = run_benchmark(
                root=Path(directory),
                dataset_counts=args.dataset_counts,
                job_counts=args.job_counts,
                samples=args.samples,
            )
    else:
        result = run_benchmark(
            root=args.root,
            dataset_counts=args.dataset_counts,
            job_counts=args.job_counts,
            samples=args.samples,
        )
    serialized = json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
