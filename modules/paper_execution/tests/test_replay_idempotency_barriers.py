from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from modular_core.events import InMemoryEventStore

from paper_execution import (
    ExecutableTradePlan,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperReasonCode,
    PlanValidationError,
)


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _service(broker, metadata, *, store=None, run_id="replay-barrier"):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )


def _changed_plan(plan: ExecutableTradePlan) -> ExecutableTradePlan:
    return ExecutableTradePlan.create(
        plan_id=plan.plan_id,
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=(replace(plan.intents[0], quantity=Decimal("0.02")),),
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
        input_hashes=plan.input_hashes,
    )


def test_identical_replay_remains_idempotent_after_scope_is_blocked(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    service = _service(broker, metadata)
    first = service.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    service.blocked_scopes.add((plan.portfolio_id, plan.instrument))

    replay = service.accept_plan(
        plan,
        now=now,
        current_index=1,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert replay is first
    assert len(broker.orders) == 1


def test_changed_content_is_rejected_before_blocked_scope_error(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    service = _service(broker, metadata)
    service.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    service.blocked_scopes.add((plan.portfolio_id, plan.instrument))

    with pytest.raises(PlanValidationError) as error:
        service.accept_plan(
            _changed_plan(plan),
            now=now,
            current_index=1,
            mark_price=Decimal("100000"),
            versions=versions,
        )

    assert error.value.reason_code is PaperReasonCode.PLAN_ID_CONTENT_MISMATCH


def test_restored_identical_plan_replays_before_recovery_barrier(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    source = _service(
        broker,
        metadata,
        store=store,
        run_id="replay-after-restore",
    )
    first = source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    snapshot = source.snapshot_state()

    recovered = _service(
        broker,
        metadata,
        store=store,
        run_id="replay-after-restore",
    )
    recovered.restore_state(snapshot)

    replay = recovered.accept_plan(
        plan,
        now=now,
        current_index=1,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert replay == first
    assert len(broker.orders) == 1


def test_restored_changed_content_fails_before_recovery_barrier(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    source = _service(
        broker,
        metadata,
        store=store,
        run_id="changed-after-restore",
    )
    source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    snapshot = source.snapshot_state()

    recovered = _service(
        broker,
        metadata,
        store=store,
        run_id="changed-after-restore",
    )
    recovered.restore_state(snapshot)

    with pytest.raises(PlanValidationError) as error:
        recovered.accept_plan(
            _changed_plan(plan),
            now=now,
            current_index=1,
            mark_price=Decimal("100000"),
            versions=versions,
        )

    assert error.value.reason_code is PaperReasonCode.PLAN_ID_CONTENT_MISMATCH
