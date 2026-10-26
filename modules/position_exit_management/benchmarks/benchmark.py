from __future__ import annotations

import argparse
import hashlib
import json
import tracemalloc
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from time import perf_counter

from modular_core import (
    Candle,
    MarketView,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionSnapshot,
    Side,
)

from modular_strategy_position_exit_management import PositionExitManagementModule

START = datetime(2020, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    elapsed_seconds: float
    peak_bytes: int
    checkpoint_bytes: int
    restore_seconds: float
    final_state_digest: str


def _module(mode: str, *, pending_orders: int = 0) -> PositionExitManagementModule:
    if mode == "noop":
        return PositionExitManagementModule(emit_status_feature=False)
    if mode == "active":
        return PositionExitManagementModule(
            partial_exits=[
                {"trigger_r": 1, "fraction": "0.25"},
                {"trigger_r": 2, "fraction": "0.25"},
            ],
            breakeven_trigger_r=1,
            trailing_mode="r_multiple",
            trailing_activation_r=2,
            trailing_distance_r=1,
            emit_status_feature=False,
        )
    if mode == "cleanup":
        return PositionExitManagementModule(
            max_tracked_cancels=max(1024, pending_orders),
            max_cancel_requests_per_bar=64,
        )
    if mode == "status":
        return PositionExitManagementModule(
            partial_exits=[
                {"trigger_r": 1, "fraction": "0.25"},
                {"trigger_r": 2, "fraction": "0.25"},
            ],
            breakeven_trigger_r=1,
            trailing_mode="r_multiple",
            trailing_activation_r=2,
            trailing_distance_r=1,
            emit_status_feature=True,
        )
    raise ValueError(f"unsupported benchmark mode: {mode}")


def run_once(
    events: int,
    *,
    mode: str,
    restart_at: int | None = None,
    trace_memory: bool = True,
    pending_orders_count: int = 0,
) -> WorkloadResult:
    module = _module(mode, pending_orders=pending_orders_count)
    position = PositionSnapshot(
        id="benchmark-position",
        side=PositionSide.LONG,
        quantity=4.0,
        entry_price=100.0,
        entry_time=START,
        stop_loss=95.0,
        take_profit=140.0,
        tag="benchmark",
        unrealized_pnl=0.0,
        setup_id="benchmark-setup",
    )
    pending_orders = tuple(
        OrderSnapshot(
            id=f"cleanup-{index:08d}",
            source_module="execution",
            created_at=START,
            side=Side.SELL,
            order_type=OrderType.STOP_MARKET,
            quantity=1.0,
            price=95.0,
            stop_loss=None,
            take_profit=None,
            tag="position-exit:cleanup",
            setup_id="benchmark-setup",
            client_order_id=f"cleanup-client-{index:08d}",
            status=OrderStatus.PENDING,
            remaining_quantity=1.0,
            reduce_only=True,
            trigger_price=95.0,
        )
        for index in range(pending_orders_count)
    )
    if trace_memory:
        tracemalloc.start()
    started = perf_counter()
    restore_seconds = 0.0
    for index in range(events):
        cycle = index % 20
        close = 100.0 if mode == "noop" else 100.0 + float(cycle) * 0.25
        candle = Candle(
            START + timedelta(minutes=5 * index),
            close,
            close + 1.0,
            close - 1.0,
            close,
            1.0,
            "BTCUSDT",
            "5m",
        )
        module.on_bar(
            MarketView(
                base_bars=(candle,),
                timeframe_bars={},
                index=index,
                cash=10_000.0,
                equity=10_000.0,
                position=None if mode == "cleanup" else position,
                pending_orders=pending_orders if mode == "cleanup" else (),
            )
        )
        if restart_at is not None and index == restart_at:
            checkpoint = module.snapshot_state()
            replacement = _module(mode, pending_orders=pending_orders_count)
            restore_started = perf_counter()
            replacement.restore_state(checkpoint)
            restore_seconds += perf_counter() - restore_started
            module = replacement
    elapsed = perf_counter() - started
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    else:
        peak = 0
    checkpoint_text = json.dumps(
        module.snapshot_state(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return WorkloadResult(
        elapsed_seconds=elapsed,
        peak_bytes=peak,
        checkpoint_bytes=len(checkpoint_text.encode("utf-8")),
        restore_seconds=restore_seconds,
        final_state_digest=hashlib.sha256(checkpoint_text.encode("utf-8")).hexdigest(),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--mode", choices=("noop", "active", "status", "cleanup"), default="noop")
    parser.add_argument("--pending-orders", type=int, default=4096)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--no-trace-memory", action="store_true")
    args = parser.parse_args()
    if args.events < 1 or args.runs < 1 or args.pending_orders < 0:
        raise SystemExit("events and runs must be positive; pending-orders cannot be negative")

    restart_at = args.events // 2 if args.restart else None
    results = [
        run_once(
            args.events,
            mode=args.mode,
            restart_at=restart_at,
            trace_memory=not args.no_trace_memory,
            pending_orders_count=args.pending_orders if args.mode == "cleanup" else 0,
        )
        for _ in range(args.runs)
    ]
    digests = {result.final_state_digest for result in results}
    if len(digests) != 1:
        raise RuntimeError("benchmark final state is not deterministic")
    elapsed = [result.elapsed_seconds for result in results]
    restore = [result.restore_seconds for result in results]
    output = {
        "events": args.events,
        "runs": args.runs,
        "mode": args.mode,
        "restart": args.restart,
        "trace_memory": not args.no_trace_memory,
        "pending_orders": args.pending_orders if args.mode == "cleanup" else 0,
        "median_seconds": median(elapsed),
        "minimum_seconds": min(elapsed),
        "maximum_seconds": max(elapsed),
        "median_events_per_second": args.events / median(elapsed),
        "maximum_peak_bytes": max(result.peak_bytes for result in results),
        "maximum_checkpoint_bytes": max(result.checkpoint_bytes for result in results),
        "median_restore_seconds": median(restore),
        "final_state_digest": next(iter(digests)),
    }
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
