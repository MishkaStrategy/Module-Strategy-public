from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import sys
import tracemalloc
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Callable

from modular_core import Candle, Feature, FeatureStore, MarketView
from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    RiskCalculator,
    RiskRequest,
    RiskSizingModule,
    TradeSide,
)

BASE_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)


def make_request(request_id: str = "benchmark") -> RiskRequest:
    return RiskRequest(
        request_id=request_id,
        symbol="BTCUSDT",
        side=TradeSide.BUY,
        entry_price=Decimal("65000"),
        stop_loss=Decimal("64675"),
        equity=Decimal("10000"),
        requested_risk_fraction=Decimal("0.005"),
        available_margin=Decimal("10000"),
        instrument=InstrumentRiskRules(
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            tick_size=Decimal("0.1"),
            max_leverage=Decimal("20"),
        ),
    )


def make_payload(index: int) -> dict[str, object]:
    return {
        "schema_version": "1",
        "request_id": f"benchmark-{index}",
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_price": "65000",
        "stop_loss": "64675",
        "equity": "10000",
        "requested_risk_fraction": "0.005",
        "available_margin": "10000",
        "instrument": {
            "quantity_step": "0.001",
            "min_quantity": "0.001",
            "min_notional": "5",
            "tick_size": "0.1",
            "max_leverage": "20",
        },
        "portfolio": {"open_positions": 0},
    }


def make_view(index: int) -> MarketView:
    close_time = BASE_TIME + timedelta(minutes=5 * (index + 1))
    candle = Candle(
        close_time - timedelta(minutes=5),
        65000,
        65100,
        64500,
        65000,
        1,
        "BTCUSDT",
        "5m",
    )
    store = FeatureStore()
    store.publish(
        Feature(
            "risk.request",
            "benchmark_producer",
            close_time,
            make_payload(index),
            symbol="BTCUSDT",
            timeframe="5m",
        )
    )
    return MarketView(
        base_bars=(candle,),
        timeframe_bars={},
        index=index,
        cash=10000,
        equity=10000,
        position=None,
        pending_orders=(),
        features=store.snapshot(close_time),
    )


def measure(operation: Callable[[], str], runs: int) -> dict[str, object]:
    durations: list[float] = []
    digests: list[str] = []
    for _ in range(runs):
        started = perf_counter()
        digests.append(operation())
        durations.append(perf_counter() - started)
        gc.collect()
    if len(set(digests)) != 1:
        raise RuntimeError("benchmark operation was not deterministic")
    return {
        "runs": runs,
        "minimum_seconds": min(durations),
        "median_seconds": statistics.median(durations),
        "maximum_seconds": max(durations),
        "digest": digests[0],
    }


def calculator_operation(events: int) -> Callable[[], str]:
    request = make_request()
    calculator = RiskCalculator()

    def run() -> str:
        decision = None
        for _ in range(events):
            decision = calculator.evaluate(request)
        assert decision is not None
        return hashlib.sha256(
            json.dumps(dict(decision.to_mapping()), sort_keys=True).encode("utf-8")
        ).hexdigest()

    return run


def module_operation(events: int) -> Callable[[], str]:
    def run() -> str:
        module = RiskSizingModule()
        last = None
        for index in range(events):
            result = module.on_bar(make_view(index))
            last = result.features[0].value
        assert last is not None
        output = {"last": dict(last), "state": module.snapshot_state()}
        return hashlib.sha256(
            json.dumps(output, sort_keys=True, default=list).encode("utf-8")
        ).hexdigest()

    return run


def add_throughput(result: dict[str, object], events: int) -> dict[str, object]:
    return {
        "events": events,
        **result,
        "throughput_per_second": events / float(result["median_seconds"]),
    }


def memory_measure(events: int) -> dict[str, object]:
    tracemalloc.start()
    started = perf_counter()
    digest = module_operation(events)()
    duration = perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "events": events,
        "seconds": duration,
        "throughput_per_second": events / duration,
        "current_bytes": current,
        "peak_bytes": peak,
        "digest": digest,
    }


def checkpoint_measure(events: int, runs: int = 5) -> dict[str, object]:
    module = RiskSizingModule()
    for index in range(events):
        module.on_bar(make_view(index))
    state = module.snapshot_state()
    encoded = json.dumps(state, sort_keys=True).encode("utf-8")
    durations: list[float] = []
    for _ in range(runs):
        restored = RiskSizingModule()
        started = perf_counter()
        restored.restore_state(state)
        durations.append(perf_counter() - started)
        if restored.snapshot_state() != state:
            raise RuntimeError("checkpoint restore changed state")
    return {
        "cached_events": min(events, module.config.max_processed_requests),
        "checkpoint_bytes": len(encoded),
        "runs": runs,
        "minimum_seconds": min(durations),
        "median_seconds": statistics.median(durations),
        "maximum_seconds": max(durations),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.smoke:
        calculator_workloads = ((1_000, 3),)
        module_workloads = ((1_000, 3),)
        memory_events = 1_000
    else:
        calculator_workloads = ((1_000, 5), (50_000, 5), (250_000, 5), (1_000_000, 1))
        module_workloads = ((1_000, 5), (50_000, 3), (250_000, 1))
        memory_events = 10_000
    report: dict[str, object] = {
        "python": sys.version,
        "platform": platform.platform(),
        "module_version": RiskSizingModule.manifest.version,
        "calculator": [
            add_throughput(measure(calculator_operation(events), runs), events)
            for events, runs in calculator_workloads
        ],
        "module": [
            add_throughput(measure(module_operation(events), runs), events)
            for events, runs in module_workloads
        ],
        "module_memory": memory_measure(memory_events),
        "checkpoint": checkpoint_measure(5_000),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
