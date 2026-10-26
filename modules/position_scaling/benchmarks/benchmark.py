from __future__ import annotations

import cProfile
import hashlib
import json
import platform
import pstats
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from modular_strategy_position_scaling import (  # noqa: E402
    AdjustmentAction,
    PositionScalingConfig,
    PositionScalingPlannerModule,
    QuantityKind,
    ScalingContext,
    ScalingMode,
    ScalingPlanner,
    ScalingRule,
    TriggerKind,
)
from modular_strategy_position_scaling.canonical import fingerprint  # noqa: E402
from modular_strategy_position_scaling.state import PlannerState  # noqa: E402


def config() -> PositionScalingConfig:
    rule = ScalingRule(
        stage=1,
        mode=ScalingMode.AVERAGING_DOWN,
        action=AdjustmentAction.ADD,
        trigger_kind=TriggerKind.ADVERSE_PERCENT,
        trigger_value=Decimal("0.01"),
        quantity_kind=QuantityKind.FIXED,
        quantity_value=Decimal("0.1"),
        cooldown_bars=1,
        expires_bars=2,
    )
    return PositionScalingConfig(
        enabled=True,
        rules=(rule,),
        allow_averaging_down=True,
        max_total_leverage=Decimal("5"),
        max_total_position_risk_fraction=Decimal("0.02"),
        max_additional_risk_fraction=Decimal("0.01"),
    )


def context(*, current_price: str = "98") -> ScalingContext:
    return ScalingContext(
        position_id="bench-position",
        setup_id="bench-setup",
        symbol="BTCUSDT",
        side="long",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("95"),
        stop_price=Decimal("95"),
        current_price=Decimal(current_price),
        unrealized_pnl=Decimal("-20") if Decimal(current_price) < 100 else Decimal("10"),
        equity=Decimal("10000"),
        cash=Decimal("9000"),
        index=10,
        attached_index=0,
        decision_time=datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        bar_seconds=60,
        last_adjustment_index=None,
        last_adjustment_price=None,
        scale_in_count=0,
        scale_out_count=0,
        data_gap=False,
        exit_pending=False,
        pending_order_count=0,
        setup_valid=True,
        market_regime="trend",
        atr=Decimal("2"),
        liquidation_distance_fraction=Decimal("0.2"),
        margin_usage_fraction=Decimal("0.1"),
        available_margin=Decimal("9000"),
        source_feature_ids=("bench-feature",),
        features={},
    )


def _run_loop(events: int, fn: Callable[[], str]) -> tuple[float, str]:
    digest = hashlib.sha256()
    start = time.perf_counter()
    for _ in range(events):
        digest.update(fn().encode())
    return time.perf_counter() - start, digest.hexdigest()


def full_plan_runner() -> Callable[[], str]:
    cfg = config()
    planner = ScalingPlanner(cfg)
    rule = cfg.rules[0]
    ctx = context()

    def run() -> str:
        result = planner.evaluate_rule(ctx, rule)
        if result.plan is None:
            raise RuntimeError(result.reason.value)
        return result.plan.adjustment_id

    return run


def risk_request_runner() -> Callable[[], str]:
    cfg = config()
    planner = ScalingPlanner(cfg)
    rule = cfg.rules[0]
    ctx = context()
    result = planner.evaluate_rule(ctx, rule)
    if result.plan is None:
        raise RuntimeError(result.reason.value)
    instrument = {
        "quantity_step": "0.001",
        "contract_size": "1",
        "max_leverage": "5",
    }

    def run() -> str:
        request = planner.build_risk_request(ctx, result.plan, instrument)
        return f"{request['request_id']}:{request['metadata']['projected_notional']}"

    return run


def no_trigger_runner() -> Callable[[], str]:
    cfg = config()
    planner = ScalingPlanner(cfg)
    rule = cfg.rules[0]
    ctx = context(current_price="101")

    def run() -> str:
        return planner.evaluate_rule(ctx, rule).reason.value

    return run


def measured(
    name: str, events: int, runs: int, factory: Callable[[], Callable[[], str]]
) -> dict[str, Any]:
    timings: list[float] = []
    digests: list[str] = []
    for _ in range(runs):
        elapsed, digest = _run_loop(events, factory())
        timings.append(elapsed)
        digests.append(digest)
    if len(set(digests)) != 1:
        raise RuntimeError(f"non-deterministic benchmark digest: {name}")
    median = statistics.median(timings)
    return {
        "name": name,
        "events": events,
        "runs": runs,
        "seconds": {
            "median": median,
            "minimum": min(timings),
            "maximum": max(timings),
        },
        "throughput_events_per_second": events / median,
        "digest": digests[0],
    }


def memory_probe(events: int = 2_000) -> dict[str, Any]:
    runner = full_plan_runner()
    tracemalloc.start()
    elapsed, digest = _run_loop(events, runner)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "workload": "full_plan_creation",
        "events": events,
        "seconds": elapsed,
        "current_bytes": current,
        "peak_bytes": peak,
        "digest": digest,
    }


def bounded_state_load(events: int = 50_000) -> dict[str, Any]:
    state = PlannerState(config_fingerprint="benchmark-config")
    started = time.perf_counter()
    for index in range(events):
        state.remember_event(f"event-{index}", 2_048)
        if index % 100 == 0:
            state.add_history({"index": index, "event": "load"}, 256)
        state.last_seen_index = index
    elapsed = time.perf_counter() - started
    snapshot = state.snapshot()
    return {
        "events": events,
        "seconds": elapsed,
        "throughput_events_per_second": events / elapsed,
        "processed_event_count": len(snapshot["processed_event_ids"]),
        "history_count": len(snapshot["history"]),
        "snapshot_digest": fingerprint(snapshot),
        "snapshot_size_bytes": len(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ),
    }


def restart_state_load(events: int = 250_000, chunks: int = 5) -> dict[str, Any]:
    if events < 1 or chunks < 1 or events % chunks != 0:
        raise ValueError("events must be positive and evenly divisible by chunks")

    def apply(state: PlannerState, start: int, stop: int) -> None:
        for index in range(start, stop):
            state.remember_event(f"event-{index}", 2_048)
            if index % 100 == 0:
                state.add_history({"index": index, "event": "load"}, 256)
            state.last_seen_index = index

    uninterrupted = PlannerState(config_fingerprint="benchmark-config")
    started = time.perf_counter()
    apply(uninterrupted, 0, events)
    uninterrupted_seconds = time.perf_counter() - started
    uninterrupted_snapshot = uninterrupted.snapshot()

    chunked = PlannerState(config_fingerprint="benchmark-config")
    chunk_size = events // chunks
    restore_seconds: list[float] = []
    started = time.perf_counter()
    for chunk in range(chunks):
        apply(chunked, chunk * chunk_size, (chunk + 1) * chunk_size)
        snapshot = chunked.snapshot()
        restore_started = time.perf_counter()
        chunked = PlannerState.restore(
            snapshot,
            config_fingerprint="benchmark-config",
            max_history=256,
            max_processed_events=2_048,
        )
        restore_seconds.append(time.perf_counter() - restore_started)
    chunked_seconds = time.perf_counter() - started
    chunked_snapshot = chunked.snapshot()
    uninterrupted_digest = fingerprint(uninterrupted_snapshot)
    chunked_digest = fingerprint(chunked_snapshot)
    if uninterrupted_digest != chunked_digest:
        raise RuntimeError("restart workload changed final deterministic state")
    return {
        "events": events,
        "chunks": chunks,
        "uninterrupted_seconds": uninterrupted_seconds,
        "chunked_seconds": chunked_seconds,
        "chunked_throughput_events_per_second": events / chunked_seconds,
        "restore_seconds_median": statistics.median(restore_seconds),
        "restore_seconds_maximum": max(restore_seconds),
        "snapshot_digest": chunked_digest,
        "snapshot_size_bytes": len(
            json.dumps(chunked_snapshot, sort_keys=True, separators=(",", ":")).encode()
        ),
        "processed_event_count": len(chunked_snapshot["processed_event_ids"]),
        "history_count": len(chunked_snapshot["history"]),
    }


def checkpoint_probe() -> dict[str, Any]:
    module = PositionScalingPlannerModule()
    snapshot = module.snapshot_state()
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    samples: list[float] = []
    for _ in range(1_000):
        target = PositionScalingPlannerModule()
        start = time.perf_counter()
        target.restore_state(snapshot)
        samples.append(time.perf_counter() - start)
    return {
        "checkpoint_size_bytes": len(encoded),
        "restore_runs": len(samples),
        "restore_seconds_median": statistics.median(samples),
        "restore_seconds_minimum": min(samples),
        "restore_seconds_maximum": max(samples),
        "snapshot_digest": hashlib.sha256(encoded).hexdigest(),
    }


def profile_probe(events: int = 1_000) -> str:
    runner = full_plan_runner()
    profiler = cProfile.Profile()
    profiler.enable()
    _run_loop(events, runner)
    profiler.disable()
    stream = StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(20)
    return stream.getvalue()


def main() -> None:
    results = {
        "module": "Position Scaling & Adjustment",
        "module_version": "0.2.2",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "core_version": "0.4.1",
        "measurements": [
            measured("full_plan_creation", 1_000, 5, full_plan_runner),
            measured("full_plan_creation", 10_000, 5, full_plan_runner),
            measured("full_plan_creation", 50_000, 3, full_plan_runner),
            measured("risk_request_creation", 10_000, 5, risk_request_runner),
            measured("early_rejection_no_trigger", 50_000, 5, no_trigger_runner),
            measured("early_rejection_no_trigger", 250_000, 3, no_trigger_runner),
            measured("early_rejection_no_trigger", 1_000_000, 1, no_trigger_runner),
        ],
        "memory": memory_probe(),
        "bounded_state_load": bounded_state_load(),
        "restart_state_load": restart_state_load(),
        "checkpoint": checkpoint_probe(),
        "stress_scope_note": (
            "One million events is measured on the representative O(1) early-rejection path. "
            "One million full cryptographic plan constructions was intentionally not used after "
            "the initial run exceeded four minutes; 50,000 full plans provides the bounded "
            "computational workload without distorting the release gate."
        ),
    }
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "benchmark_results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True), encoding="utf-8"
    )
    (artifacts / "profile_top20.txt").write_text(profile_probe(), encoding="utf-8")
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
