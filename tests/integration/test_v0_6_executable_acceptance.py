from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from modular_core import (
    Candle,
    EngineConfig,
    FeatureStore,
    FeatureView,
    MarketView,
    ExecutionConfig,
    Journal,
    OrderType,
    Side,
    TimeInForce,
)
from modular_core.execution import BrokerSimulator
from modular_modules.entry_model.enums import EntryKind
from modular_modules.entry_model.plan import EntryPlan, deterministic_plan_id
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    ReasonCode as ScalingReasonCode,
    ScalingMode,
)
from modular_strategy_position_scaling.models import (
    ApprovedAdjustment,
    PositionAdjustmentPlan,
)
from modular_strategy_protection_target.models import ProtectionPlan
from modular_strategy_trade_plan_router import (
    DecisionNormalizer,
    DecisionRouter,
    DownstreamPlanEnvelope,
    PortfolioSnapshot,
    PositionState,
    RouterInputPackage,
    ScalingDecision,
    ScalingDirection,
    TradePlanRouterModule,
    TradeSide,
)
from strategy_modules.risk_sizing.decision import RiskDecision
from strategy_modules.risk_sizing.enums import RiskDecisionStatus

from paper_execution import (
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PlanAction,
    RouterExecutionAdapter,
    RouterOCCSnapshot,
    RouterPlanAdapter,
    StateVersions,
    VenueMetadataSnapshot,
)

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
PORTFOLIO_ID = "portfolio-main"


class StaticMetadataProvider:
    def __init__(self, metadata: VenueMetadataSnapshot) -> None:
        self.metadata = metadata

    def snapshot(
        self,
        instrument: str,
        timestamp: datetime,
    ) -> VenueMetadataSnapshot:
        assert instrument == self.metadata.instrument
        return self.metadata


class EnvelopeOCCProvider:
    def __init__(self, envelope: DownstreamPlanEnvelope) -> None:
        self.values = {item.kind: item.expected for item in envelope.preconditions}

    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterOCCSnapshot:
        return RouterOCCSnapshot(
            portfolio_id=portfolio_id,
            instrument=instrument,
            observed_at=timestamp,
            values=self.values,
            source="v0.6-executable-acceptance",
        )


def _metadata() -> VenueMetadataSnapshot:
    return VenueMetadataSnapshot(
        venue_id="paper-core",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="binance-usdtm-2026-01-01",
        effective_from=NOW - timedelta(days=1),
        source_hash="a" * 64,
    )


def _versions() -> StateVersions:
    return StateVersions(portfolio=3, positions=4, orders=5)


def _broker(metadata: VenueMetadataSnapshot) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )


def _released_entry_plan() -> EntryPlan:
    identity = {
        "schema_version": "2",
        "module_version": "0.2.0",
        "setup_id": "setup-v06",
        "setup_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_kind": "limit_explicit",
        "order_type": "limit",
        "created_at": NOW.isoformat(),
        "setup_confirmed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_bars": 12,
        "price": "100",
        "trigger_price": None,
        "limit_price": "100",
        "post_only": False,
        "rationale": "v0.6 executable acceptance",
        "metadata": {
            "strategy_id": "strategy-v06",
            "market": "usdt_perpetual",
        },
    }
    return EntryPlan(
        plan_id=deterministic_plan_id(identity),
        module_version="0.2.0",
        setup_id="setup-v06",
        setup_fingerprint="a" * 64,
        config_fingerprint="b" * 64,
        symbol="BTCUSDT",
        side=Side.BUY,
        entry_kind=EntryKind.LIMIT_EXPLICIT,
        order_type=OrderType.LIMIT,
        created_at=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        rationale="v0.6 executable acceptance",
        metadata={
            "strategy_id": "strategy-v06",
            "market": "usdt_perpetual",
        },
    )


def _released_protection_plan(entry: EntryPlan) -> ProtectionPlan:
    return ProtectionPlan(
        protection_plan_id="protection-v06",
        entry_plan_id=entry.plan_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        timeframe="5m",
        side=Side.BUY,
        entry_price=Decimal("100"),
        invalidation_price=Decimal("94"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        risk_distance=Decimal("5"),
        reward_distance=Decimal("10"),
        stop_distance_bps=Decimal("500"),
        reward_r=Decimal("2"),
        invalidation_mode="explicit",
        target_mode="r_multiple",
        created_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        source_entry_feature_id=entry.plan_id,
        source_invalidation_feature_id="invalidation-v06",
        source_target_feature_id="target-v06",
    )


def _released_risk_decision(
    entry: EntryPlan,
    protection: ProtectionPlan,
) -> RiskDecision:
    return RiskDecision(
        request_id="risk-v06",
        symbol=entry.symbol,
        status=RiskDecisionStatus.APPROVED,
        quantity=Decimal("10"),
        raw_quantity=Decimal("10.1"),
        risk_fraction=Decimal("0.005"),
        risk_budget=Decimal("50"),
        actual_risk_amount=Decimal("50"),
        actual_risk_fraction=Decimal("0.005"),
        notional=Decimal("1000"),
        required_leverage=Decimal("1"),
        setup_id=entry.setup_id,
        diagnostics={
            "entry_plan_id": entry.plan_id,
            "protection_plan_id": protection.protection_plan_id,
        },
    )


def _portfolio(*, positions: tuple[PositionState, ...] = ()) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        portfolio_id=PORTFOLIO_ID,
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
        positions=positions,
    )


def _normalized_open_package() -> RouterInputPackage:
    entry_plan = _released_entry_plan()
    protection_plan = _released_protection_plan(entry_plan)
    risk_decision = _released_risk_decision(entry_plan, protection_plan)
    normalizer = DecisionNormalizer()
    entry = normalizer.normalize_entry(
        entry_plan.to_feature_value(),
        feature_key="entry_model.plan",
        feature_schema_version="2",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id=entry_plan.plan_id,
        source_module="entry_model",
        symbol=entry_plan.symbol,
        portfolio_id=PORTFOLIO_ID,
    )
    protection = normalizer.normalize_protection(
        protection_plan.as_mapping(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id=protection_plan.protection_plan_id,
        symbol=entry_plan.symbol,
    )
    sizing = normalizer.normalize_sizing(
        risk_decision.to_mapping(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="risk-feature-v06",
        symbol=entry_plan.symbol,
    )
    assert entry.candidate_id == entry_plan.plan_id
    assert protection.entry_candidate_id == entry_plan.plan_id
    assert sizing.entry_candidate_id == entry_plan.plan_id
    assert sizing.protection_decision_id == protection_plan.protection_plan_id
    assert sizing.quantity == Decimal("10")
    return RouterInputPackage(
        as_of=NOW,
        portfolio=_portfolio(),
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
    )


def test_released_entry_protection_risk_chain_routes_and_executes_limit_order() -> None:
    metadata = _metadata()
    broker = _broker(metadata)
    versions = _versions()
    routed = DecisionRouter().route(_normalized_open_package())
    assert routed.plan is not None
    envelope = DownstreamPlanEnvelope.from_plan(routed.plan)
    coordinator = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="v0.6-executable-acceptance",
    )
    result = RouterExecutionAdapter(
        coordinator,
        occ_provider=EnvelopeOCCProvider(envelope),
    ).accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )

    assert result.accepted
    assert len(result.client_order_ids) == 1
    assert len(broker.orders) == 1
    evidence = tuple(coordinator.journal.store.read())[0]
    assert evidence.event_type == "router.envelope_received"
    assert evidence.payload["canonical_plan_json"] == envelope.canonical_plan_json
    assert evidence.payload["payload_sha256"] == envelope.payload_sha256
    assert coordinator.journal.verify_chain()


def _released_scaling_decision() -> tuple[PositionState, ScalingDecision]:
    position = PositionState(
        position_id="position-v06",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=Decimal("10"),
        average_entry=Decimal("100"),
        version="position-v1",
        market="usdt_perpetual",
        strategy_id="strategy-v06",
        setup_id="setup-v06",
        opened_at=NOW - timedelta(hours=1),
    )
    plan = PositionAdjustmentPlan(
        adjustment_id="adjustment-v06",
        position_id=position.position_id,
        setup_id=position.setup_id,
        symbol=position.symbol,
        side="long",
        action=AdjustmentAction.REDUCE,
        direction=AdjustmentDirection.DECREASE,
        scaling_mode=ScalingMode.SCALE_OUT,
        stage=1,
        decision_time=NOW,
        decision_index=10,
        desired_entry_kind="limit",
        desired_price=Decimal("100"),
        trigger_price=Decimal("101"),
        desired_quantity=Decimal("2"),
        maximum_quantity=Decimal("2"),
        risk_budget=Decimal("50"),
        notional_budget=Decimal("1000"),
        current_average_price=Decimal("100"),
        expected_average_price=Decimal("100"),
        current_quantity=Decimal("10"),
        expected_total_quantity=Decimal("8"),
        current_total_risk=Decimal("50"),
        projected_total_risk=Decimal("40"),
        maximum_total_risk=Decimal("50"),
        stop_price=Decimal("95"),
        invalidation_price=Decimal("94"),
        expires_at=NOW + timedelta(minutes=5),
        expires_index=11,
        reason_code=ScalingReasonCode.REDUCTION_APPROVED,
        risk_request_id=None,
        metadata={"source": "v0.6-executable-acceptance"},
        source_feature_ids=("position-source-v06",),
        config_fingerprint="c" * 64,
    )
    approved = ApprovedAdjustment(
        adjustment_id=plan.adjustment_id,
        position_id=plan.position_id,
        symbol=plan.symbol,
        side=plan.side,
        action=plan.action,
        direction=plan.direction,
        approved_quantity=Decimal("2"),
        desired_price=plan.desired_price,
        risk_request_id=None,
        reason_code=ScalingReasonCode.REDUCTION_APPROVED,
        approved_at=NOW,
        plan=plan.to_mapping(),
    )
    scaling = DecisionNormalizer().normalize_scaling(
        approved.to_mapping(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="scaling-feature-v06",
        symbol=position.symbol,
    )
    assert scaling.direction is ScalingDirection.DECREASE
    assert scaling.approved_quantity == Decimal("2")
    return position, scaling


def test_released_scaling_reduction_preserves_exact_quantity_and_reduce_only() -> None:
    metadata = _metadata()
    versions = _versions()
    position, scaling = _released_scaling_decision()
    routed = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=_portfolio(positions=(position,)),
            entries=(),
            protections=(),
            sizings=(),
            scalings=(scaling,),
        )
    )
    assert routed.plan is not None
    envelope = DownstreamPlanEnvelope.from_plan(routed.plan)
    occ_provider = EnvelopeOCCProvider(envelope)
    adaptation = RouterPlanAdapter().adapt(
        envelope,
        now=NOW,
        paper_versions=versions,
        metadata_version=metadata.metadata_version,
        paper_configuration_hash=PaperExecutionConfig().canonical_hash,
        occ_snapshot=occ_provider.snapshot(PORTFOLIO_ID, "BTCUSDT", NOW),
    )

    assert adaptation.plan.action is PlanAction.REDUCE_POSITION
    assert len(adaptation.plan.intents) == 1
    intent = adaptation.plan.intents[0]
    assert intent.quantity == Decimal("2")
    assert intent.reduce_only is True
    assert intent.close_position is False


def test_router_transient_rejection_features_remain_bounded() -> None:
    router = TradePlanRouterModule()
    store = FeatureStore()

    for index in range(64):
        candle = Candle(
            timestamp=NOW + timedelta(minutes=5 * index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        store.expire(candle.close_time)
        view = MarketView(
            base_bars=(candle,),
            timeframe_bars={},
            index=index,
            cash=10_000.0,
            equity=10_000.0,
            position=None,
            pending_orders=(),
            data_gap=False,
            features=FeatureView((), candle.close_time),
        )
        decision = router.on_bar(view)
        assert len(decision.features) == 3
        assert all(item.valid_until == candle.close_time for item in decision.features)
        for item in decision.features:
            store.publish(item)
        assert store.count == 3
