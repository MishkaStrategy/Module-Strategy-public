from __future__ import annotations

import argparse
import json
import random
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    PendingOrderPolicy,
    PendingOrderState,
    PlanVerdict,
    PortfolioSnapshot,
    PositionState,
    RouterConfiguration,
    RouterInputPackage,
    ScalingAction,
    ScalingDecision,
    ScalingDirection,
    TradeAction,
    TradeSide,
)
from modular_strategy_trade_plan_router.canonical import canonical_json

UTC = timezone.utc
AS_OF = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)


def _position(case_id: int, quantity: Decimal) -> PositionState:
    return PositionState(
        position_id=f"position-{case_id}",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=quantity,
        average_entry=Decimal("100"),
        version=f"position-version-{case_id}",
        market="usdt_perpetual",
        strategy_id="strategy-a",
        setup_id="setup-a",
        opened_at=AS_OF - timedelta(hours=1),
    )


def _pending_order(
    case_id: int,
    *,
    mode: str,
) -> PendingOrderState | None:
    if mode == "none":
        return None
    expires_at = AS_OF - timedelta(seconds=1) if mode == "expired" else AS_OF + timedelta(minutes=5)
    return PendingOrderState(
        order_intent_id=f"pending-{case_id}",
        plan_id=f"old-plan-{case_id}",
        symbol="BTCUSDT",
        side=TradeSide.SHORT,
        order_type="limit",
        quantity=Decimal("1"),
        status="pending",
        version=f"order-version-{case_id}",
        created_at=AS_OF - timedelta(minutes=10),
        price=Decimal("102"),
        reduce_only=True,
        setup_id="setup-a",
        expires_at=expires_at,
    )


def _package(
    case_id: int,
    rng: random.Random,
) -> tuple[RouterInputPackage, RouterConfiguration]:
    position_quantity = Decimal(rng.randint(1, 20))
    approved_quantity = Decimal(rng.randint(1, 24))
    order_type = rng.choice(("market", "limit", "stop_market", "stop_limit"))
    trigger_price = (
        Decimal("100.5") if order_type.startswith("stop_") or rng.random() < 0.35 else None
    )
    created_offset = rng.choice((0, 1, 30, 300, 86_399, 86_401))
    created_at = AS_OF - timedelta(seconds=created_offset)
    expires_at = rng.choice(
        (
            AS_OF + timedelta(minutes=5),
            AS_OF + timedelta(seconds=1),
            AS_OF - timedelta(seconds=1),
        )
    )
    if expires_at <= created_at:
        created_at = expires_at - timedelta(seconds=1)
    scaling = ScalingDecision(
        decision_id=f"scale-{case_id}",
        position_id=f"position-{case_id}",
        setup_id="setup-a",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        action=rng.choice((ScalingAction.REDUCE, ScalingAction.REBALANCE)),
        direction=ScalingDirection.DECREASE,
        approved_quantity=approved_quantity,
        desired_price=Decimal("101"),
        order_type=order_type,
        trigger_price=trigger_price,
        created_at=created_at,
        expires_at=expires_at,
    )
    snapshot_age = rng.choice((0, 1, 299, 300, 301))
    pending_mode = rng.choice(("none", "expired", "active"))
    pending = _pending_order(case_id, mode=pending_mode)
    locked = bool(rng.getrandbits(1))
    allow_locked = bool(rng.getrandbits(1))
    pending_policy = rng.choice((PendingOrderPolicy.REJECT, PendingOrderPolicy.REPLACE_SAME_SETUP))
    portfolio = PortfolioSnapshot(
        portfolio_id="portfolio-a",
        version=f"portfolio-version-{case_id}",
        timestamp=AS_OF - timedelta(seconds=snapshot_age),
        equity=Decimal("10000"),
        available_balance=Decimal("9000"),
        used_margin=Decimal("1000"),
        free_margin=Decimal("9000"),
        risk_already_reserved=Decimal("0"),
        available_risk_budget=Decimal("0"),
        position_state_version=f"position-state-{case_id}",
        order_state_version=f"order-state-{case_id}",
        risk_reservation_version=f"risk-state-{case_id}",
        positions=(_position(case_id, position_quantity),),
        pending_orders=(() if pending is None else (pending,)),
        portfolio_locked=locked,
    )
    package = RouterInputPackage(
        as_of=AS_OF,
        portfolio=portfolio,
        entries=(),
        protections=(),
        sizings=(),
        scalings=(scaling,),
    )
    config = RouterConfiguration(
        allow_risk_reducing_when_locked=allow_locked,
        pending_order_policy=pending_policy,
    )
    return package, config


def _assert_result_invariants(
    package: RouterInputPackage,
    first: Any,
    second: Any,
) -> None:
    if canonical_json(first.to_mapping()) != canonical_json(second.to_mapping()):
        raise AssertionError("identical package produced different serialized result")
    if first.result_id != second.result_id:
        raise AssertionError("identical package produced different result_id")

    if first.verdict is PlanVerdict.ACCEPTED:
        plan = first.plan
        if plan is None:
            raise AssertionError("accepted result has no plan")
        scaling = package.scalings[0]
        position = package.portfolio.positions[0]
        if plan.selected_candidate_id != scaling.decision_id:
            raise AssertionError("standalone reduction selected non-scaling candidate")
        if plan.entry_intent.reduce_only is not True:
            raise AssertionError("standalone reduction is not reduce_only")
        if plan.protection_intent is not None:
            raise AssertionError("standalone reduction invented protection")
        if plan.sizing_intent.risk_amount != Decimal("0"):
            raise AssertionError("standalone reduction reserved positive risk")
        if plan.position_intent is None:
            raise AssertionError("standalone reduction has no position intent")
        if plan.position_intent.position_id != position.position_id:
            raise AssertionError("standalone reduction targets wrong position")
        if plan.entry_intent.quantity > position.quantity:
            raise AssertionError("accepted reduction exceeds factual position")
        expected_action = (
            TradeAction.CLOSE_POSITION
            if plan.entry_intent.quantity == position.quantity
            else TradeAction.REDUCE_POSITION
        )
        if plan.action is not expected_action:
            raise AssertionError("reduce/close action does not match quantity")
    else:
        if first.plan is not None:
            raise AssertionError("rejected result unexpectedly carries a plan")
        if not first.reason_codes:
            raise AssertionError("rejected result has no reason code")


def run(seed: int, cases: int) -> dict[str, Any]:
    rng = random.Random(seed)
    verdicts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    started = time.perf_counter_ns()
    for case_id in range(cases):
        package, config = _package(case_id, rng)
        router = DecisionRouter(config)
        first = router.route(package)
        second = router.route(package)
        _assert_result_invariants(package, first, second)
        verdicts[first.verdict.value] += 1
        reasons.update(reason.value for reason in first.reason_codes)
    elapsed_ns = time.perf_counter_ns() - started
    return {
        "seed": seed,
        "cases": cases,
        "elapsed_ms": round(elapsed_ns / 1_000_000, 3),
        "cases_per_second": round(cases / (elapsed_ns / 1_000_000_000), 3),
        "verdicts": dict(sorted(verdicts.items())),
        "reason_codes": dict(sorted(reasons.items())),
        "status": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=24023)
    parser.add_argument("--cases", type=int, default=20_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.seed, args.cases)
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
