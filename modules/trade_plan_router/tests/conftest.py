from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import (
    EntryCandidate,
    PortfolioSnapshot,
    ProtectionDecision,
    RouterInputPackage,
    ScalingAction,
    ScalingDecision,
    SizingDecision,
    TargetIntent,
    TradeSide,
)

NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


def make_entry(
    *,
    candidate_id: str = "entry-a",
    setup_id: str = "setup-a",
    side: TradeSide = TradeSide.LONG,
    priority: int = 0,
    created_at: datetime = NOW,
    available_at: datetime | None = None,
    expires_at: datetime | None = None,
    strategy_id: str = "strategy-a",
    portfolio_id: str = "portfolio-a",
    order_type: str = "limit",
) -> EntryCandidate:
    reference = Decimal("100")
    return EntryCandidate(
        candidate_id=candidate_id,
        source_decision_id=f"feature-{candidate_id}",
        strategy_id=strategy_id,
        setup_id=setup_id,
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id=portfolio_id,
        symbol="BTCUSDT",
        market="usdt_perpetual",
        side=side,
        signal_time=(available_at or created_at),
        available_at=(available_at or created_at),
        created_at=created_at,
        expires_at=(expires_at or NOW + timedelta(minutes=10)),
        entry_kind="limit_explicit",
        order_type=order_type,
        ttl_bars=12,
        price=(reference if order_type == "limit" else None),
        limit_price=(reference if order_type == "limit" else None),
        trigger_price=(reference if order_type in {"stop_market", "stop_limit"} else None),
        priority=priority,
        confidence=Decimal("0.8"),
        metadata={"fixture": True},
    )


def make_protection(
    entry: EntryCandidate,
    *,
    decision_id: str | None = None,
    stop_loss: Decimal | None = None,
    target: Decimal | None = None,
    created_at: datetime = NOW,
) -> ProtectionDecision:
    if entry.side is TradeSide.LONG:
        stop = stop_loss or Decimal("95")
        take = target or Decimal("110")
    else:
        stop = stop_loss or Decimal("105")
        take = target or Decimal("90")
    return ProtectionDecision(
        decision_id=decision_id or f"protection-{entry.candidate_id}",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=stop,
        targets=(TargetIntent(take),),
        created_at=created_at,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=abs(Decimal("100") - stop),
        risk_reward=Decimal("2"),
    )


def make_sizing(
    entry: EntryCandidate,
    protection: ProtectionDecision,
    *,
    decision_id: str | None = None,
    approved: bool = True,
    quantity: Decimal | None = None,
    risk_amount: Decimal = Decimal("50"),
    risk_fraction: Decimal = Decimal("0.005"),
    notional: Decimal = Decimal("1000"),
    created_at: datetime = NOW,
) -> SizingDecision:
    return SizingDecision(
        decision_id=decision_id or f"sizing-{entry.candidate_id}",
        request_id=f"request-{entry.candidate_id}",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=approved,
        quantity=(
            quantity if quantity is not None else (Decimal("10") if approved else Decimal("0"))
        ),
        notional=notional,
        risk_amount=risk_amount,
        risk_fraction=risk_fraction,
        leverage=Decimal("1"),
        created_at=created_at,
        expires_at=NOW + timedelta(minutes=10),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )


def make_portfolio(**overrides: object) -> PortfolioSnapshot:
    values: dict[str, object] = {
        "portfolio_id": "portfolio-a",
        "version": "portfolio-v1",
        "timestamp": NOW,
        "equity": Decimal("10000"),
        "available_balance": Decimal("9000"),
        "used_margin": Decimal("1000"),
        "free_margin": Decimal("9000"),
        "risk_already_reserved": Decimal("0"),
        "available_risk_budget": Decimal("300"),
        "position_state_version": "positions-v1",
        "order_state_version": "orders-v1",
        "risk_reservation_version": "risk-v1",
    }
    values.update(overrides)
    return PortfolioSnapshot(**values)  # type: ignore[arg-type]


def make_scaling(
    entry: EntryCandidate,
    *,
    action: ScalingAction = ScalingAction.ADD,
    quantity: Decimal = Decimal("2"),
    position_id: str = "position-a",
) -> ScalingDecision:
    risk_request_id = f"risk-{entry.candidate_id}" if action is ScalingAction.ADD else None
    return ScalingDecision(
        decision_id=f"scale-{entry.candidate_id}",
        position_id=position_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        action=action,
        risk_request_id=risk_request_id,
        approved_quantity=quantity,
        desired_price=Decimal("100"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def make_package(
    *,
    entries: tuple[EntryCandidate, ...] | None = None,
    protections: tuple[ProtectionDecision, ...] | None = None,
    sizings: tuple[SizingDecision, ...] | None = None,
    scalings: tuple[ScalingDecision, ...] = (),
    portfolio: PortfolioSnapshot | None = None,
    as_of: datetime = NOW,
) -> RouterInputPackage:
    if entries is None:
        entry = make_entry()
        protection = make_protection(entry)
        sizing = make_sizing(entry, protection)
        entries = (entry,)
        protections = (protection,)
        sizings = (sizing,)
    return RouterInputPackage(
        as_of=as_of,
        portfolio=portfolio or make_portfolio(),
        entries=entries,
        protections=protections or (),
        sizings=sizings or (),
        scalings=scalings,
    )


@pytest.fixture
def package() -> RouterInputPackage:
    return make_package()
