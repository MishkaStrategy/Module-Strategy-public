from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _component_root() -> Path:
    root = Path(__file__).parents[1]
    return root if (root / "benchmarks").is_dir() else root / "module"


def _load_benchmark() -> ModuleType:
    path = _component_root() / "benchmarks" / "session_range_benchmark.py"
    spec = importlib.util.spec_from_file_location("session_range_benchmark_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_trace_runs_after_all_throughput_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = _load_benchmark()
    calls: list[tuple[int, bool]] = []

    def fake_process(count: int, *, trace_memory: bool = False) -> tuple[float, int, int, float]:
        calls.append((count, trace_memory))
        return (float(count), 123 if trace_memory else 0, count, 0.001)

    monkeypatch.setattr(benchmark, "_process", fake_process)
    results = benchmark.run_suite(((1_000, 2), (10_000, 2)), memory_events=10_000)

    assert calls == [
        (1_000, False),
        (1_000, False),
        (10_000, False),
        (10_000, False),
        (10_000, True),
    ]
    assert results[1]["peak_memory_bytes"] == 123


def test_memory_workload_must_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setattr(
        benchmark,
        "_process",
        lambda count, trace_memory=False: (float(count), 0, count, 0.001),
    )
    with pytest.raises(ValueError, match="memory_events"):
        benchmark.run_suite(((1_000, 1),), memory_events=10_000)
