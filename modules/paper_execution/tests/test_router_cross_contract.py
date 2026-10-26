from __future__ import annotations

from copy import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from modular_strategy_trade_plan_router import (
    DecisionRouter,
    DownstreamPlanEnvelope,
    EntryCandidate,
    ExistingPositionPolicy,
    PortfolioSnapshot,
    PositionState,
    ProtectionDecision,
    RouterConfiguration,
    RouterInputPackage,
    ScalingAction,
    ScalingDecision,
    SizingDecision,
    TargetIntent,
    TradeSide,
)

from paper_execution import (
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperReasonCode,
    PlanAction,
    PlanValidationError,
    RouterExecutionAdapter,
    RouterOCCSnapshot,
    RouterPlanAdapter,
    StateVersions,
)

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


class MutableOCCProvider:
    def __init__(self, envelope: DownstreamPlanEnvelope) -> None:
        self.values = {item.kind: item.expected for item in envelope.preconditions}
        self.calls = 0

    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterOCCSnapshot:
        self.calls += 1
        return RouterOCCSnapshot(
            portfolio_id=portfolio_id,
            instrument=instrument,
            observed_at=timestamp,
            values=self.values,
            source="router-cross-contract-test",
        )


def _entry(
    *,
    side: TradeSide = TradeSide.LONG,
    order_type: str = "limit",
) -> EntryCandidate:
    return EntryCandidate(
        candidate_id="entry-a",
        source_decision_id="feature-entry-a",
        strategy_id="strategy-a",
        setup_id="setup-a",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-main",
        symbol="BTCUSDT",
        market="usdt_perpetual",
        side=side,
        signal_time=NOW,
        available_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_kind="router-contract",
        order_type=order_type,
        ttl_bars=12,
        price=(Decimal("100") if order_type == "limit" else None),
        limit_price=(Decimal("100") if order_type == "limit" else None),
        trigger_price=(Decimal("100") if order_type in {"stop_market", "stop_limit"} else None),
        confidence=Decimal("0.8"),
    )


def _protection(entry: EntryCandidate) -> ProtectionDecision:
    stop = Decimal("95") if entry.side is TradeSide.LONG else Decimal("105")
    target = Decimal("110") if entry.side is TradeSide.LONG else Decimal("90")
    return ProtectionDecision(
        decision_id="protection-a",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=stop,
        targets=(TargetIntent(price=target, allocation=Decimal("1")),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
    )


def _sizing(
    entry: EntryCandidate,
    protection: ProtectionDecision,
) -> SizingDecision:
    return SizingDecision(
        decision_id="sizing-a",
        request_id="request-a",
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


def _position(
    side: TradeSide = TradeSide.LONG,
    *,
    quantity: Decimal = Decimal("5"),
) -> PositionState:
    return PositionState(
        position_id="position-a",
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        average_entry=Decimal("100"),
        version="position-v1",
        market="usdt_perpetual",
        strategy_id="strategy-a",
        setup_id="setup-a",
        opened_at=NOW - timedelta(hours=1),
    )


def _portfolio(*, positions: tuple[PositionState, ...] = ()) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        portfolio_id="portfolio-main",
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


def _open_envelope(*, order_type: str = "limit") -> DownstreamPlanEnvelope:
    entry = _entry(order_type=order_type)
    protection = _protection(entry)
    sizing = _sizing(entry, protection)
    package = RouterInputPackage(
        as_of=NOW,
        portfolio=_portfolio(),
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
    )
    result = DecisionRouter().route(package)
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _open_envelope_with_lifecycle() -> DownstreamPlanEnvelope:
    entry = _entry()
    protection = ProtectionDecision(
        decision_id="protection-lifecycle",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        targets=(TargetIntent(price=Decimal("110"), allocation=Decimal("1")),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
        max_holding_seconds=1_200,
        no_progress_rule={
            "after_seconds": 600,
            "minimum_mfe_r": "0.5",
        },
    )
    sizing = _sizing(entry, protection)
    result = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=_portfolio(),
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
        )
    )
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _position_action_envelope(quantity: Decimal) -> DownstreamPlanEnvelope:
    target = _position()
    entry = _entry()
    scaling = ScalingDecision(
        decision_id=f"scaling-reduce-{quantity}",
        position_id=target.position_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        action=ScalingAction.REDUCE,
        approved_quantity=quantity,
        desired_price=Decimal("100"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    package = RouterInputPackage(
        as_of=NOW,
        portfolio=_portfolio(positions=(target,)),
        entries=(),
        protections=(),
        sizings=(),
        scalings=(scaling,),
    )
    result = DecisionRouter().route(package)
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _add_envelope() -> DownstreamPlanEnvelope:
    entry = _entry()
    protection = _protection(entry)
    sizing = _sizing(entry, protection)
    package = RouterInputPackage(
        as_of=NOW,
        portfolio=_portfolio(positions=(_position(),)),
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
    )
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD)
    ).route(package)
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _reversal_envelope() -> DownstreamPlanEnvelope:
    entry = _entry()
    protection = _protection(entry)
    sizing = _sizing(entry, protection)
    package = RouterInputPackage(
        as_of=NOW,
        portfolio=_portfolio(positions=(_position(TradeSide.SHORT),)),
        entries=(entry,),
        protections=(protection,),
        sizings=(sizing,),
    )
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.CLOSE_THEN_REVERSE)
    ).route(package)
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _coordinator(broker, metadata) -> PaperExecutionCoordinator:
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="router-cross-contract",
    )


def _adapt(
    envelope: DownstreamPlanEnvelope,
    metadata,
    versions: StateVersions,
):
    provider = MutableOCCProvider(envelope)
    snapshot = provider.snapshot(
        "portfolio-main",
        "BTCUSDT",
        NOW,
    )
    return RouterPlanAdapter().adapt(
        envelope,
        now=NOW,
        paper_versions=versions,
        metadata_version=metadata.metadata_version,
        paper_configuration_hash=PaperExecutionConfig().canonical_hash,
        occ_snapshot=snapshot,
    )


@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_canonical_open_envelope_executes_without_recalculating_router_semantics(
    order_type,
    versions,
    broker,
    metadata,
) -> None:
    envelope = _open_envelope(order_type=order_type)
    provider = MutableOCCProvider(envelope)
    coordinator = _coordinator(broker, metadata)
    adapter = RouterExecutionAdapter(coordinator, occ_provider=provider)

    result = adapter.accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )

    assert result.accepted
    event_types = tuple(event.event_type for event in coordinator.journal.store.read())
    assert event_types == (
        "router.envelope_received",
        "execution.intent_recorded",
        "order.submitted",
        "plan.accepted",
        "protection.deferred",
    )
    evidence_event = tuple(coordinator.journal.store.read())[0]
    assert evidence_event.payload["canonical_plan_json"] == envelope.canonical_plan_json
    assert evidence_event.payload["payload_sha256"] == envelope.payload_sha256
    assert tuple(
        (row["kind"], row["expected"]) for row in evidence_event.payload["preconditions"]
    ) == tuple((item.kind, item.expected) for item in envelope.preconditions)


def test_lifecycle_protection_metadata_survives_router_to_paper_entry_intent(
    versions,
    metadata,
) -> None:
    adaptation = _adapt(_open_envelope_with_lifecycle(), metadata, versions)

    entry_intent = adaptation.plan.intents[0]
    assert entry_intent.role is IntentRole.ENTRY
    assert entry_intent.metadata["router_max_holding_seconds"] == 1_200
    assert entry_intent.metadata["router_no_progress_after_seconds"] == 600
    assert entry_intent.metadata["router_no_progress_minimum_mfe_r"] == "0.5"


def test_router_occ_strings_remain_separate_from_paper_integer_versions(
    versions,
    metadata,
) -> None:
    envelope = _open_envelope()
    adaptation = _adapt(envelope, metadata, versions)

    assert adaptation.plan.expected_versions is versions
    assert all(isinstance(expected, str) for _, expected in adaptation.evidence.preconditions)
    assert tuple(adaptation.evidence.preconditions) == tuple(
        (item.kind, item.expected) for item in envelope.preconditions
    )


@pytest.mark.parametrize(
    ("quantity", "expected_action"),
    [
        (Decimal("2"), PlanAction.REDUCE_POSITION),
        (Decimal("5"), PlanAction.CLOSE_POSITION),
    ],
)
def test_reduce_and_close_translation_preserves_reduce_only_semantics(
    quantity,
    expected_action,
    versions,
    metadata,
) -> None:
    envelope = _position_action_envelope(quantity)
    adaptation = _adapt(envelope, metadata, versions)

    assert adaptation.plan.action is expected_action
    assert adaptation.plan.intents[0].reduce_only
    assert adaptation.plan.intents[0].close_position is (
        expected_action is PlanAction.CLOSE_POSITION
    )
    assert adaptation.plan.intents[0].side.value == "sell"


def test_add_translation_requires_factual_position_provider_before_execution(
    versions,
    broker,
    metadata,
) -> None:
    envelope = _add_envelope()
    adaptation = _adapt(envelope, metadata, versions)
    assert adaptation.plan.action is PlanAction.ADD_TO_POSITION
    assert tuple(intent.role for intent in adaptation.plan.intents) == (
        IntentRole.ENTRY,
        IntentRole.STOP,
        IntentRole.TARGET,
    )

    coordinator = _coordinator(broker, metadata)
    with pytest.raises(PlanValidationError) as exc:
        RouterExecutionAdapter(
            coordinator,
            occ_provider=MutableOCCProvider(envelope),
        ).accept(
            envelope,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=versions,
        )
    assert exc.value.reason_code is PaperReasonCode.INVALID_CONFIGURATION
    assert broker.orders == []


def test_reversal_is_typed_rejected_before_paper_translation() -> None:
    envelope = _reversal_envelope()
    with pytest.raises(PlanValidationError) as exc:
        RouterPlanAdapter().inspect(envelope, now=NOW)
    assert exc.value.reason_code is PaperReasonCode.UNSUPPORTED_PLAN_ACTION


def test_occ_mismatch_fails_before_router_evidence_or_venue_side_effect(
    versions,
    broker,
    metadata,
) -> None:
    envelope = _open_envelope()
    provider = MutableOCCProvider(envelope)
    provider.values["portfolio_version"] = "portfolio-v2"
    coordinator = _coordinator(broker, metadata)

    with pytest.raises(PlanValidationError) as exc:
        RouterExecutionAdapter(coordinator, occ_provider=provider).accept(
            envelope,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=versions,
        )
    assert exc.value.reason_code is PaperReasonCode.STATE_VERSION_MISMATCH
    assert coordinator.journal.store.read() == ()
    assert broker.orders == []


def test_identical_router_replay_does_not_recheck_changed_occ_or_local_versions(
    versions,
    broker,
    metadata,
) -> None:
    envelope = _open_envelope()
    provider = MutableOCCProvider(envelope)
    coordinator = _coordinator(broker, metadata)
    adapter = RouterExecutionAdapter(coordinator, occ_provider=provider)

    first = adapter.accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )
    provider.values["portfolio_version"] = "changed-after-acceptance"
    second = adapter.accept(
        envelope,
        now=NOW,
        current_index=1,
        mark_price=Decimal("101"),
        paper_versions=StateVersions(portfolio=99, positions=99, orders=99),
    )

    assert second is first
    assert provider.calls == 1
    assert len(broker.orders) == 1


def test_tampered_payload_is_rejected_before_occ_lookup(
    versions,
    broker,
    metadata,
) -> None:
    envelope = _open_envelope()
    tampered = copy(envelope)
    object.__setattr__(
        tampered,
        "canonical_plan_json",
        envelope.canonical_plan_json + " ",
    )
    provider = MutableOCCProvider(envelope)
    coordinator = _coordinator(broker, metadata)

    with pytest.raises(PlanValidationError) as exc:
        RouterExecutionAdapter(coordinator, occ_provider=provider).accept(
            tampered,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=versions,
        )
    assert exc.value.reason_code is PaperReasonCode.PLAN_HASH_MISMATCH
    assert provider.calls == 0


def test_expired_router_envelope_is_rejected_before_occ_lookup(
    versions,
    broker,
    metadata,
) -> None:
    envelope = _open_envelope()
    provider = MutableOCCProvider(envelope)
    coordinator = _coordinator(broker, metadata)

    with pytest.raises(PlanValidationError):
        RouterExecutionAdapter(coordinator, occ_provider=provider).accept(
            envelope,
            now=envelope.expires_at,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=versions,
        )
    assert provider.calls == 0
