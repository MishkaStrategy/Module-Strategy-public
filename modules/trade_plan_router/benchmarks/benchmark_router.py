from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    EntryCandidate,
    PendingOrderState,
    PortfolioSnapshot,
    PositionState,
    ProtectionDecision,
    RouterConfiguration,
    RouterInputPackage,
    SizingDecision,
    TargetIntent,
    TradeSide,
)
from modular_strategy_trade_plan_router.canonical import canonical_json, sha256_hex

NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("values cannot be empty")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def candidate_rows(
    count: int,
) -> tuple[
    tuple[EntryCandidate, ...],
    tuple[ProtectionDecision, ...],
    tuple[SizingDecision, ...],
]:
    entries: list[EntryCandidate] = []
    protections: list[ProtectionDecision] = []
    sizings: list[SizingDecision] = []
    for index in range(count):
        candidate_id = f"entry-{index:06d}"
        setup_id = f"setup-{index:06d}"
        entry = EntryCandidate(
            candidate_id=candidate_id,
            source_decision_id=f"feature-{candidate_id}",
            strategy_id="benchmark-strategy",
            setup_id=setup_id,
            module_id="entry_model",
            module_version="0.2.0",
            portfolio_id="benchmark-portfolio",
            symbol="BTCUSDT",
            market="usdt_perpetual",
            side=TradeSide.LONG,
            signal_time=NOW,
            available_at=NOW,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            entry_kind="limit_explicit",
            order_type="limit",
            ttl_bars=12,
            price=Decimal("100"),
            limit_price=Decimal("100"),
            priority=index,
            confidence=Decimal("0.8"),
        )
        protection = ProtectionDecision(
            decision_id=f"protection-{candidate_id}",
            entry_candidate_id=candidate_id,
            setup_id=setup_id,
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            targets=(TargetIntent(Decimal("110")),),
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            risk_distance=Decimal("5"),
            risk_reward=Decimal("2"),
        )
        sizing = SizingDecision(
            decision_id=f"sizing-{candidate_id}",
            request_id=f"request-{candidate_id}",
            setup_id=setup_id,
            symbol="BTCUSDT",
            approved=True,
            quantity=Decimal("1"),
            notional=Decimal("100"),
            risk_amount=Decimal("5"),
            risk_fraction=Decimal("0.0005"),
            leverage=Decimal("1"),
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            entry_candidate_id=candidate_id,
            protection_decision_id=protection.decision_id,
        )
        entries.append(entry)
        protections.append(protection)
        sizings.append(sizing)
    return tuple(entries), tuple(protections), tuple(sizings)


def portfolio(*, positions: int = 0, pending_orders: int = 0) -> PortfolioSnapshot:
    position_rows = tuple(
        PositionState(
            position_id=f"position-{index:06d}",
            symbol=f"ALT{index:06d}USDT",
            side=TradeSide.LONG,
            quantity=Decimal("1"),
            average_entry=Decimal("1"),
            version=f"position-v{index}",
        )
        for index in range(positions)
    )
    order_rows = tuple(
        PendingOrderState(
            order_intent_id=f"order-{index:06d}",
            plan_id=None,
            symbol=f"ALT{index:06d}USDT",
            side=TradeSide.LONG,
            order_type="limit",
            quantity=Decimal("1"),
            status="pending",
            version=f"order-v{index}",
            created_at=NOW,
            price=Decimal("1"),
        )
        for index in range(pending_orders)
    )
    return PortfolioSnapshot(
        portfolio_id="benchmark-portfolio",
        version="portfolio-v1",
        timestamp=NOW,
        equity=Decimal("1000000"),
        available_balance=Decimal("1000000"),
        used_margin=Decimal("0"),
        free_margin=Decimal("1000000"),
        risk_already_reserved=Decimal("0"),
        available_risk_budget=Decimal("100000"),
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
        positions=position_rows,
        pending_orders=order_rows,
    )


def package(count: int, *, snapshot: PortfolioSnapshot | None = None) -> RouterInputPackage:
    entries, protections, sizings = candidate_rows(count)
    return RouterInputPackage(
        as_of=NOW,
        portfolio=snapshot or portfolio(),
        entries=entries,
        protections=protections,
        sizings=sizings,
    )


def timed(call: Callable[[], Any], repeats: int) -> tuple[list[float], Any]:
    values: list[float] = []
    result: Any = None
    for _ in range(repeats):
        started = time.perf_counter_ns()
        result = call()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return values, result


def stats(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": min(values),
        "mean_ms": statistics.fmean(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }


def benchmark_case(
    router: DecisionRouter, payload: RouterInputPackage, repeats: int
) -> dict[str, Any]:
    warmup = router.route(payload)
    if warmup.plan is None:
        raise RuntimeError(f"benchmark input was rejected: {warmup.reason_codes}")
    latencies, result = timed(lambda: router.route(payload), repeats)
    if result.plan is None:
        raise RuntimeError("benchmark result unexpectedly rejected")
    if result.plan.plan_id != warmup.plan.plan_id:
        raise RuntimeError("deterministic replay changed plan_id")

    tracemalloc.start()
    measured = router.route(payload)
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if measured.plan is None:
        raise RuntimeError("memory benchmark result unexpectedly rejected")

    serialization, serialized = timed(measured.plan.to_mapping, max(3, min(repeats, 20)))
    hashing, semantic_hash = timed(
        lambda: sha256_hex(
            measured.plan.semantic_mapping(include_identifiers=False, include_audit=False)
        ),
        max(3, min(repeats, 20)),
    )
    if semantic_hash != measured.plan.content_hash:
        raise RuntimeError("content hash replay mismatch")
    serialized_json = canonical_json(serialized)
    return {
        "route": stats(latencies),
        "serialization": stats(serialization),
        "hashing": stats(hashing),
        "peak_tracemalloc_bytes": peak_bytes,
        "retained_tracemalloc_bytes": current_bytes,
        "serialized_plan_bytes": len(serialized_json.encode("utf-8")),
        "plan_id": measured.plan.plan_id,
        "content_hash": measured.plan.content_hash,
    }


def run() -> dict[str, Any]:
    router = DecisionRouter(
        RouterConfiguration(
            max_entry_candidates=2_000,
            max_decisions_per_type=4_000,
            max_portfolio_positions=10_000,
            max_pending_orders=20_000,
        )
    )
    cases: dict[str, Any] = {}
    for count, repeats in ((1, 200), (10, 100), (100, 30), (1_000, 5)):
        cases[f"candidates_{count}"] = benchmark_case(router, package(count), repeats)
    large_snapshot = portfolio(positions=5_000, pending_orders=10_000)
    cases["large_snapshot_5000_positions_10000_orders"] = benchmark_case(
        router,
        package(1, snapshot=large_snapshot),
        10,
    )
    return {
        "schema_version": "1",
        "python": sys.version,
        "platform": platform.platform(),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
