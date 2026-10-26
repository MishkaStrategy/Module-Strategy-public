from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "benchmark.py"
SPEC = importlib.util.spec_from_file_location("position_exit_benchmark", BENCHMARK_PATH)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCHMARK
SPEC.loader.exec_module(BENCHMARK)


def test_round_21_cleanup_benchmark_is_deterministic():
    first = BENCHMARK.run_once(
        3,
        mode="cleanup",
        pending_orders_count=128,
        trace_memory=False,
    )
    second = BENCHMARK.run_once(
        3,
        mode="cleanup",
        pending_orders_count=128,
        trace_memory=False,
    )
    assert first.final_state_digest == second.final_state_digest
    assert first.checkpoint_bytes == second.checkpoint_bytes
    assert first.peak_bytes == second.peak_bytes == 0


def test_round_23_active_restart_workload_is_deterministic_and_bounded():
    first = BENCHMARK.run_once(
        5_000,
        mode="active",
        restart_at=2_500,
        trace_memory=False,
    )
    second = BENCHMARK.run_once(
        5_000,
        mode="active",
        restart_at=2_500,
        trace_memory=False,
    )
    assert first.final_state_digest == second.final_state_digest
    assert first.checkpoint_bytes == second.checkpoint_bytes
    assert first.checkpoint_bytes < 2_048
    assert first.restore_seconds > 0
    assert second.restore_seconds > 0
