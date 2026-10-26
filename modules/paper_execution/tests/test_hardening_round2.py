from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from modular_core import OrderSnapshot, OrderStatus, OrderType, Side

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperReasonCode,
    PlanAction,
    PlanValidationError,
    ReconciliationBlocked,
    ReconciliationEngine,
    ReconciliationMode,
)
from paper_execution.state_machine import OrderAggregate


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _plan_like(
    plan,
    *,
    plan_id: str,
    config: PaperExecutionConfig,
    intents=None,
    portfolio_id=None,
    action=None,
):
    return ExecutableTradePlan.create(
        plan_id=plan_id,
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=portfolio_id or plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=action or plan.action,
        intents=tuple(intents or plan.intents),
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=config.canonical_hash,
        input_hashes=plan.input_hashes,
    )


def test_open_order_limit_is_enforced_before_second_submit(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    config = PaperExecutionConfig(maximum_open_orders=1)
    service = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="limit",
    )
    first = _plan_like(plan, plan_id="limit-1", config=config)
    second = _plan_like(plan, plan_id="limit-2", config=config)
    assert service.accept_plan(
        first,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    ).accepted
    result = service.accept_plan(
        second,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert not result.accepted
    assert result.reason_code is PaperReasonCode.ORDER_LIMIT_EXCEEDED
    assert len(broker.orders) == 1


def test_reconciliation_is_scope_aware(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    config = PaperExecutionConfig()
    service = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="scope-aware",
    )
    first = _plan_like(
        plan,
        plan_id="scope-a",
        config=config,
        portfolio_id="portfolio-a",
    )
    second = _plan_like(
        plan,
        plan_id="scope-b",
        config=config,
        portfolio_id="portfolio-b",
    )
    assert service.accept_plan(
        first,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    ).accepted
    assert service.accept_plan(
        second,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    ).accepted
    report = service.reconcile(
        portfolio_id="portfolio-a",
        instrument=plan.instrument,
    )
    assert not report.findings
    assert not report.blocked


class ExplodingVenue:
    def __init__(self, broker) -> None:
        self.broker = broker

    def submit(self, request, **kwargs):
        raise RuntimeError("simulated internal venue defect")

    def cancel(self, request, **kwargs):
        return self.broker.cancel(request, **kwargs)

    def pending_snapshots(self):
        return self.broker.pending_snapshots()

    def process_bar(self, bar, index):
        self.broker.process_bar(bar, index)


def test_unexpected_venue_error_is_not_misclassified_as_business_rejection(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    config = PaperExecutionConfig()
    service = PaperExecutionCoordinator(
        config=config,
        venue=ExplodingVenue(broker),
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="unexpected",
    )
    compatible = _plan_like(
        plan,
        plan_id="unexpected-1",
        config=config,
    )
    result = service.accept_plan(
        compatible,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert not result.accepted
    assert result.reason_code is PaperReasonCode.INTERNAL_UNEXPECTED_ERROR

    with pytest.raises(ReconciliationBlocked) as exc:
        service.accept_plan(
            _plan_like(
                plan,
                plan_id="unexpected-2",
                config=config,
            ),
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )
    assert exc.value.reason_code is PaperReasonCode.RECONCILIATION_BLOCKED


def test_cancel_cannot_cross_portfolio_scope(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    config = PaperExecutionConfig()
    service = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="cancel-scope",
    )
    source = _plan_like(
        plan,
        plan_id="source",
        config=config,
        portfolio_id="portfolio-a",
    )
    source_result = service.accept_plan(
        source,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    cancel = ExecutionIntent(
        intent_id="cancel-cross-scope",
        role=IntentRole.CANCEL,
        cancel_client_order_id=source_result.client_order_ids[0],
    )
    cancel_plan = _plan_like(
        plan,
        plan_id="cancel-cross",
        config=config,
        portfolio_id="portfolio-b",
        intents=(cancel,),
        action=PlanAction.CANCEL_ORDERS,
    )
    result = service.accept_plan(
        cancel_plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert not result.accepted
    assert result.reason_code is PaperReasonCode.CANCEL_TARGET_NOT_FOUND
    assert len(broker.pending_snapshots()) == 1


def test_action_semantics_reject_mixed_cancel_and_open(plan) -> None:
    cancel = ExecutionIntent(
        intent_id="cancel",
        role=IntentRole.CANCEL,
        cancel_client_order_id="x",
    )
    with pytest.raises(ValueError, match="cannot contain cancel"):
        _plan_like(
            plan,
            plan_id="bad-action",
            config=PaperExecutionConfig(),
            intents=(plan.intents[0], cancel),
            action=PlanAction.OPEN_POSITION,
        )


def test_reversal_is_explicitly_unsupported_until_sequence_is_implemented(
    plan,
) -> None:
    with pytest.raises(PlanValidationError) as exc:
        _plan_like(
            plan,
            plan_id="reverse",
            config=PaperExecutionConfig(),
            action=PlanAction.REVERSE_POSITION,
        )
    assert exc.value.reason_code is PaperReasonCode.UNSUPPORTED_PLAN_ACTION


def test_hash_fields_are_strict_sha256(plan, metadata) -> None:
    with pytest.raises(ValueError, match="configuration_hash"):
        replace(plan, configuration_hash="not-a-hash")
    with pytest.raises(ValueError, match="source_hash"):
        replace(metadata, source_hash="bad")
    with pytest.raises(ValueError, match="input_hashes"):
        replace(plan, input_hashes={"decision": "bad"})


def test_duplicate_venue_client_id_is_blocking(now) -> None:
    aggregate = OrderAggregate(
        "pevr-entry-0-1234567890123456",
        "ord-1",
        Decimal("1"),
        portfolio_id="p",
        instrument="BTCUSDT",
    )
    snapshot = OrderSnapshot(
        id="ord-1",
        source_module="paper_execution_venue_reconciliation",
        created_at=now,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id=aggregate.client_order_id,
        status=OrderStatus.PENDING,
        filled_quantity=0.0,
        remaining_quantity=1.0,
    )
    report = ReconciliationEngine().reconcile_orders(
        internal={aggregate.client_order_id: aggregate},
        venue_orders=(snapshot, replace(snapshot, id="ord-2")),
        mode=ReconciliationMode.STRICT_BLOCK,
        known_client_order_ids={aggregate.client_order_id},
        owned_prefix="pevr-",
    )
    assert report.blocked
    assert any(finding.category == "duplicate_venue_order" for finding in report.findings)


def test_replace_order_can_reuse_open_order_capacity(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    config = PaperExecutionConfig(maximum_open_orders=1)
    service = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="replace-capacity",
    )
    source = _plan_like(
        plan,
        plan_id="replace-source",
        config=config,
    )
    source_result = service.accept_plan(
        source,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    replacement = ExecutionIntent(
        intent_id="replacement",
        role=IntentRole.REPLACEMENT,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.02"),
        limit_price=Decimal("99900"),
    )
    replace_plan = _plan_like(
        plan,
        plan_id="replace-plan",
        config=config,
        action=PlanAction.REPLACE_ORDER,
        intents=(
            ExecutionIntent(
                intent_id="cancel-old",
                role=IntentRole.CANCEL,
                cancel_client_order_id=(source_result.client_order_ids[0]),
            ),
            replacement,
        ),
    )
    result = service.accept_plan(
        replace_plan,
        now=now,
        current_index=1,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert result.accepted
    assert len(broker.pending_snapshots()) == 1
    assert broker.pending_snapshots()[0].client_order_id == result.client_order_ids[0]


def test_duplicate_known_order_from_other_scope_is_ignored(now) -> None:
    other = OrderAggregate(
        "pevr-entry-0-known-other",
        "ord-other",
        Decimal("1"),
        portfolio_id="other",
        instrument="BTCUSDT",
    )
    snapshot = OrderSnapshot(
        id="ord-other",
        source_module="paper_execution_venue_reconciliation",
        created_at=now,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id=other.client_order_id,
        status=OrderStatus.PENDING,
        filled_quantity=0.0,
        remaining_quantity=1.0,
    )
    report = ReconciliationEngine().reconcile_orders(
        internal={},
        venue_orders=(snapshot, replace(snapshot, id="ord-other-2")),
        mode=ReconciliationMode.STRICT_BLOCK,
        known_client_order_ids={other.client_order_id},
        owned_prefix="pevr-",
    )
    assert not report.findings
