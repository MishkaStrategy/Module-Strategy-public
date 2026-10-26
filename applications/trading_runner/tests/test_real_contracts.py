from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

modular_core = pytest.importorskip("modular_core")
router = pytest.importorskip("modular_strategy_trade_plan_router")

MarketView = modular_core.MarketView
ModuleDecision = modular_core.ModuleDecision
ModuleManifest = modular_core.ModuleManifest
ModuleRegistry = modular_core.ModuleRegistry
StrategyModule = modular_core.StrategyModule
DecisionRouter = router.DecisionRouter
DownstreamPlanEnvelope = router.DownstreamPlanEnvelope
EntryCandidate = router.EntryCandidate
PortfolioSnapshot = router.PortfolioSnapshot
ProtectionDecision = router.ProtectionDecision
RouterInputPackage = router.RouterInputPackage
SizingDecision = router.SizingDecision
TargetIntent = router.TargetIntent
TradeSide = router.TradeSide

NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


class _Module(StrategyModule):
    def __init__(self, name: str, manifest: ModuleManifest, calls: list[str]) -> None:
        self.name = name
        self.manifest = manifest
        self.calls = calls

    def on_start(self) -> None:
        self.calls.append(f"start:{self.name}")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def _router_envelope() -> DownstreamPlanEnvelope:
    entry = EntryCandidate(
        candidate_id="entry-a",
        source_decision_id="feature-entry-a",
        strategy_id="strategy-a",
        setup_id="setup-a",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-a",
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
        confidence=Decimal("0.8"),
    )
    protection = ProtectionDecision(
        decision_id="protection-entry-a",
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
        decision_id="sizing-entry-a",
        request_id="request-entry-a",
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
        portfolio_id="portfolio-a",
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
    result = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=portfolio,
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
        )
    )
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def test_core_registry_provides_deterministic_dependency_order() -> None:
    calls: list[str] = []
    producer = _Module(
        "producer",
        ModuleManifest(name="producer", produced_features=("feature.a",)),
        calls,
    )
    consumer = _Module(
        "consumer",
        ModuleManifest(name="consumer", required_features=("feature.a",)),
        calls,
    )
    plan = ModuleRegistry().validate_and_plan((consumer, producer), base_timeframe="5m")
    assert tuple(module.name for module in plan.modules) == ("producer", "consumer")


def test_router_envelope_is_a_real_canonical_contract() -> None:
    envelope = _router_envelope()
    envelope.validate_at(NOW)
    assert envelope.schema_version == "1"
    assert envelope.action.value == "open_position"
    assert len(envelope.content_hash) == 64
    assert len(envelope.payload_sha256) == 64
    assert envelope.canonical_plan_json
