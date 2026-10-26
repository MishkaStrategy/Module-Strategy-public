from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from modular_core import PositionSide
from modular_core.events import InMemoryEventStore

from paper_execution import (
    ExpectedPositionState,
    LifecycleState,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
)
from paper_execution._utils import canonical_json, sha256_text


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _coordinator(broker, metadata, store, run_id):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )


def _restored(plan, versions, now, broker, metadata, run_id):
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, run_id)
    source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    target = _coordinator(broker, metadata, store, run_id)
    target.restore_state(source.snapshot_state())
    return target


def test_position_then_orders_completes_recovery(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    recovered = _restored(
        plan,
        versions,
        now,
        broker,
        metadata,
        "position-first",
    )

    assert (
        recovered.reconcile_position_state(
            portfolio_id=plan.portfolio_id,
            instrument=plan.instrument,
            expected=None,
            observed=None,
        )
        == ()
    )
    assert recovered.recovery_pending
    assert recovered.lifecycle is LifecycleState.RECONCILING

    report = recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert not report.blocked
    assert not recovered.recovery_pending
    assert recovered.lifecycle is LifecycleState.READY


def test_orders_then_position_completes_recovery(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    recovered = _restored(
        plan,
        versions,
        now,
        broker,
        metadata,
        "orders-first",
    )

    report = recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert not report.blocked
    assert recovered.recovery_pending
    assert recovered.lifecycle is LifecycleState.RECONCILING

    assert (
        recovered.reconcile_position_state(
            portfolio_id=plan.portfolio_id,
            instrument=plan.instrument,
            expected=None,
            observed=None,
        )
        == ()
    )
    assert not recovered.recovery_pending
    assert recovered.lifecycle is LifecycleState.READY


def test_clean_orders_cannot_clear_unresolved_position_block(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    recovered = _restored(
        plan,
        versions,
        now,
        broker,
        metadata,
        "position-block",
    )
    scope = (plan.portfolio_id, plan.instrument)

    findings = recovered.reconcile_position_state(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        expected=ExpectedPositionState(PositionSide.LONG, Decimal("1")),
        observed=None,
    )
    assert [finding.category for finding in findings] == ["position_missing"]
    assert scope in recovered.blocked_scopes
    assert recovered.lifecycle is LifecycleState.BLOCKED

    report = recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert not report.blocked
    assert recovered.recovery_pending
    assert scope in recovered.blocked_scopes
    assert recovered.lifecycle is LifecycleState.BLOCKED


def test_persisted_block_is_restored_conservatively(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "persisted-block")
    source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    state = deepcopy(dict(source.snapshot_state()))
    state["blocked_scopes"] = [{"portfolio_id": plan.portfolio_id, "instrument": plan.instrument}]
    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))

    recovered = _coordinator(broker, metadata, store, "persisted-block")
    recovered.restore_state(state)
    scope = (plan.portfolio_id, plan.instrument)
    assert scope in recovered.blocked_scopes

    recovered.reconcile(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
    )
    assert scope in recovered.blocked_scopes
    recovered.reconcile_position_state(
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        expected=None,
        observed=None,
    )
    assert scope not in recovered.blocked_scopes
    assert not recovered.recovery_pending
