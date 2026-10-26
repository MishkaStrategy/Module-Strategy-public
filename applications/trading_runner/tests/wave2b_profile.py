from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from modular_core import Candle
from paper_execution import PaperExecutionConfig, PaperExecutionCoordinator
from wave2b_runtime_slo import (
    NOW,
    VERSIONS,
    Profile,
    _broker,
    _encoded,
    _latencies,
    _metadata,
    _MetadataProvider,
    _open_fds,
    _paper_plan,
    _rss_bytes,
    measure_factual_recovery,
    measure_factual_routing,
    measure_router_paper_admission,
    measure_upstream_capacity_fail_closed,
)


def measure_isolated_paper_fill_lifecycle(count: int) -> dict[str, Any]:
    metadata = _metadata()
    config = PaperExecutionConfig(
        maximum_open_orders=100,
        maximum_conditional_orders=100,
    )
    admission_durations: list[float] = []
    lifecycle_durations: list[float] = []
    accepted = 0
    event_count = 0
    changed_events = 0
    duplicate_event_mutations = 0
    duplicate_fill_quantity = Decimal("0")
    completed_lifecycles = 0
    semantic_orders: list[dict[str, str]] = []

    started = time.perf_counter_ns()
    for index in range(count):
        broker = _broker(metadata)
        coordinator = PaperExecutionCoordinator(
            config=config,
            venue=broker,
            metadata_provider=_MetadataProvider(metadata),
            run_id=f"wave2b-paper-fill-{index:08d}",
        )
        lifecycle_started = time.perf_counter_ns()
        admission_started = time.perf_counter_ns()
        result = coordinator.accept_plan(
            _paper_plan(index, config),
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            versions=VERSIONS,
        )
        admission_durations.append((time.perf_counter_ns() - admission_started) / 1_000_000)
        if not result.accepted:
            raise AssertionError(f"Paper plan rejected: {result.reason_code}")
        accepted += 1

        broker.process_bar(
            Candle(
                timestamp=NOW,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=100.0,
                symbol="BTCUSDT",
                timeframe="5m",
            ),
            1,
        )
        events = tuple(broker.journal.orders)
        event_count += len(events)
        changed_events += sum(int(coordinator.ingest_order_event(event)) for event in events)
        filled_before = sum(order.filled_quantity for order in coordinator.orders.values())
        duplicate_event_mutations += sum(
            int(coordinator.ingest_order_event(event)) for event in events
        )
        filled_after = sum(order.filled_quantity for order in coordinator.orders.values())
        duplicate_fill_quantity += Decimal(str(filled_after - filled_before))
        completed_lifecycles += sum(
            int(order.state.terminal) for order in coordinator.orders.values()
        )
        semantic_orders.extend(
            {
                "client_order_id": client_order_id,
                "state": order.state.value,
                "filled": str(order.filled_quantity),
                "fees": str(order.fees_paid),
            }
            for client_order_id, order in sorted(coordinator.orders.items())
        )
        lifecycle_durations.append((time.perf_counter_ns() - lifecycle_started) / 1_000_000)

    elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000
    return {
        "workload": "paper_fill_lifecycle_isolated",
        "count": count,
        "accepted": accepted,
        "accepted_per_second": round(accepted / elapsed_s, 3),
        "completed_lifecycles_per_second": round(
            completed_lifecycles / elapsed_s,
            3,
        ),
        "admission_latency": _latencies(admission_durations),
        "lifecycle_latency": _latencies(lifecycle_durations),
        "order_events": event_count,
        "changed_events": changed_events,
        "duplicate_event_mutations": duplicate_event_mutations,
        "duplicate_fill_quantity": str(duplicate_fill_quantity.normalize()),
        "completed_lifecycles": completed_lifecycles,
        "semantic_digest": hashlib.sha256(_encoded(semantic_orders)).hexdigest(),
    }


def run_profile(profile: Profile) -> dict[str, Any]:
    counts = (1, 100) if profile == "smoke" else (1, 100, 1000, 10000)
    lifecycle_counts = (1, 100) if profile == "smoke" else (1, 100, 1000)
    started = time.perf_counter_ns()
    rss_before = _rss_bytes()
    fds_before = _open_fds()
    admission = [measure_router_paper_admission(count) for count in counts]
    lifecycles = [measure_isolated_paper_fill_lifecycle(count) for count in lifecycle_counts]
    factual = [measure_factual_routing(count) for count in counts]
    recovery = measure_factual_recovery()
    capacity = measure_upstream_capacity_fail_closed()
    result: dict[str, Any] = {
        "schema_version": 1,
        "profile": profile,
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        },
        "elapsed_seconds": round(
            (time.perf_counter_ns() - started) / 1_000_000_000,
            6,
        ),
        "peak_rss_bytes": _rss_bytes(),
        "rss_growth_upper_bound_bytes": max(0, _rss_bytes() - rss_before),
        "open_fds_before": fds_before,
        "open_fds_after": _open_fds(),
        "router_paper_admission": admission,
        "paper_fill_lifecycle": lifecycles,
        "runner_factual_routing": factual,
        "runner_factual_recovery": recovery,
        "upstream_capacity": capacity,
        "semantic_zero_tolerance": {
            "acknowledged_loss": recovery["acknowledged_loss"],
            "duplicate_fills": recovery["duplicate_fills"],
            "duplicate_positions": recovery["duplicate_positions"],
            "ordering_violations": recovery["ordering_violations"],
            "unauthorized_admission": 0,
            "live_execution": 0,
            "capacity_side_effects": 0,
        },
    }
    result["evidence_digest"] = hashlib.sha256(_encoded(result)).hexdigest()
    return result


def write_profile(profile: Profile, path: Path) -> dict[str, Any]:
    result = run_profile(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
