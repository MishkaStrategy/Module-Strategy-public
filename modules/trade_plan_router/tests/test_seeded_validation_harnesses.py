from __future__ import annotations

from pathlib import Path
import sys

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARK_DIR))

import benchmark_router  # noqa: E402
import fuzz_decimal_math  # noqa: E402
import fuzz_risk_reduction  # noqa: E402
import fuzz_serialization  # noqa: E402


def test_decimal_math_seeded_oracle_is_context_independent() -> None:
    report = fuzz_decimal_math.run(cases=200, seed=28101)
    assert report["status"] == "PASS"
    assert report["operations"] == 200 * len(fuzz_decimal_math.CONTEXTS) * 5
    assert len(report["digest"]) == 64


def test_serialization_seeded_mutations_fail_closed_or_replay() -> None:
    report = fuzz_serialization.run(seed=28023, cases=1_000)
    assert report["status"] == "PASS"
    assert sum(report["outcomes"].values()) == 1_000
    assert report["outcomes"].get("typed_rejection", 0) > 0


def test_risk_reduction_seeded_matrix_preserves_invariants() -> None:
    report = fuzz_risk_reduction.run(seed=24023, cases=500)
    assert report["status"] == "PASS"
    assert sum(report["verdicts"].values()) == 500
    assert report["verdicts"].get("accepted", 0) > 0
    assert report["verdicts"].get("rejected", 0) > 0


def test_benchmark_fixture_replays_stable_plan_identity() -> None:
    router = benchmark_router.DecisionRouter()
    report = benchmark_router.benchmark_case(
        router,
        benchmark_router.package(10),
        repeats=3,
    )
    assert report["route"]["min_ms"] >= 0
    assert report["serialized_plan_bytes"] > 0
    assert report["plan_id"].startswith("trade-plan-")
    assert len(report["content_hash"]) == 64
