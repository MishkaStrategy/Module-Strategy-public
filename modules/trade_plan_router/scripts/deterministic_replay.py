from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    EntryCandidate,
    PortfolioSnapshot,
    ProtectionDecision,
    RouterInputPackage,
    SizingDecision,
    TargetIntent,
    TradeSide,
)
from modular_strategy_trade_plan_router.canonical import canonical_json, sha256_hex

NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


def build_package() -> RouterInputPackage:
    entry = EntryCandidate(
        candidate_id="entry-replay",
        source_decision_id="feature-entry-replay",
        strategy_id="strategy-replay",
        setup_id="setup-replay",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-replay",
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
        priority=7,
        confidence=Decimal("0.8"),
        metadata={"fixture": "deterministic-replay"},
    )
    protection = ProtectionDecision(
        decision_id="protection-replay",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        targets=(TargetIntent(Decimal("110")),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
    )
    sizing = SizingDecision(
        decision_id="sizing-replay",
        request_id="risk-request-replay",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=True,
        quantity=Decimal("10"),
        notional=Decimal("1000"),
        risk_amount=Decimal("50"),
        risk_fraction=Decimal("0.005"),
        leverage=Decimal("1"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )
    portfolio = PortfolioSnapshot(
        portfolio_id="portfolio-replay",
        version="portfolio-v1",
        timestamp=NOW,
        equity=Decimal("10000"),
        available_balance=Decimal("9000"),
        used_margin=Decimal("1000"),
        free_margin=Decimal("9000"),
        risk_already_reserved=Decimal("0"),
        available_risk_budget=Decimal("300"),
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
    )
    return RouterInputPackage(
        as_of=NOW,
        portfolio=portfolio,
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
    )


def main() -> None:
    result = DecisionRouter().route(build_package())
    payload = result.to_mapping()
    encoded = canonical_json(payload)
    summary = {
        "digest": sha256_hex(payload),
        "result_id": result.result_id,
        "plan_id": result.plan.plan_id if result.plan is not None else None,
        "content_hash": result.plan.content_hash if result.plan is not None else None,
        "payload": json.loads(encoded),
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
