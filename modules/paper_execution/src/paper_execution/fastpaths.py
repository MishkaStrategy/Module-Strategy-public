from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from modular_core import OrderSnapshot

from .adapter import CorePaperVenue
from .contracts import ExecutableTradePlan, StateVersions, VenueMetadataSnapshot
from .coordinator import _PreparedIntent
from .enums import IntentRole
from .router_adapter import (
    RouterAdaptation,
    RouterOCCProvider,
    RouterOCCSnapshot,
    RouterPlanAdapter,
    RouterPlanEvidence,
)
from .router_execution import RouterExecutionAdapter as LegacyRouterExecutionAdapter
from .router_position import RouterPositionProvider
from .service import PaperExecutionCoordinator as LegacyPaperExecutionCoordinator
from .state_machine import OrderAggregate


class _CachingRouterPlanAdapter(RouterPlanAdapter):
    """Reuse one exact envelope inspection across inspect+adapt in one admission."""

    def __init__(self) -> None:
        self._cached_envelope: object | None = None
        self._cached_now: datetime | None = None
        self._cached_result: tuple[RouterPlanEvidence, Mapping[str, Any]] | None = None

    def inspect(
        self,
        envelope: object,
        *,
        now: datetime,
    ) -> tuple[RouterPlanEvidence, Mapping[str, Any]]:
        if (
            envelope is self._cached_envelope
            and now == self._cached_now
            and self._cached_result is not None
        ):
            return self._cached_result
        result = super().inspect(envelope, now=now)
        self._cached_envelope = envelope
        self._cached_now = now
        self._cached_result = result
        return result

    def adapt(
        self,
        envelope: object,
        *,
        now: datetime,
        paper_versions: StateVersions,
        metadata_version: str,
        paper_configuration_hash: str,
        occ_snapshot: RouterOCCSnapshot,
    ) -> RouterAdaptation:
        try:
            return super().adapt(
                envelope,
                now=now,
                paper_versions=paper_versions,
                metadata_version=metadata_version,
                paper_configuration_hash=paper_configuration_hash,
                occ_snapshot=occ_snapshot,
            )
        finally:
            if envelope is self._cached_envelope and now == self._cached_now:
                self._cached_envelope = None
                self._cached_now = None
                self._cached_result = None


class RouterExecutionAdapter(LegacyRouterExecutionAdapter):
    """Public Router boundary with single-pass immutable-envelope inspection."""

    def __init__(
        self,
        coordinator: Any,
        *,
        occ_provider: RouterOCCProvider,
        position_provider: RouterPositionProvider | None = None,
        plan_adapter: RouterPlanAdapter | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            occ_provider=occ_provider,
            position_provider=position_provider,
            plan_adapter=plan_adapter or _CachingRouterPlanAdapter(),
        )


class _ScopedOrderView:
    """Read-only full lookup with one cached active-scope items traversal."""

    def __init__(
        self,
        orders: Mapping[str, OrderAggregate],
        scope: tuple[str, str],
    ) -> None:
        self._orders = orders
        self._items = tuple(
            (client_id, aggregate)
            for client_id, aggregate in orders.items()
            if (aggregate.portfolio_id, aggregate.instrument) == scope
            and not aggregate.state.terminal
        )

    def __contains__(self, key: object) -> bool:
        return key in self._orders

    def get(self, key: str, default: OrderAggregate | None = None) -> OrderAggregate | None:
        return self._orders.get(key, default)

    def items(self) -> tuple[tuple[str, OrderAggregate], ...]:
        return self._items


class _PendingSnapshotElidingVenue:
    """Delegate venue operations while proving an empty no-cancel snapshot set."""

    def __init__(self, venue: CorePaperVenue) -> None:
        self._venue = venue

    def pending_snapshots(self) -> tuple[OrderSnapshot, ...]:
        return ()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._venue, name)


class PaperExecutionCoordinator(LegacyPaperExecutionCoordinator):
    """Public coordinator with profiled admission-only traversal reductions.

    Paper execution is synchronous. During preflight, the existing order mapping
    is replaced only for the duration of the call by a read-only view that keeps
    full key lookup semantics but caches the active scope once. Plans without a
    cancel intent also skip materializing venue pending snapshots that cannot be
    consumed by that plan.
    """

    def _prepare_plan(
        self,
        *,
        plan: ExecutableTradePlan,
        metadata: VenueMetadataSnapshot,
        now: datetime,
        mark_price: Decimal,
        scope: tuple[str, str],
    ) -> tuple[_PreparedIntent, ...]:
        original_orders = self.orders
        original_venue = self.venue
        scoped_orders = _ScopedOrderView(original_orders, scope)
        has_cancel = any(intent.role is IntentRole.CANCEL for intent in plan.intents)
        self.orders = cast(Any, scoped_orders)
        if not has_cancel:
            self.venue = cast(
                CorePaperVenue,
                _PendingSnapshotElidingVenue(original_venue),
            )
        try:
            return super()._prepare_plan(
                plan=plan,
                metadata=metadata,
                now=now,
                mark_price=mark_price,
                scope=scope,
            )
        finally:
            self.orders = original_orders
            self.venue = original_venue
