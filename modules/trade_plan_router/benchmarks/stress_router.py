from __future__ import annotations

import argparse
import json
import random
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from modular_strategy_trade_plan_router import DecisionRouter
from modular_strategy_trade_plan_router.canonical import canonical_json

from fuzz_risk_reduction import _package


def _duplicate_delivery(*, workers: int, deliveries: int) -> dict[str, Any]:
    package, config = _package(1, random.Random(25001))
    router = DecisionRouter(config)
    expected = router.route(package)
    expected_json = canonical_json(expected.to_mapping())
    started = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _index: router.route(package), range(deliveries)))
    elapsed_ns = time.perf_counter_ns() - started
    if any(result.result_id != expected.result_id for result in results):
        raise AssertionError("concurrent duplicate delivery changed result_id")
    if any(canonical_json(result.to_mapping()) != expected_json for result in results):
        raise AssertionError("concurrent duplicate delivery changed serialization")
    return {
        "workers": workers,
        "deliveries": deliveries,
        "elapsed_ms": round(elapsed_ns / 1_000_000, 3),
        "deliveries_per_second": round(deliveries / (elapsed_ns / 1_000_000_000), 3),
        "result_id": expected.result_id,
        "status": "PASS",
    }


def _unique_delivery(*, workers: int, packages: int) -> dict[str, Any]:
    rows = [_package(index, random.Random(26000 + index)) for index in range(packages)]

    def route(row: tuple[Any, Any]) -> tuple[str, str]:
        package, config = row
        result = DecisionRouter(config).route(package)
        replay = DecisionRouter(config).route(package)
        if result.result_id != replay.result_id:
            raise AssertionError("unique package replay changed result_id")
        return result.verdict.value, result.result_id

    started = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(route, rows))
    elapsed_ns = time.perf_counter_ns() - started
    if len({result_id for _verdict, result_id in results}) != packages:
        raise AssertionError("unique packages produced duplicate result identities")
    accepted = sum(verdict == "accepted" for verdict, _result_id in results)
    return {
        "workers": workers,
        "packages": packages,
        "accepted": accepted,
        "rejected": packages - accepted,
        "elapsed_ms": round(elapsed_ns / 1_000_000, 3),
        "packages_per_second": round(packages / (elapsed_ns / 1_000_000_000), 3),
        "status": "PASS",
    }


def _soak(*, iterations: int) -> dict[str, Any]:
    package, config = _package(7, random.Random(27007))
    router = DecisionRouter(config)
    expected = router.route(package).result_id
    tracemalloc.start()
    started = time.perf_counter_ns()
    for _index in range(iterations):
        if router.route(package).result_id != expected:
            raise AssertionError("soak replay changed result identity")
    elapsed_ns = time.perf_counter_ns() - started
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "iterations": iterations,
        "elapsed_ms": round(elapsed_ns / 1_000_000, 3),
        "iterations_per_second": round(iterations / (elapsed_ns / 1_000_000_000), 3),
        "retained_tracemalloc_bytes": current_bytes,
        "peak_tracemalloc_bytes": peak_bytes,
        "status": "PASS",
    }


def run() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "duplicate_delivery": _duplicate_delivery(workers=32, deliveries=5_000),
        "unique_delivery": _unique_delivery(workers=32, packages=2_000),
        "soak": _soak(iterations=20_000),
        "status": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
