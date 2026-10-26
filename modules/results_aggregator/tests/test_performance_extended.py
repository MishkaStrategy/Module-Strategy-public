from __future__ import annotations

import threading

import pytest

import modular_results_registry.performance as performance
from modular_results_registry.errors import ValidationError
from modular_results_registry.performance import OperationSample, OperationalMetrics, _percentile


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": ""},
        {"status": ""},
        {"duration_seconds": -1},
        {"duration_seconds": float("inf")},
        {"duration_seconds": True},
        {"row_count": -1},
        {"row_count": True},
        {"byte_count": 1.5},
    ],
)
def test_operation_sample_validation(kwargs) -> None:
    values = dict(operation="import", duration_seconds=1.0, status="PASS")
    values.update(kwargs)
    with pytest.raises(ValidationError):
        OperationSample(**values)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_metrics_sample_limit_validation(value) -> None:
    with pytest.raises(ValueError):
        OperationalMetrics(max_samples_per_operation=value)


def test_snapshot_aggregates_percentiles_order_and_hash() -> None:
    metrics = OperationalMetrics()
    metrics.record(OperationSample("z", 5.0, "PASS", 1, 10))
    for duration, status in [(1.0, "PASS"), (2.0, "FAIL"), (3.0, "PASS"), (4.0, "PASS")]:
        metrics.record(OperationSample("a", duration, status, 2, 20))
    snapshot = metrics.snapshot()
    assert [item.operation for item in snapshot.summaries] == ["a", "z"]
    first = snapshot.summaries[0]
    assert (first.samples, first.failures, first.rows, first.bytes) == (4, 1, 8, 80)
    assert (first.p50_seconds, first.p95_seconds, first.p99_seconds, first.maximum_seconds) == (
        2.0,
        4.0,
        4.0,
        4.0,
    )
    assert snapshot.sample_count == 5
    assert snapshot.snapshot_hash == metrics.snapshot().snapshot_hash


def test_rolling_limit_drops_oldest_sample() -> None:
    metrics = OperationalMetrics(max_samples_per_operation=2)
    for value in (1.0, 2.0, 3.0):
        metrics.record(OperationSample("op", value, "PASS"))
    summary = metrics.snapshot().summaries[0]
    assert summary.samples == 2
    assert summary.p50_seconds == 2.0
    assert summary.maximum_seconds == 3.0


def test_measure_records_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter((10.0, 11.5, 20.0, 23.0))
    monkeypatch.setattr(performance.time, "perf_counter", lambda: next(values))
    metrics = OperationalMetrics()
    with metrics.measure("success", row_count=3, byte_count=7):
        pass
    with pytest.raises(RuntimeError, match="boom"):
        with metrics.measure("failure"):
            raise RuntimeError("boom")
    by_name = {item.operation: item for item in metrics.snapshot().summaries}
    assert by_name["success"].failures == 0
    assert by_name["success"].maximum_seconds == 1.5
    assert by_name["failure"].failures == 1
    assert by_name["failure"].maximum_seconds == 3.0


def test_empty_snapshot_and_percentile_edges() -> None:
    snapshot = OperationalMetrics().snapshot()
    assert snapshot.summaries == ()
    assert snapshot.sample_count == 0
    assert _percentile([], 0.5) == 0.0
    assert _percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert _percentile([1.0, 2.0, 3.0], 1.0) == 3.0


def test_concurrent_recording_is_lossless_within_cap() -> None:
    metrics = OperationalMetrics(max_samples_per_operation=10_000)

    def worker(worker_id: int) -> None:
        for index in range(200):
            metrics.record(OperationSample("parallel", worker_id + index / 1000, "PASS", 1, 2))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    summary = metrics.snapshot().summaries[0]
    assert summary.samples == 1600
    assert summary.rows == 1600
    assert summary.bytes == 3200
