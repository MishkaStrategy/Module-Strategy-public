from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RuntimeClock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class DependencyPlanner(Protocol):
    def validate_and_plan(self, modules: Sequence[Any], *, base_timeframe: str) -> Any: ...


@runtime_checkable
class AuditJournal(Protocol):
    def append(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        correlation_id: str | None = None,
        symbol: str | None = None,
    ) -> Any: ...


@runtime_checkable
class RuntimeStateStore(Protocol):
    def load(self, key: str) -> Mapping[str, Any] | None: ...

    def save(self, key: str, value: Mapping[str, Any]) -> None: ...


@runtime_checkable
class RecoveryCoordinator(Protocol):
    def recover(self) -> Any: ...


@runtime_checkable
class ReconciliationCoordinator(Protocol):
    def reconcile(self) -> Any: ...


@runtime_checkable
class HealthManager(Protocol):
    def is_ready(self) -> bool: ...


@runtime_checkable
class MarketDataRuntime(Protocol):
    def start(self) -> Any: ...

    def stop(self) -> Any: ...


@runtime_checkable
class PlanExecutionBoundary(Protocol):
    def accept(
        self,
        envelope: object,
        *,
        now: datetime,
        current_index: int,
        mark_price: Decimal,
        paper_versions: object,
    ) -> object: ...


@runtime_checkable
class EntryCancellationBoundary(Protocol):
    def cancel_pending_entries(self, *, reason: str, timestamp: datetime) -> bool: ...


@runtime_checkable
class CheckpointCoordinator(Protocol):
    def capture(self) -> Any: ...
