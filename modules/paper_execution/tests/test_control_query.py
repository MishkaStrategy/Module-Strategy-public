from __future__ import annotations

from decimal import Decimal

from modular_core.events import InMemoryEventStore

from paper_execution import PaperExecutionConfig, PaperExecutionCoordinator
from paper_execution.control_query import ReadOnlyPaperExecutionQuery


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def test_control_query_projects_owner_state_without_mutation(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    coordinator = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="control-query",
        event_store=InMemoryEventStore(),
    )
    coordinator.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    before = coordinator.snapshot_state()

    query = ReadOnlyPaperExecutionQuery(coordinator)
    overview = query.snapshot()
    orders = query.list_orders(limit=200, offset=0)
    blocked = query.list_blocked_scopes()

    assert overview.native_lifecycle == coordinator.lifecycle.value
    assert overview.order_count == len(coordinator.orders)
    assert overview.active_order_count <= overview.order_count
    assert overview.blocked_scope_count == len(coordinator.blocked_scopes)
    assert orders
    assert orders[0].portfolio_id == plan.portfolio_id
    assert orders[0].instrument == plan.instrument
    assert blocked == tuple(
        sorted(
            blocked,
            key=lambda item: (item.portfolio_id, item.instrument),
        )
    )
    assert coordinator.snapshot_state() == before


def test_control_query_pagination_is_bounded(broker, metadata) -> None:
    coordinator = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="control-query-bounds",
        event_store=InMemoryEventStore(),
    )
    query = ReadOnlyPaperExecutionQuery(coordinator)

    for limit in (0, 201):
        try:
            query.list_orders(limit=limit)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid limit must fail closed")

    try:
        query.list_orders(offset=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative offset must fail closed")
