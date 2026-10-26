from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


pytest.importorskip("modular_strategy_trade_plan_router")


def test_wave2_routing_signal_benchmark_smoke() -> None:
    script = Path(__file__).with_name("wave2_slo_benchmark.py")
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    output = Path("reports/core-ci") / f"wave2-slo-smoke-py{version}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--profile",
            "smoke",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["profile"] == "smoke"
    assert isinstance(payload["environment"], dict)
    assert payload["semantic_verdict"]["passed"] is True

    records = payload["records"]
    assert isinstance(records, list)
    assert len(records) == 26
    assert {record["component"] for record in records} == {
        "signal_bot",
        "trade_plan_router",
    }

    for record in records:
        latency = record["latency"]
        assert latency["samples"] > 0
        assert latency["minimum_ns"] <= latency["p50_ns"]
        assert latency["p50_ns"] <= latency["p95_ns"]
        assert latency["p95_ns"] <= latency["p99_ns"]
        assert latency["p99_ns"] <= latency["maximum_ns"]
        assert record["throughput_per_second"] > 0
        assert record["ordering_violations"] == 0
        assert record["invalid_admission_success"] == 0
        assert record["event_loop_lag_ns"] is None

    signal_records = [record for record in records if record["component"] == "signal_bot"]
    assert signal_records
    assert all(record["duplicate_delivery_count"] == 0 for record in signal_records)
    assert all(record["loss_count"] == 0 for record in signal_records)

    retry = next(record for record in records if record["workload"] == "retry:10")
    assert retry["delivered"] == 10
    assert retry["queue_depth"] == 10
    assert retry["retry_state_size"] == 0
    assert retry["recovery_batch_size"] == 10

    failing = next(record for record in records if record["workload"] == "failing_consumer:10")
    assert failing["failures_by_class"] == {"TimeoutError": 10}
    assert failing["queue_depth"] == 10
    assert failing["retry_state_size"] == 10
    assert failing["delivered"] == 0

    saturation = next(record for record in records if record["workload"] == "queue_saturation:105")
    assert saturation["queue_depth"] == 105
    assert saturation["retry_state_size"] == 105
    assert saturation["recovery_batch_size"] == 100

    restart = next(record for record in records if record["workload"] == "restart:10")
    assert restart["delivered"] == 10
    assert restart["queue_depth"] == 10
    assert restart["retry_state_size"] == 0
    assert restart["restart_recovery_ns"] > 0

    soak = next(record for record in records if record["workload"] == "soak:100:duplicate_cycles=1")
    assert soak["accepted"] == 100
    assert soak["delivered"] == 100
    assert soak["duplicates_suppressed"] == 100
    assert soak["dedup_state_size"] == 100
    assert soak["persisted_bytes"] > 0

    variance = payload["variance"]
    assert isinstance(variance, list)
    assert variance
    assert all(item["sample_count"] == 1 for item in variance)
